from __future__ import annotations

import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector
from src.connectors.sqlserver import SQLServerConnector


TARGETS: dict[str, str] = {
    "48392858": "missing inscripción",
    "46603659": "missing cuota 1",
    "47177813": "missing inscripción",
    "39847440": "missing cuota 1",
    "34509186": "missing inscripción",
    "47518087": "missing inscripción",
    "38197312": "missing inscripción",
    "34589883": "missing inscripción",
    "41411707": "missing inscripción",
    "47987134": "missing inscripción",
    "45955611": "missing inscripción",
    "45093475": "missing inscripción",
    "33151205": "missing inscripción",
    "42259642": "missing inscripción",
    "43235585": "missing inscripción",
    "40035946": "missing inscripción",
    "32663821": "missing inscripción",
    "43476819": "missing cuota 1 but has cuota 2",
    "43933504": "missing inscripción + cuota 1 + cuota 2, has cuota 3",
    "31153485": "missing inscripción?",
    "26938030": "inscripción mal alojada + cuota 1 amount not split",
    "41636864": "missing inscripción + cuota 3 misallocated",
    "34614823": "missing inscripción",
    "48434146": "missing inscripción",
    "26107661": "missing inscripción",
    "47061902": "missing inscripción",
}

CUOTA_RE = re.compile(r"Cuota\s+(\d+)", re.IGNORECASE)


def parse_money(value: str | None) -> Decimal | None:
    if not value:
        return None
    text = str(value).replace("$", "").replace(" ", "").replace("\xa0", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def extract_cuota(concepto: str) -> int | None:
    m = CUOTA_RE.search(concepto or "")
    return int(m.group(1)) if m else None


def near(a: Decimal | None, b: Decimal | None, tolerance: Decimal = Decimal("0.02")) -> bool:
    if a is None or b is None:
        return False
    if b == 0:
        return a == 0
    return abs(a - b) / b <= tolerance


def main() -> None:
    config = load_config("config/settings.yaml")

    sheets = SheetsConnector(config["sheets"])
    sheets.connect()
    spreadsheet = sheets._client.open_by_key(config["sheets"]["spreadsheet_id"])
    cobros = spreadsheet.worksheet("COBROS")
    cobros_values = cobros.get_all_values()

    sql = SQLServerConnector(config["database"])
    sql.connect()
    cursor = sql.connection.cursor()

    # Build sheet rows by DNI
    sheet_by_dni: dict[str, list[dict]] = defaultdict(list)
    for idx, row in enumerate(cobros_values[1:], start=2):
        if len(row) < 14:
            continue
        dni = (row[5] or "").strip()
        if dni not in TARGETS:
            continue
        sheet_by_dni[dni].append(
            {
                "row": idx,
                "comision": (row[2] or "").strip(),
                "fecha": (row[3] or "").strip(),
                "tipo": (row[4] or "").strip(),
                "concepto": (row[6] or "").strip(),
                "monto": parse_money(row[7]),
                "monto_raw": (row[7] or "").strip(),
                "medio": (row[8] or "").strip(),
                "id_mov": (row[12] or "").strip(),
                "id_pago": (row[13] or "").strip(),
            }
        )

    # Fetch active memberships + commission pricing for targets
    report: list[str] = []
    for dni, expected_note in TARGETS.items():
        cursor.execute(
            """
            SELECT
                c.id_comision,
                c.id_curso,
                c.nombre,
                c.valor_inscripcion,
                c.valor_inscripcion_promocion,
                c.valor_cuota,
                c.valor_cuota_bonificada,
                c.cantidad_cuotas,
                c.fecha_inicio,
                cp.id_rol,
                cp.id_estado_academico,
                cp.id_estado_administrativo,
                cp.eliminado
            FROM COMISIONES_PERSONAS cp
            INNER JOIN COMISIONES c ON c.id_comision = cp.id_comision
            INNER JOIN PERSONAS p ON p.id_persona = cp.id_persona
            WHERE p.dni = ?
              AND YEAR(c.fecha_inicio) = 2026
              AND cp.id_rol = 1
            ORDER BY c.nombre
            """,
            (dni,),
        )
        memberships = cursor.fetchall()

        report.append("=" * 95)
        report.append(f"DNI {dni} | esperado: {expected_note}")
        if not memberships:
            report.append("  Sin membresías activas (rol=1) en DB para 2026")
            continue

        sheet_rows = sheet_by_dni.get(dni, [])
        rows_by_commission: dict[str, list[dict]] = defaultdict(list)
        for row in sheet_rows:
            rows_by_commission[row["comision"]].append(row)

        for m in memberships:
            commission = str(m[2]).strip()
            insc = m[4] if m[4] is not None else m[3]
            cuota = m[6] if m[6] is not None and m[6] > 0 else m[5]
            total_cuotas = m[7] or 0

            ventas = [r for r in rows_by_commission.get(commission, []) if r["tipo"].casefold() == "venta"]
            cobros_comm = [r for r in rows_by_commission.get(commission, []) if r["tipo"].casefold() == "cobro"]
            inscripciones = [r for r in ventas if "inscrip" in r["concepto"].casefold()]
            cuotas = sorted(
                [(extract_cuota(r["concepto"]), r) for r in ventas if extract_cuota(r["concepto"]) is not None],
                key=lambda item: item[0],
            )
            cuota_numbers = [n for n, _r in cuotas]
            max_cuota = max(cuota_numbers) if cuota_numbers else 0
            missing_before_max = [n for n in range(1, max_cuota + 1) if n not in cuota_numbers]

            findings: list[str] = []
            if not inscripciones and (cuota_numbers or any("pago único" in r["concepto"].casefold() for r in ventas)):
                findings.append("sin inscripción en hoja")
            if max_cuota >= 1 and 1 not in cuota_numbers:
                findings.append("sin cuota 1")
            if missing_before_max:
                findings.append(f"saltos de cuota: falta {missing_before_max}")

            # Detect cuota rows carrying combined / wrong amounts
            for cuota_n, row in cuotas:
                amount = row["monto"]
                if cuota is not None and amount is not None and not near(amount, Decimal(cuota)):
                    combo_insc_cuota = (Decimal(insc) if insc is not None else Decimal("0")) + Decimal(cuota)
                    if insc is not None and near(amount, combo_insc_cuota):
                        findings.append(
                            f"Cuota {cuota_n} tiene monto combinado inscripción+cuota ({row['monto_raw']})"
                        )
                    else:
                        findings.append(
                            f"Cuota {cuota_n} monto no estándar ({row['monto_raw']}, cuota esperada {cuota})"
                        )

            # Detect inscripción amount wrong / possibly allocated as cuota
            for row in inscripciones:
                amount = row["monto"]
                if insc is not None and amount is not None and not near(amount, Decimal(insc)):
                    findings.append(
                        f"Inscripción con monto no estándar ({row['monto_raw']}, insc esperada {insc})"
                    )

            report.append(
                f"  {commission} | curso={m[1]} | cuotas={total_cuotas} | rows={len(rows_by_commission.get(commission, []))}"
            )
            report.append(
                f"    precios: insc={insc} cuota={cuota} | académ={m[10]} admin={m[11]} eliminado={m[12]}"
            )
            if cuota_numbers:
                report.append(f"    cuotas presentes: {cuota_numbers}")
            else:
                report.append("    cuotas presentes: []")
            report.append(f"    inscripciones: {len(inscripciones)} | cobros: {len(cobros_comm)}")
            if findings:
                for finding in findings:
                    report.append(f"    hallazgo: {finding}")
            else:
                report.append("    hallazgo: sin anomalías obvias según la hoja")

            if ventas:
                for row in ventas[:8]:
                    report.append(
                        f"      Venta row {row['row']}: {row['fecha']} | {row['concepto']} | {row['monto_raw']} | pago={row['id_pago']}"
                    )
            else:
                report.append("      Sin filas Venta en COBROS para esta comisión")

        other_commissions = sorted(set(rows_by_commission) - {str(m[2]).strip() for m in memberships})
        if other_commissions:
            report.append(f"  Otras comisiones en hoja sin membership rol=1: {other_commissions}")

    print("\n".join(report))
    sql.connection.close()


if __name__ == "__main__":
    main()
