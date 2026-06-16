from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector
from src.context.context_manager import ContextManager
from src.reviews.review_manager import ReviewManager


DB_PATH = Path("data/context.db")
EXPORTED_COUNT = 124


def _load_summary(text: str | None) -> dict:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    runs = conn.execute(
        """
        SELECT id, summary_json
        FROM runs
        WHERE mode = 'live'
        ORDER BY started_at DESC
        LIMIT 60
        """
    ).fetchall()

    selected_run_ids: list[str] = []
    for run in runs:
        summary = _load_summary(run["summary_json"])
        reviews_export = summary.get("reviews_export") or {}
        if reviews_export.get("skipped_by_flag") and summary.get("pending_review", 0) > 0:
            selected_run_ids.append(str(run["id"]))

    review_rows = conn.execute(
        f"""
        SELECT id, run_id, reason, context_json
        FROM pending_reviews
        WHERE status = 'open'
          AND run_id IN ({','.join('?' for _ in selected_run_ids)})
        ORDER BY id
        """,
        selected_run_ids,
    ).fetchall()
    conn.close()

    config = load_config("config/settings.yaml")
    sheets = SheetsConnector(config["sheets"])
    context = ContextManager(str(DB_PATH))
    review_manager = ReviewManager(sheets, context, config)

    with context, sheets.connect():
        worksheet = review_manager._get_revisiones_sheet()  # noqa: SLF001
        values = worksheet.get_all_values() if worksheet is not None else []

    case_id_to_row: dict[str, int] = {}
    content_key_to_row: dict[str, int] = {}
    for idx, row in enumerate(values[1:], start=2):
        if not row:
            continue
        if len(row) >= 1 and row[0].strip():
            case_id_to_row[row[0].strip()] = idx
        if len(row) >= 5:
            key = review_manager._dedup_key(row[1], row[2], row[3], row[4])  # noqa: SLF001
            content_key_to_row[key] = idx

    appended_threshold = max(2, len(values) - EXPORTED_COUNT + 1)

    print(f"Total rows in REVISIONES (incl header): {len(values)}")
    print(f"Assuming last {EXPORTED_COUNT} were appended in export, threshold row is: {appended_threshold}")
    print()

    content_deduped: list[str] = []
    case_id_preexisting: list[str] = []

    for review in review_rows:
        review_id = int(review["id"])
        case_id = f"REV-{review_id}"
        context_json = json.loads(review["context_json"] or "{}")
        commission = str(context_json.get("commission") or "").strip()
        dni = str(context_json.get("dni") or "").strip()
        if not commission or not dni:
            continue

        problema, detalle = review_manager.build_problem_summary(review["reason"], context_json)
        content_key = review_manager._dedup_key(commission, dni, problema, detalle)  # noqa: SLF001

        row_num = case_id_to_row.get(case_id)
        if row_num is None:
            # No exact case_id in sheet -> must have been content-deduped
            matched_row = content_key_to_row.get(content_key)
            content_deduped.append(
                f"{case_id} | {commission} | DNI {dni} | row_match={matched_row} | {problema}"
            )
            continue

        if row_num < appended_threshold:
            # Exact case_id exists above the appended block -> preexisting
            case_id_preexisting.append(
                f"{case_id} | {commission} | DNI {dni} | existing_row={row_num} | {problema}"
            )

    print("Likely content-deduped (case_id missing, content already existed):")
    if content_deduped:
        for item in content_deduped:
            print(f"  - {item}")
    else:
        print("  (none)")

    print("\nLikely exact-case-id deduped (case_id already existed before append):")
    if case_id_preexisting:
        for item in case_id_preexisting:
            print(f"  - {item}")
    else:
        print("  (none)")


if __name__ == "__main__":
    main()
