from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


DB_PATH = Path("data/context.db")


def main() -> None:
    run_id = sys.argv[1] if len(sys.argv) > 1 else ""
    if not run_id:
        print("usage: python scripts/inspeccionar_run.py <run_id>")
        raise SystemExit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    run = conn.execute("SELECT id, summary_json FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        print(f"run not found: {run_id}")
        raise SystemExit(1)

    summary = json.loads(run["summary_json"] or "{}")
    print(f"Run: {run_id}")
    print(json.dumps(summary.get("patch_summary"), indent=2, ensure_ascii=False))

    rows = conn.execute(
        """
        SELECT commission, dni, discrepancy_type, field, expected_value, actual_value,
               resolution, resolved_by, confidence
        FROM discrepancies
        WHERE run_id = ?
        ORDER BY commission, dni, field, id
        """,
        (run_id,),
    ).fetchall()

    print(f"\nDiscrepancies: {len(rows)}")
    for row in rows:
        print(
            f"  {row['dni']} | {row['discrepancy_type']} | {row['field'] or '-'} | "
            f"exp={str(row['expected_value'])[:40]} | act={str(row['actual_value'])[:40]} | "
            f"{row['resolution']} ({row['resolved_by']}) conf={row['confidence']}"
        )

    conn.close()


if __name__ == "__main__":
    main()
