from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector


TARGETS = {
    # DNI 38282178
    "REV-694": "Pago 79097 ya reflejado en PERITO-D-FEBRERO-2026; review en APICULTURA es falso positivo por multi-comision.",
    "REV-695": "Pago 79459 ya reflejado en PERITO-D-FEBRERO-2026; review en APICULTURA es falso positivo por multi-comision.",
    "REV-696": "Pago 81146 ya reflejado en PERITO-D-FEBRERO-2026; review en APICULTURA es falso positivo por multi-comision.",
    "REV-697": "Pago 84003 ya reflejado en PERITO-D-FEBRERO-2026; review en APICULTURA es falso positivo por multi-comision.",
    "REV-698": "Pago 85561 ya reflejado en PERITO-D-FEBRERO-2026; review en APICULTURA es falso positivo por multi-comision.",
    "REV-754": "Pago 79098 ya reflejado en APICULTURA-D-FEBRERO-2026; review en PERITO-D es falso positivo por multi-comision.",
}


def main() -> None:
    config = load_config("config/settings.yaml")
    sheets = SheetsConnector(config["sheets"])
    sheets.connect()
    spreadsheet = sheets._client.open_by_key(config["sheets"]["spreadsheet_id"])
    revisiones = spreadsheet.worksheet("REVISIONES")
    values = revisiones.get_all_values()

    rows_to_delete: list[tuple[int, str]] = []
    for idx, row in enumerate(values[1:], start=2):
        if not row:
            continue
        case_id = (row[0] or "").strip()
        if case_id in TARGETS:
            rows_to_delete.append((idx, case_id))

    print(f"Found {len(rows_to_delete)} target REVISIONES rows")
    for row_num, case_id in rows_to_delete:
        print(f"  row {row_num}: {case_id}")

    for row_num, case_id in sorted(rows_to_delete, reverse=True):
        revisiones.delete_rows(row_num)
        print(f"  deleted row {row_num}: {case_id}")
        time.sleep(1.2)

    conn = sqlite3.connect("data/context.db")
    for case_id, note in TARGETS.items():
        rev_id = int(case_id.replace("REV-", ""))
        conn.execute(
            "UPDATE pending_reviews SET status = 'resolved', reviewer_notes = ? WHERE id = ?",
            (note, rev_id),
        )
    conn.commit()
    conn.close()
    print("Marked target pending_reviews as resolved in SQLite")


if __name__ == "__main__":
    main()
