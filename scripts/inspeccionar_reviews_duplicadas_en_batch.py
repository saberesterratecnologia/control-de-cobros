from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector
from src.context.context_manager import ContextManager
from src.reviews.review_manager import ReviewManager


DB_PATH = Path("data/context.db")


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

    groups: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, str] = {}

    for review in review_rows:
        context_json = json.loads(review["context_json"] or "{}")
        commission = str(context_json.get("commission") or "").strip()
        dni = str(context_json.get("dni") or "").strip()
        if not commission or not dni:
            continue
        problema, detalle = review_manager.build_problem_summary(review["reason"], context_json)
        key = review_manager._dedup_key(commission, dni, problema, detalle)  # noqa: SLF001
        groups[key].append(f"REV-{review['id']}")
        labels[key] = f"{commission} | DNI {dni} | {problema} | {detalle}"

    dup_groups = [(key, ids) for key, ids in groups.items() if len(ids) > 1]
    print(f"Duplicate content groups inside skipped batch: {len(dup_groups)}")
    for key, ids in dup_groups:
        print(f"  - {labels[key]}")
        print(f"    Reviews: {', '.join(ids)}")


if __name__ == "__main__":
    main()
