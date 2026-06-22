"""Close guard:invalid_sequence reviews caused by the renumber bug (duplicate_cuota_N).

These duplicates were caused by the now-fixed renumber_allocations() bug that
reset cuota numbering to 1. The fix (commit ebd993d) prevents new duplicates,
but existing reviews for the old damage remain open. This script closes them.

Usage (from project root):
    # Dry run (default) — shows what would be closed
    .venv\Scripts\python.exe scripts\close_duplicate_cuota_reviews.py

    # Live — actually closes them
    .venv\Scripts\python.exe scripts\close_duplicate_cuota_reviews.py --live
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    live = "--live" in sys.argv
    db_path = Path("data/context.db")

    if not db_path.exists():
        print(f"[ERROR] DB not found: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Find all open guard:invalid_sequence reviews
    rows = conn.execute(
        "SELECT id, reason, context_json FROM pending_reviews "
        "WHERE status = 'open' AND reason = 'guard:invalid_sequence' "
        "ORDER BY id"
    ).fetchall()

    # Classify: only-duplicate vs mixed reasons
    only_duplicate: list[dict] = []
    mixed_with_duplicate: list[dict] = []
    no_duplicate: list[dict] = []

    for row in rows:
        ctx = json.loads(row["context_json"] or "{}")
        reasons = ctx.get("reasons") or []
        dup_reasons = [r for r in reasons if r.startswith("duplicate_cuota_")]
        non_dup_reasons = [r for r in reasons if not r.startswith("duplicate_cuota_")]

        entry = {
            "id": row["id"],
            "commission": ctx.get("commission", "?"),
            "dni": ctx.get("dni", "?"),
            "reasons": reasons,
            "dup_reasons": dup_reasons,
            "non_dup_reasons": non_dup_reasons,
        }

        if dup_reasons and not non_dup_reasons:
            only_duplicate.append(entry)
        elif dup_reasons and non_dup_reasons:
            mixed_with_duplicate.append(entry)
        else:
            no_duplicate.append(entry)

    print("=" * 70)
    print("DUPLICATE CUOTA REVIEWS ANALYSIS")
    print("=" * 70)
    print(f"Total guard:invalid_sequence open: {len(rows)}")
    print(f"  ONLY duplicate_cuota (safe to close):  {len(only_duplicate)}")
    print(f"  Mixed (duplicate + other issues):      {len(mixed_with_duplicate)}")
    print(f"  No duplicates (other issues only):     {len(no_duplicate)}")
    print()

    # Show what would be closed
    if only_duplicate:
        print(f"--- WILL CLOSE ({len(only_duplicate)} reviews) ---")
        by_commission: dict[str, list[dict]] = {}
        for entry in only_duplicate:
            by_commission.setdefault(entry["commission"], []).append(entry)
        for comm in sorted(by_commission):
            entries = by_commission[comm]
            print(f"  {comm}: {len(entries)} reviews")
            for e in entries[:3]:
                print(f"    REV-{e['id']} DNI {e['dni']} — {', '.join(e['dup_reasons'])}")
            if len(entries) > 3:
                print(f"    ... and {len(entries) - 3} more")
        print()

    # Show mixed (won't close, but informational)
    if mixed_with_duplicate:
        print(f"--- WILL NOT CLOSE (mixed reasons, {len(mixed_with_duplicate)}) ---")
        for e in mixed_with_duplicate[:10]:
            print(f"  REV-{e['id']} {e['commission']} DNI {e['dni']}")
            print(f"    dup: {e['dup_reasons']}")
            print(f"    other: {e['non_dup_reasons']}")
        if len(mixed_with_duplicate) > 10:
            print(f"  ... and {len(mixed_with_duplicate) - 10} more")
        print()

    if not only_duplicate:
        print("[INFO] Nothing to close.")
        conn.close()
        return

    if not live:
        print(f"[DRY RUN] Would close {len(only_duplicate)} reviews.")
        print("Run with --live to actually close them.")
        conn.close()
        return

    # Close them
    note = "Auto-closed: duplicate cuota caused by renumber bug (fixed in ebd993d)"
    closed = 0
    for entry in only_duplicate:
        conn.execute(
            "UPDATE pending_reviews SET status = 'resolved', "
            "reviewer_notes = ?, reviewed_at = datetime('now') "
            "WHERE id = ?",
            (note, entry["id"]),
        )
        closed += 1

    conn.commit()
    conn.close()

    print(f"[OK] Closed {closed} reviews.")
    print(f"[INFO] Re-run the reexport script to update the REVISIONES sheet.")


if __name__ == "__main__":
    main()
