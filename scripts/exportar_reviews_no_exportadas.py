from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import sys

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
        SELECT id, started_at, summary_json
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

    if not selected_run_ids:
        print("No hay corridas live recientes con reviews no exportadas por flag.")
        conn.close()
        return

    review_rows = conn.execute(
        f"""
        SELECT id, run_id, reason, context_json, status
        FROM pending_reviews
        WHERE status = 'open'
          AND run_id IN ({','.join('?' for _ in selected_run_ids)})
        ORDER BY id
        """,
        selected_run_ids,
    ).fetchall()
    conn.close()

    actionable_reviews: list[dict] = []
    skipped_missing_context: list[str] = []

    for review in review_rows:
        context = json.loads(review["context_json"] or "{}")
        commission = str(context.get("commission") or "").strip()
        dni = str(context.get("dni") or "").strip()
        if not commission or not dni:
            skipped_missing_context.append(f"REV-{review['id']} ({review['reason']})")
            continue
        actionable_reviews.append(
            {
                "id": int(review["id"]),
                "run_id": str(review["run_id"]),
                "reason": str(review["reason"]),
                "context": context,
            }
        )

    print(f"Runs seleccionados: {len(selected_run_ids)}")
    print(f"Open reviews totales: {len(review_rows)}")
    print(f"Accionables para exportar: {len(actionable_reviews)}")
    print(f"Omitidas por falta de comision/dni: {len(skipped_missing_context)}")
    if skipped_missing_context:
        for item in skipped_missing_context:
            print(f"  - {item}")

    config = load_config("config/settings.yaml")
    sheets = SheetsConnector(config["sheets"])
    context = ContextManager(str(DB_PATH))
    review_manager = ReviewManager(sheets, context, config)

    with context, sheets.connect():
        worksheet = review_manager._get_revisiones_sheet()  # noqa: SLF001
        if worksheet is None:
            print("No se pudo acceder a REVISIONES.")
            return

        existing_case_ids = set()
        existing_content_keys: set[str] = set()
        all_values = worksheet.get_all_values()
        for row in all_values[1:]:
            if not row:
                continue
            if row[0].strip():
                existing_case_ids.add(row[0].strip())
            if len(row) >= 5:
                existing_content_keys.add(
                    review_manager._dedup_key(row[1], row[2], row[3], row[4])  # noqa: SLF001
                )

        rows_to_append: list[list[str]] = []
        skipped_existing = 0
        for review in actionable_reviews:
            case_id = f"REV-{review['id']}"
            if case_id in existing_case_ids:
                skipped_existing += 1
                continue

            problema, detalle = review_manager.build_problem_summary(
                review["reason"], review["context"],
            )
            comision = str(review["context"].get("commission") or "").strip()
            dni = str(review["context"].get("dni") or "").strip()
            content_key = review_manager._dedup_key(comision, dni, problema, detalle)  # noqa: SLF001
            if content_key in existing_content_keys:
                skipped_existing += 1
                continue
            existing_content_keys.add(content_key)
            rows_to_append.append([case_id, comision, dni, problema, detalle, ""])

        if rows_to_append:
            worksheet.append_rows(rows_to_append, value_input_option="RAW")

        print(f"Exportadas: {len(rows_to_append)}")
        print(f"Saltadas por dedup/case_id existente: {skipped_existing}")


if __name__ == "__main__":
    main()
