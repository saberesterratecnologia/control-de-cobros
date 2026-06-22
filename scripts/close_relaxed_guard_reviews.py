"""Close guard:invalid_sequence reviews for reasons that were relaxed in ecf05f8.

These guards were removed from the pipeline because the allocation engine
handles the patterns correctly. Existing reviews for those patterns should
be closed so they don't pollute the REVISIONES sheet.

Closes reviews whose ONLY guard reasons are from the relaxed set.
Reviews with mixed reasons (relaxed + still-active) are NOT closed.

Usage (from project root):
    # Dry run (default)
    .venv\Scripts\python.exe scripts\close_relaxed_guard_reviews.py

    # Live
    .venv\Scripts\python.exe scripts\close_relaxed_guard_reviews.py --live
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RELAXED_REASONS = {
    "inscription_with_non_standard_amount",
    "cuota_1_matches_inscription_amount",
    "cuota_1_combines_inscription_and_cuota",
}


def main() -> None:
    live = "--live" in sys.argv
    db_path = Path("data/context.db")

    if not db_path.exists():
        print(f"[ERROR] DB not found: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, context_json FROM pending_reviews "
        "WHERE status = 'open' AND reason = 'guard:invalid_sequence' "
        "ORDER BY id"
    ).fetchall()

    to_close: list[dict] = []
    mixed: list[dict] = []

    for row in rows:
        ctx = json.loads(row["context_json"] or "{}")
        reasons = set(ctx.get("reasons") or [])

        # Normalize: duplicate_cuota_N -> duplicate_cuota
        normalized = set()
        for r in reasons:
            if r.startswith("duplicate_cuota_"):
                normalized.add(r)  # keep as-is, not relaxed
            elif r.startswith("missing_cuotas_before_"):
                normalized.add(r)  # keep as-is, not relaxed
            elif r.startswith("cuota_exceeds_total:"):
                normalized.add(r)  # keep as-is, not relaxed
            else:
                normalized.add(r)

        # Check if ALL reasons are in the relaxed set
        all_relaxed = all(r in RELAXED_REASONS for r in reasons)
        has_any_relaxed = any(r in RELAXED_REASONS for r in reasons)

        entry = {
            "id": row["id"],
            "commission": ctx.get("commission", "?"),
            "dni": ctx.get("dni", "?"),
            "reasons": list(reasons),
        }

        if all_relaxed and reasons:
            to_close.append(entry)
        elif has_any_relaxed:
            mixed.append(entry)

    print("=" * 70)
    print("RELAXED GUARD REVIEWS ANALYSIS")
    print("=" * 70)
    print(f"Total open guard:invalid_sequence: {len(rows)}")
    print(f"  All reasons relaxed (safe to close): {len(to_close)}")
    print(f"  Mixed (relaxed + active, NOT closing): {len(mixed)}")
    print()

    # Count by reason
    reason_counts: dict[str, int] = {}
    for entry in to_close:
        for r in entry["reasons"]:
            reason_counts[r] = reason_counts.get(r, 0) + 1
    if reason_counts:
        print("Reasons being closed:")
        for r, c in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"  {r}: {c}")
        print()

    if not to_close:
        print("[INFO] Nothing to close.")
        conn.close()
        return

    if not live:
        print(f"[DRY RUN] Would close {len(to_close)} reviews.")
        print("Run with --live to actually close them.")
        conn.close()
        return

    note = "Auto-closed: guard reason relaxed in ecf05f8"
    closed = 0
    for entry in to_close:
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
    print("[INFO] Run scripts/reexport_revisiones.py to update the sheet, or")
    print("       leave the sheet empty and let tomorrow's live run fill it.")


if __name__ == "__main__":
    main()
