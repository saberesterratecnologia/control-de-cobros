from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector


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
        commission = (row[1] or "").strip()
        dni = (row[2] or "").strip()
        problema = (row[3] or "").strip()
        detalle = (row[4] or "").strip()
        groups[(commission, dni, problema, detalle)].append((idx, case_id))

    dupes = [(key, rows) for key, rows in groups.items() if len(rows) > 1]
    dupes.sort(key=lambda item: (-len(item[1]), item[0][0], item[0][1]))

    total_rows = sum(len(rows) for _key, rows in dupes)
    total_delete = sum(len(rows) - 1 for _key, rows in dupes)
    print(f"Exact duplicate visible groups: {len(dupes)}")
    print(f"Rows inside duplicate groups: {total_rows}")
    print(f"Safe visible delete estimate: {total_delete}")

    for (commission, dni, problema, detalle), rows in dupes[:25]:
        ids = ", ".join(case_id for _idx, case_id in rows)
        print(f"\n  {commission[:35]} | DNI {dni} | {problema}")
        print(f"    {detalle[:100]}")
        print(f"    Rows: {ids}")


if __name__ == "__main__":
    main()
