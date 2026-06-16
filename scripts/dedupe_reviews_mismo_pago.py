from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path


DB_PATH = Path("data/context.db")


def _reason_rank(reason: str) -> tuple[int, str]:
    order = {
        "ambiguous_allocation:flag_review": 0,
        "ambiguous_allocation:fix": 1,
        "unresolved_ambiguous_payment": 2,
        "pago_no_controlado": 3,
    }
    return (order.get(reason, 99), reason)


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, reason, context_json FROM pending_reviews WHERE status = 'open' ORDER BY id"
    ).fetchall()

    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        context = json.loads(row["context_json"] or "{}")
        commission = str(context.get("commission") or "").strip()
        dni = str(context.get("dni") or "").strip()
        payment_id = context.get("payment_id")
        if not commission or not dni or not payment_id:
            continue
        groups[(commission, dni, str(payment_id))].append(
            {
                "review_id": int(row["id"]),
                "reason": str(row["reason"]),
                "context": context,
            }
        )

    keep_ids: set[int] = set()
    delete_ids: set[int] = set()
    decisions: list[tuple[tuple[str, str, str], int, list[int]]] = []

    for key, items in groups.items():
        if len(items) <= 1:
            continue

        # Keep the "best" review for this exact payment in this exact commission.
        # Prefer explicit flag_review over fix over unresolved duplicate noise.
        # Tie-break by highest review_id (latest run).
        sorted_items = sorted(
            items,
            key=lambda item: (_reason_rank(item["reason"]), -item["review_id"]),
        )
        keep = sorted_items[0]
        keep_ids.add(keep["review_id"])
        dupes = [item["review_id"] for item in items if item["review_id"] != keep["review_id"]]
        delete_ids.update(dupes)
        decisions.append((key, keep["review_id"], dupes))

    print(f"Duplicate same-payment groups: {len(decisions)}")
    print(f"Keep: {len(keep_ids)} | Delete duplicates: {len(delete_ids)}")

    # Delete from REVISIONES sheet if present
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from main import load_config
    from src.connectors.sheets import SheetsConnector

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
        if not case_id.startswith("REV-"):
            continue
        try:
            review_id = int(case_id.replace("REV-", ""))
        except ValueError:
            continue
        if review_id in delete_ids:
            rows_to_delete.append((idx, case_id))

    print(f"REVISIONES rows to delete: {len(rows_to_delete)}")
    for row_num, case_id in sorted(rows_to_delete, reverse=True):
        revisiones.delete_rows(row_num)
        print(f"  deleted row {row_num}: {case_id}")
        time.sleep(1.1)

    # Resolve duplicate pending_reviews in SQLite
    for review_id in delete_ids:
        conn.execute(
            "UPDATE pending_reviews SET status = 'resolved', reviewer_notes = ? WHERE id = ?",
            ("Deduped duplicate review for same commission/DNI/payment_id; kept a single representative review.", review_id),
        )
    conn.commit()

    print("\nSample decisions:")
    for (commission, dni, payment_id), keep_id, dupes in decisions[:12]:
        print(
            f"  {commission[:35]} | DNI {dni} | payment_id={payment_id} | "
            f"keep REV-{keep_id} | delete {', '.join(f'REV-{d}' for d in dupes)}"
        )

    conn.close()


if __name__ == "__main__":
    main()
