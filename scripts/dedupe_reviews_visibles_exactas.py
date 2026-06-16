from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector


def _case_id_to_int(case_id: str) -> int:
    return int(case_id.replace("REV-", ""))


def main() -> None:
    config = load_config("config/settings.yaml")
    sheets = SheetsConnector(config["sheets"])
    sheets.connect()
    spreadsheet = sheets._client.open_by_key(config["sheets"]["spreadsheet_id"])
    revisiones = spreadsheet.worksheet("REVISIONES")
    values = revisiones.get_all_values()

    groups: dict[tuple[str, str, str, str], list[tuple[int, str]]] = defaultdict(list)
    for idx, row in enumerate(values[1:], start=2):
        if len(row) < 6:
            continue
        resolucion = (row[5] or "").strip()
        if resolucion:
            continue
        case_id = (row[0] or "").strip()
        if not case_id.startswith("REV-"):
            continue
        commission = (row[1] or "").strip()
        dni = (row[2] or "").strip()
        problema = (row[3] or "").strip()
        detalle = (row[4] or "").strip()
        groups[(commission, dni, problema, detalle)].append((idx, case_id))

    rows_to_delete: list[tuple[int, str]] = []
    review_ids_to_resolve: list[int] = []

    for _key, rows in groups.items():
        if len(rows) <= 1:
            continue
        # Keep the latest REV id, delete the older duplicates.
        sorted_rows = sorted(rows, key=lambda item: _case_id_to_int(item[1]))
        keep = sorted_rows[-1]
        dupes = sorted_rows[:-1]
        for row_num, case_id in dupes:
            rows_to_delete.append((row_num, case_id))
            review_ids_to_resolve.append(_case_id_to_int(case_id))
        print(
            f"Keeping {keep[1]} | deleting {', '.join(case_id for _row_num, case_id in dupes)}"
        )

    print(f"\nDeleting {len(rows_to_delete)} duplicate rows from REVISIONES")
    for row_num, case_id in sorted(rows_to_delete, reverse=True):
        revisiones.delete_rows(row_num)
        print(f"  deleted row {row_num}: {case_id}")
        time.sleep(1.0)

    conn = sqlite3.connect("data/context.db")
    for review_id in review_ids_to_resolve:
        conn.execute(
            "UPDATE pending_reviews SET status = 'resolved', reviewer_notes = ? WHERE id = ?",
            ("Deduped exact duplicate visible review; kept a single representative row in REVISIONES.", review_id),
        )
    conn.commit()
    conn.close()

    print(f"Marked {len(review_ids_to_resolve)} pending_reviews as resolved in SQLite")


if __name__ == "__main__":
    main()
