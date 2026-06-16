from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector
from src.connectors.sqlserver import SQLServerConnector


TARGET_DNIS = [
    "27423392",
    "44184950",
    "38282178",
    "29710967",
]


def main() -> None:
    config = load_config("config/settings.yaml")

    sheets = SheetsConnector(config["sheets"])
    sheets.connect()
    spreadsheet = sheets._client.open_by_key(config["sheets"]["spreadsheet_id"])
    cobros = spreadsheet.worksheet("COBROS")
    revisiones = spreadsheet.worksheet("REVISIONES")
    cobros_values = cobros.get_all_values()
    rev_values = revisiones.get_all_values()

    sql = SQLServerConnector(config["database"])
    sql.connect()
    cursor = sql.connection.cursor()

    for dni in TARGET_DNIS:
        print(f"\n{'=' * 90}")
        print(f"DNI {dni}")

        print("\nDB commissions:")
        cursor.execute(
            """
            SELECT c.id_comision, c.id_curso, c.nombre, c.fecha_inicio,
                   cp.id_rol, cp.id_estado_academico, cp.id_estado_administrativo, cp.eliminado
            FROM COMISIONES_PERSONAS cp
            INNER JOIN COMISIONES c ON c.id_comision = cp.id_comision
            INNER JOIN PERSONAS p ON p.id_persona = cp.id_persona
            WHERE p.dni = ?
              AND YEAR(c.fecha_inicio) = 2026
            ORDER BY c.nombre
            """,
            (dni,),
        )
        for row in cursor.fetchall():
            print(
                f"  curso={row[1]} comision={row[2].strip()} inicio={row[3]} "
                f"rol={row[4]} acad={row[5]} admin={row[6]} eliminado={row[7]}"
            )

        print("\nDB payments:")
        cursor.execute(
            """
            SELECT p.id_pago_mp, p.fecha, p.monto, p.id_concepto_pago,
                   p.id_movimiento_bancario, p.controlado, p.controlado_auto, p.nro_operacion
            FROM PAGO_MERCADO_PAGO p
            INNER JOIN PERSONAS per ON p.id_persona = per.id_persona
            WHERE per.dni = ?
              AND YEAR(p.fecha) = 2026
            ORDER BY p.fecha, p.id_pago_mp
            """,
            (dni,),
        )
        for row in cursor.fetchall():
            print(
                f"  pago={row[0]} fecha={row[1]} monto=${row[2]:,.0f} concept={row[3]} "
                f"mov={row[4]} controlado={row[5]} auto={row[6]} op={row[7]}"
            )

        print("\nCOBROS rows:")
        for idx, row in enumerate(cobros_values[1:], start=2):
            if len(row) < 14:
                continue
            if (row[5] or "").strip() != dni:
                continue
            print(
                f"  row={idx} comision={(row[2] or '').strip()} fecha={(row[3] or '').strip()} "
                f"tipo={(row[4] or '').strip()} concepto={(row[6] or '').strip()} monto={(row[7] or '').strip()} "
                f"medio={(row[8] or '').strip()} mov={(row[12] or '').strip()} pago={(row[13] or '').strip()}"
            )

        print("\nREVISIONES rows:")
        found = False
        for idx, row in enumerate(rev_values[1:], start=2):
            if len(row) < 6:
                continue
            if (row[2] or "").strip() != dni:
                continue
            found = True
            print(
                f"  row={idx} case={(row[0] or '').strip()} comision={(row[1] or '').strip()} "
                f"problema={(row[3] or '').strip()} detalle={(row[4] or '').strip()} resol={(row[5] or '').strip()}"
            )
        if not found:
            print("  (none)")

    sql.connection.close()


if __name__ == "__main__":
    main()
