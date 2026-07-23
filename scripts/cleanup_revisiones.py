"""One-time cleanup: remove non-actionable rows from REVISIONES sheet.

Removes:
  1. All seminario/event reviews (single-day commissions filtered at SQL level now)
  2. All non-blocking guard reviews that should be in LIMPIEZA_HOJA
  3. Generic "Requiere revisión" bucket entries

Also closes matching pending_reviews in context.db.

Usage:
    python scripts/cleanup_revisiones.py --dry-run   # preview only
    python scripts/cleanup_revisiones.py --live       # actually delete
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import gspread  # noqa: E402
import os  # noqa: E402
from google.oauth2.service_account import Credentials  # noqa: E402

# Categories to remove from REVISIONES (non-actionable)
REMOVE_CATEGORIES = {
    "Cuota duplicada",
    "Falta inscripción",
    "Cuotas faltantes",
    "Inscripción con monto irregular",
    "Cuota 1 con monto de inscripción",
    "Cuota 1 combina inscripción + cuota",
    "Requiere revisión",
    "Fecha faltante",
}

SEMINARIO_KEYWORDS = {"SEMINARIO", "JORNADA", "LANZAMIENTO", "CAPACITACIÓN"}


def is_seminario_or_event(commission_name: str) -> bool:
    upper = commission_name.upper()
    return any(kw in upper for kw in SEMINARIO_KEYWORDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup REVISIONES sheet")
    parser.add_argument("--live", action="store_true", help="Actually delete rows")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    if not args.live and not args.dry_run:
        parser.error("specify --dry-run or --live")

    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"=== Cleanup REVISIONES [{mode}] ===\n")

    # Connect to sheet
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_CREDENTIALS_PATH"),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(os.getenv("SPREADSHEET_ID"))
    ws = sh.worksheet("REVISIONES")

    all_rows = ws.get_all_values()
    header = all_rows[0] if all_rows else []
    data = all_rows[1:]

    rows_to_delete: list[int] = []  # 1-based sheet row numbers
    case_ids_to_close: list[str] = []

    by_reason: dict[str, int] = {}

    for idx, row in enumerate(data, start=2):  # row 2 is first data row
        if len(row) < 5:
            continue

        case_id = (row[0] or "").strip()
        commission = (row[1] or "").strip()
        problema = (row[3] or "").strip()

        should_remove = False
        reason = ""

        # 1. Seminario/event commission
        if is_seminario_or_event(commission):
            should_remove = True
            reason = f"event:{commission[:30]}"

        # 2. Non-actionable guard category
        elif problema in REMOVE_CATEGORIES:
            should_remove = True
            reason = f"category:{problema}"

        if should_remove:
            rows_to_delete.append(idx)
            by_reason[reason] = by_reason.get(reason, 0) + 1
            if case_id:
                case_ids_to_close.append(case_id)

    # Summary
    total_remove = len(rows_to_delete)
    total_keep = len(data) - total_remove
    print(f"Total rows in REVISIONES: {len(data)}")
    print(f"Rows to REMOVE: {total_remove}")
    print(f"Rows to KEEP: {total_keep}")
    print()

    # Breakdown
    print("Removal breakdown:")
    for reason, count in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}")
    print()

    if not args.live:
        print("[DRY-RUN] No changes made. Run with --live to apply.")
        return

    # Close in context.db
    db_path = ROOT / "data" / "context.db"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()
        closed = 0
        for case_id in case_ids_to_close:
            # REV-123 format
            match = re.match(r"^REV-(\d+)$", case_id)
            if match:
                review_id = int(match.group(1))
                c.execute(
                    "UPDATE pending_reviews SET status='resolved', reviewer_notes='auto_close:reclassified_to_cleanup' WHERE id=? AND status='open'",
                    (review_id,),
                )
                if c.rowcount > 0:
                    closed += 1
            # GRP-123-WF format
            grp_match = re.match(r"^GRP-(\d+)-WF$", case_id)
            if grp_match:
                payment_id = int(grp_match.group(1))
                c.execute(
                    "UPDATE pending_reviews SET status='resolved', reviewer_notes='auto_close:reclassified_to_cleanup' WHERE status='open' AND context_json LIKE ?",
                    (f'%"payment_id": {payment_id}%',),
                )
                closed += c.rowcount
        conn.commit()
        print(f"Closed {closed} pending reviews in context.db")
        conn.close()

    # Rewrite the sheet: clear all data rows and write back only the kept ones.
    # This is far more efficient than deleting 2000+ rows one by one.
    rows_to_delete_set = set(rows_to_delete)
    kept_rows = [
        row for idx, row in enumerate(data, start=2)
        if idx not in rows_to_delete_set
    ]

    print(f"Rewriting REVISIONES: clearing {len(data)} rows, writing {len(kept_rows)} back...")

    # Clear all data rows (keep header)
    if len(data) > 0:
        last_row = len(data) + 1  # +1 for header
        ws.batch_clear([f"A2:F{last_row}"])

    # Write kept rows back
    if kept_rows:
        ws.update(range_name=f"A2:F{len(kept_rows) + 1}", values=kept_rows)

    print(f"\nDone. REVISIONES now has {len(kept_rows)} actionable rows.")


if __name__ == "__main__":
    main()
