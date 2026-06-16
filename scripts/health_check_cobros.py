"""Read-only health-check for COBROS anomalies.

Focuses on anomalies that indicate historical engine damage or dangerous
states that should not be processed automatically again.

Examples:
  python scripts/health_check_cobros.py
  python scripts/health_check_cobros.py --commission "PERITO-S-PERGAMINO-2026"
  python scripts/health_check_cobros.py --org 1
  python scripts/health_check_cobros.py --dni 42530660
  python scripts/health_check_cobros.py --json-out data/health_check_pergamino.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector
from src.connectors.sqlserver import SQLServerConnector
from src.models.sheet import SheetRow


CUOTA_RE = re.compile(r"Cuota\s+(\d+)", re.IGNORECASE)


def extract_cuota_numbers(concepto: str | None) -> list[int]:
    if not concepto:
        return []
    return [int(match) for match in CUOTA_RE.findall(concepto)]


def is_close_amount(actual: Decimal, target: Decimal | None, tolerance: Decimal = Decimal("0.02")) -> bool:
    if target is None or target <= 0:
        return False
    return abs(actual - target) / target <= tolerance


def commission_has_short_single(commission: dict[str, Any]) -> bool:
    cuota = commission.get("valor_cuota_bonificada") or commission.get("valor_cuota")
    total_cuotas = commission.get("cantidad_cuotas") or 0
    duration_months = commission.get("duracion_meses") or 0
    return (cuota is None or cuota <= 0) and total_cuotas == 0 and 0 < duration_months < 9


def add_anomaly(
    anomalies: list[dict[str, Any]],
    severity: str,
    code: str,
    commission: str,
    dni: str,
    student_name: str,
    message: str,
    rows: list[int],
    ) -> None:
    anomalies.append(
        {
            "severity": severity,
            "code": code,
            "commission": commission,
            "dni": dni,
            "student_name": student_name,
            "message": message,
            "rows": rows,
        }
    )


def build_action_plan(anomaly: dict[str, Any]) -> dict[str, Any]:
    code = anomaly["code"]

    # Current philosophy: structural COBROS anomalies are usually NOT safe to
    # auto-apply without a stronger domain-specific fixer. We still attach an
    # explicit plan so the operator knows what to do, and the CLI can evolve to
    # auto-apply only the truly safe subset later.
    plan_map = {
        "payment_id_across_multiple_commissions": {
            "what_to_do": "Review which commission legitimately owns the payment_id and remove the duplicate allocation from the other commission(s).",
            "recommended_action": "review_manual_cross_commission_payment",
            "can_auto_apply": False,
            "confidence": 0.95,
        },
        "non_positive_monto": {
            "what_to_do": "Inspect the row and correct or remove the non-positive monto manually.",
            "recommended_action": "review_manual_non_positive_monto",
            "can_auto_apply": False,
            "confidence": 1.0,
        },
        "multi_cuota_concept_label": {
            "what_to_do": "Split the malformed concept into separate rows or re-run the affected student/commission after fixing the source state.",
            "recommended_action": "review_manual_split_multi_cuota_label",
            "can_auto_apply": False,
            "confidence": 0.95,
        },
        "mixed_concept_label": {
            "what_to_do": "Split the mixed concept label into separate conceptual rows (e.g. Inscripción + Cuota 1) manually.",
            "recommended_action": "review_manual_split_mixed_concept",
            "can_auto_apply": False,
            "confidence": 0.95,
        },
        "missing_inscription_with_existing_cuotas": {
            "what_to_do": "Review whether the inscription is missing or was absorbed into Cuota 1; do not continue numbering until fixed.",
            "recommended_action": "review_manual_missing_inscription",
            "can_auto_apply": False,
            "confidence": 0.9,
        },
        "duplicate_cuota_number": {
            "what_to_do": "Review duplicate cuota rows and keep only the valid sequence.",
            "recommended_action": "review_manual_duplicate_quota",
            "can_auto_apply": False,
            "confidence": 0.95,
        },
        "quota_sequence_gap": {
            "what_to_do": "Review the cuota sequence gap before allowing new allocations on this student.",
            "recommended_action": "review_manual_sequence_gap",
            "can_auto_apply": False,
            "confidence": 0.95,
        },
        "quota_exceeds_commission_total": {
            "what_to_do": "Review rows beyond the commission's total cuota count and remove/renumber them manually.",
            "recommended_action": "review_manual_excess_quota",
            "can_auto_apply": False,
            "confidence": 0.98,
        },
        "cuota1_matches_inscription_amount": {
            "what_to_do": "Review whether Cuota 1 is actually the missing inscription and whether subsequent cuotas need renumbering.",
            "recommended_action": "review_manual_relabel_inscription",
            "can_auto_apply": False,
            "confidence": 0.9,
        },
        "cuota1_combines_inscription_and_cuota": {
            "what_to_do": "Review splitting Cuota 1 into Inscripción + Cuota 1.",
            "recommended_action": "review_manual_split_inscription_and_cuota",
            "can_auto_apply": False,
            "confidence": 0.95,
        },
        "non_standard_inscription_amount": {
            "what_to_do": "Review whether the inscription amount is discounted, belongs to another commission, or was misallocated.",
            "recommended_action": "review_manual_inscription_amount",
            "can_auto_apply": False,
            "confidence": 0.8,
        },
        "non_standard_cuota_amount": {
            "what_to_do": "Review whether the cuota amount is discounted, recargo, foreign commission pricing, or a combined amount.",
            "recommended_action": "review_manual_cuota_amount",
            "can_auto_apply": False,
            "confidence": 0.8,
        },
        "cobro_without_matching_venta": {
            "what_to_do": "Review why the Cobro exists without a matching Venta and reconstruct the missing Venta if appropriate.",
            "recommended_action": "review_manual_orphan_cobro",
            "can_auto_apply": False,
            "confidence": 0.95,
        },
        "cuota_in_zero_cuota_commission": {
            "what_to_do": "Review rows that created cuotas in a commission that should not have cuotas.",
            "recommended_action": "review_manual_zero_cuota_commission",
            "can_auto_apply": False,
            "confidence": 0.98,
        },
    }

    default = {
        "what_to_do": "Review manually.",
        "recommended_action": "review_manual",
        "can_auto_apply": False,
        "confidence": 0.5,
    }
    return plan_map.get(code, default)


def apply_safe_actions(anomalies: list[dict[str, Any]]) -> dict[str, Any]:
    applicable = [anomaly for anomaly in anomalies if anomaly["plan"]["can_auto_apply"]]
    # No structural COBROS anomaly is auto-applied yet. This function exists
    # so the workflow can evolve safely without changing the interface.
    return {
        "applicable": len(applicable),
        "applied": 0,
        "skipped": len(anomalies) - len(applicable),
        "details": [],
    }


@click.command()
@click.option("--org", default=None, type=int, help="Organization ID to audit (default: from config)")
@click.option("--year", default=None, type=int, help="Year to audit (default: from config)")
@click.option("--commission", default=None, help="Filter by commission substring")
@click.option("--dni", default=None, help="Filter by exact DNI")
@click.option("--json-out", default=None, help="Optional path to write full anomaly JSON")
@click.option("--apply-safe", is_flag=True, help="Apply only actions explicitly marked as safe")
def main(org: int | None, year: int | None, commission: str | None, dni: str | None, json_out: str | None, apply_safe: bool) -> None:
    config = load_config("config/settings.yaml")
    target_org = org if org is not None else int(config.get("agent", {}).get("id_organizacion", 2))
    target_year = year if year is not None else int(config.get("agent", {}).get("year", 2026))

    sheets = SheetsConnector(config["sheets"])
    sheets.connect()
    sheet_rows = sheets.read_all_rows()

    sql = SQLServerConnector(config["database"])
    sql.connect()
    cursor = sql.connection.cursor()
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
            c.duracion_meses,
            c.fecha_inicio,
            c.fecha_finalizacion,
            c.id_estado_comision
        FROM COMISIONES c
        WHERE c.id_organizacion = ?
          AND YEAR(c.fecha_inicio) = ?
          AND c.borrado = 0
          AND c.id_estado_comision IN (2, 3, 4)
        ORDER BY c.nombre
        """,
        (target_org, target_year),
    )
    commissions = cursor.fetchall()
    sql.connection.close()

    commission_map: dict[str, dict[str, Any]] = {}
    for row in commissions:
        name = str(row[2]).strip()
        if commission and commission.strip().casefold() not in name.casefold():
            continue
        commission_map[name] = {
            "id_comision": row[0],
            "id_curso": row[1],
            "nombre": name,
            "valor_inscripcion": row[3],
            "valor_inscripcion_promocion": row[4],
            "valor_cuota": row[5],
            "valor_cuota_bonificada": row[6],
            "cantidad_cuotas": row[7],
            "duracion_meses": row[8],
            "fecha_inicio": str(row[9]) if row[9] is not None else None,
            "fecha_finalizacion": str(row[10]) if row[10] is not None else None,
            "id_estado_comision": row[11],
        }

    scoped_rows = [
        row for row in sheet_rows
        if (row.comision or "").strip() in commission_map
        and (dni is None or row.dni.strip() == dni.strip())
    ]

    grouped: dict[tuple[str, str], list[SheetRow]] = defaultdict(list)
    for row in scoped_rows:
        grouped[((row.comision or "").strip(), row.dni.strip())].append(row)

    anomalies: list[dict[str, Any]] = []

    # Global payment_id cross-commission check.
    payment_usage: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for row in scoped_rows:
        if row.id_pago_mp is not None and row.id_pago_mp > 0:
            payment_usage[row.id_pago_mp].add(((row.comision or "").strip(), row.dni.strip()))
    for payment_id, usages in payment_usage.items():
        if len(usages) <= 1:
            continue
        usage_list = sorted(usages)
        for commission_name, student_dni in usage_list:
            rows = [r.row_number for r in scoped_rows if r.id_pago_mp == payment_id and (r.comision or "").strip() == commission_name and r.dni.strip() == student_dni]
            add_anomaly(
                anomalies,
                severity="critical",
                code="payment_id_across_multiple_commissions",
                commission=commission_name,
                dni=student_dni,
                student_name="",
                message=f"id_pago_mp {payment_id} appears in multiple commissions: {usage_list}",
                rows=rows,
            )

    for (commission_name, student_dni), rows in grouped.items():
        commission_info = commission_map[commission_name]
        student_name = next((row.estudiante or "" for row in rows if row.estudiante), "")
        venta_rows = [row for row in rows if (row.tipo_movimiento or "").strip().casefold() == "venta"]
        cobro_rows = [row for row in rows if (row.tipo_movimiento or "").strip().casefold() == "cobro"]
        insc_price = commission_info["valor_inscripcion_promocion"] or commission_info["valor_inscripcion"]
        cuota_price = commission_info["valor_cuota_bonificada"] or commission_info["valor_cuota"]
        total_cuotas = commission_info["cantidad_cuotas"] or 0
        is_short_single = commission_has_short_single(commission_info)

        has_inscription = False
        cuota_numbers: list[int] = []
        cuota_counts: Counter[int] = Counter()

        venta_concepts: set[str] = set()
        for row in venta_rows:
            concepto = (row.concepto or "").strip()
            concepto_lower = concepto.casefold()
            venta_concepts.add(concepto_lower)

            if row.monto <= 0:
                add_anomaly(
                    anomalies,
                    severity="critical",
                    code="non_positive_monto",
                    commission=commission_name,
                    dni=student_dni,
                    student_name=student_name,
                    message=f"Venta row {row.row_number} has non-positive monto {row.monto}",
                    rows=[row.row_number],
                )

            cuota_matches = extract_cuota_numbers(concepto)
            has_inscripcion_label = "inscripción" in concepto_lower or "inscripcion" in concepto_lower
            has_pago_unico = "pago único" in concepto_lower or "pago unico" in concepto_lower

            if len(cuota_matches) > 1:
                add_anomaly(
                    anomalies,
                    severity="critical",
                    code="multi_cuota_concept_label",
                    commission=commission_name,
                    dni=student_dni,
                    student_name=student_name,
                    message=f"Venta row {row.row_number} concept has multiple cuota numbers: {concepto}",
                    rows=[row.row_number],
                )

            if has_inscripcion_label and (cuota_matches or has_pago_unico):
                add_anomaly(
                    anomalies,
                    severity="critical",
                    code="mixed_concept_label",
                    commission=commission_name,
                    dni=student_dni,
                    student_name=student_name,
                    message=f"Venta row {row.row_number} mixes multiple concepts in one label: {concepto}",
                    rows=[row.row_number],
                )

            if has_inscripcion_label:
                has_inscription = True
                if insc_price is not None and not is_close_amount(row.monto, Decimal(insc_price)):
                    add_anomaly(
                        anomalies,
                        severity="suspicious",
                        code="non_standard_inscription_amount",
                        commission=commission_name,
                        dni=student_dni,
                        student_name=student_name,
                        message=f"Inscripción row {row.row_number} has {row.monto} but expected {insc_price}",
                        rows=[row.row_number],
                    )
                continue

            cuota_n = cuota_matches[0] if cuota_matches else None
            if cuota_n is not None:
                cuota_numbers.append(cuota_n)
                cuota_counts[cuota_n] += 1

                if total_cuotas == 0:
                    add_anomaly(
                        anomalies,
                        severity="critical",
                        code="cuota_in_zero_cuota_commission",
                        commission=commission_name,
                        dni=student_dni,
                        student_name=student_name,
                        message=f"Venta row {row.row_number} is {concepto} in a commission with zero cuotas",
                        rows=[row.row_number],
                    )

                if cuota_n == 1 and insc_price is not None and is_close_amount(row.monto, Decimal(insc_price)):
                    add_anomaly(
                        anomalies,
                        severity="critical",
                        code="cuota1_matches_inscription_amount",
                        commission=commission_name,
                        dni=student_dni,
                        student_name=student_name,
                        message=f"Cuota 1 row {row.row_number} has inscription amount {row.monto}",
                        rows=[row.row_number],
                    )

                if (
                    cuota_n == 1
                    and insc_price is not None
                    and cuota_price is not None
                    and is_close_amount(row.monto, Decimal(insc_price) + Decimal(cuota_price))
                ):
                    add_anomaly(
                        anomalies,
                        severity="critical",
                        code="cuota1_combines_inscription_and_cuota",
                        commission=commission_name,
                        dni=student_dni,
                        student_name=student_name,
                        message=f"Cuota 1 row {row.row_number} combines inscripción + cuota ({row.monto})",
                        rows=[row.row_number],
                    )

                if cuota_price is not None:
                    expected_combo = (Decimal(insc_price) + Decimal(cuota_price)) if insc_price is not None else None
                    if not is_close_amount(row.monto, Decimal(cuota_price)) and not is_close_amount(row.monto, expected_combo):
                        add_anomaly(
                            anomalies,
                            severity="suspicious",
                            code="non_standard_cuota_amount",
                            commission=commission_name,
                            dni=student_dni,
                            student_name=student_name,
                            message=f"{concepto} row {row.row_number} has non-standard amount {row.monto} (expected cuota {cuota_price})",
                            rows=[row.row_number],
                        )

        if cuota_numbers and not has_inscription and not is_short_single:
            add_anomaly(
                anomalies,
                severity="critical",
                code="missing_inscription_with_existing_cuotas",
                commission=commission_name,
                dni=student_dni,
                student_name=student_name,
                message="Student has cuota Venta rows but no Inscripción Venta row",
                rows=[row.row_number for row in venta_rows],
            )

        for cuota_n, count in cuota_counts.items():
            if count > 1:
                dup_rows = [row.row_number for row in venta_rows if extract_cuota_numbers(row.concepto or "")[:1] == [cuota_n]]
                add_anomaly(
                    anomalies,
                    severity="critical",
                    code="duplicate_cuota_number",
                    commission=commission_name,
                    dni=student_dni,
                    student_name=student_name,
                    message=f"Cuota {cuota_n} appears {count} times",
                    rows=dup_rows,
                )

        if cuota_numbers:
            max_cuota = max(cuota_numbers)
            missing = [n for n in range(1, max_cuota + 1) if n not in cuota_counts]
            if missing:
                add_anomaly(
                    anomalies,
                    severity="critical",
                    code="quota_sequence_gap",
                    commission=commission_name,
                    dni=student_dni,
                    student_name=student_name,
                    message=f"Missing cuota numbers before {max_cuota}: {missing}",
                    rows=[row.row_number for row in venta_rows],
                )
            if total_cuotas > 0 and max_cuota > total_cuotas:
                add_anomaly(
                    anomalies,
                    severity="critical",
                    code="quota_exceeds_commission_total",
                    commission=commission_name,
                    dni=student_dni,
                    student_name=student_name,
                    message=f"Max cuota {max_cuota} exceeds commission total {total_cuotas}",
                    rows=[row.row_number for row in venta_rows],
                )

        # Cobro without matching Venta concept.
        venta_concepts_normalized = {row.concepto.strip().casefold() for row in venta_rows}
        for row in cobro_rows:
            concepto_lower = (row.concepto or "").strip().casefold()
            if concepto_lower not in venta_concepts_normalized:
                add_anomaly(
                    anomalies,
                    severity="critical",
                    code="cobro_without_matching_venta",
                    commission=commission_name,
                    dni=student_dni,
                    student_name=student_name,
                    message=f"Cobro row {row.row_number} has no matching Venta concept: {row.concepto}",
                    rows=[row.row_number],
                )

    severity_counts = Counter(anomaly["severity"] for anomaly in anomalies)
    code_counts = Counter(anomaly["code"] for anomaly in anomalies)
    for anomaly in anomalies:
        anomaly["plan"] = build_action_plan(anomaly)
    action_counts = Counter(anomaly["plan"]["recommended_action"] for anomaly in anomalies)
    auto_counts = Counter("auto" if anomaly["plan"]["can_auto_apply"] else "manual" for anomaly in anomalies)

    print(f"Health check | org={target_org} year={target_year}")
    print(f"Commission scope: {len(commission_map)}")
    print(f"Sheet rows in scope: {len(scoped_rows)}")
    print(f"Student/commission groups: {len(grouped)}")
    print(f"Total anomalies: {len(anomalies)}")
    print("\nBy severity:")
    for severity, count in severity_counts.most_common():
        print(f"  {count:>4} | {severity}")
    print("\nBy code:")
    for code, count in code_counts.most_common():
        print(f"  {count:>4} | {code}")
    print("\nBy recommended action:")
    for action, count in action_counts.most_common():
        print(f"  {count:>4} | {action}")
    print("\nAuto-apply eligibility:")
    for key, count in auto_counts.most_common():
        print(f"  {count:>4} | {key}")

    print("\nSample anomalies:")
    for anomaly in anomalies[:40]:
        print(
            f"  [{anomaly['severity']}] {anomaly['code']} | {anomaly['commission']} | "
            f"DNI {anomaly['dni']} | rows={anomaly['rows']} | {anomaly['message']}"
        )
        print(
            f"      -> action={anomaly['plan']['recommended_action']} | "
            f"auto={anomaly['plan']['can_auto_apply']} | confidence={anomaly['plan']['confidence']}"
        )

    apply_result: dict[str, Any] | None = None
    if apply_safe:
        apply_result = apply_safe_actions(anomalies)
        print("\nApply-safe result:")
        print(
            f"  applicable={apply_result['applicable']} | applied={apply_result['applied']} | "
            f"skipped={apply_result['skipped']}"
        )
        if apply_result["applicable"] == 0:
            print("  No safe structural COBROS actions are auto-applied yet; all current findings require manual review.")

    if json_out:
        out_path = Path(json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "org": target_org,
                    "year": target_year,
                    "commission_filter": commission,
                    "dni_filter": dni,
                    "summary": {
                        "commission_count": len(commission_map),
                        "row_count": len(scoped_rows),
                        "group_count": len(grouped),
                        "total_anomalies": len(anomalies),
                        "by_severity": severity_counts,
                        "by_code": code_counts,
                        "by_action": action_counts,
                        "auto_apply": auto_counts,
                    },
                    "apply_safe_result": apply_result,
                    "anomalies": anomalies,
                },
                handle,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        print(f"\nJSON written to {out_path}")


if __name__ == "__main__":
    main()
