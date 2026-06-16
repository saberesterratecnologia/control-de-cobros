from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector


TARGET = "REV-779"
NOTE = "Pago 84756 ya reflejado en PERITO-D-ABRIL-2026; review en PRODUCCION EXTENSIVA es falso positivo por multi-comision y mismatch de precios."


def main() -> None:
    config = load_config("config/settings.yaml")
    sheets = SheetsConnector(config["sheets"])
    sheets.connect()
    spreadsheet = sheets._client.open_by_key(config["sheets"]["spreadsheet_id"])
    revisiones = spreadsheet.worksheet("REVISIONES")
    values = revisiones.get_all_values()

    target_row = None
    for idx, row in enumerate(values[1:], start=2):
        if not row:
            continue
        if (row[0] or "").strip() == TARGET:
            target_row = idx
            break

    if target_row is not None:
        revisiones.delete_rows(target_row)
        print(f"Deleted REVISIONES row {target_row}: {TARGET}")
        time.sleep(1.2)
    else:
        print(f"{TARGET} not found in REVISIONES")

    conn = sqlite3.connect("data/context.db")
    conn.execute(
        "UPDATE pending_reviews SET status = 'resolved', reviewer_notes = ? WHERE id = 779",
        (NOTE,),
    )
    conn.commit()
    conn.close()
    print(f"Marked {TARGET} as resolved in SQLite")


if __name__ == "__main__":
    main()
