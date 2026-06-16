from __future__ import annotations

import json
import sqlite3
from pathlib import Path


DB_PATH = Path("data/context.db")
TARGET_IDS = [712, 771, 755, 756, 760, 770]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, run_id, reason, context_json, discrepancy_id, status FROM pending_reviews WHERE id IN (%s) ORDER BY id"
        % ",".join("?" for _ in TARGET_IDS),
        TARGET_IDS,
    ).fetchall()

    for row in rows:
        context = json.loads(row["context_json"] or "{}")
        print(f"REV-{row['id']}")
        print(f"  run_id: {row['run_id']}")
        print(f"  reason: {row['reason']}")
        print(f"  discrepancy_id: {row['discrepancy_id']}")
        print(f"  status: {row['status']}")
        print(f"  context: {json.dumps(context, ensure_ascii=False, indent=2)}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
