from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


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

    selected_runs: list[sqlite3.Row] = []
    for run in runs:
        summary = _load_summary(run["summary_json"])
        reviews_export = summary.get("reviews_export") or {}
        if reviews_export.get("skipped_by_flag") and summary.get("pending_review", 0) > 0:
            selected_runs.append(run)

    if not selected_runs:
        print("No encontré corridas live recientes con reviews no exportadas por flag.")
        conn.close()
        return

    print("Corridas live recientes con reviews no exportadas por skip_reviews=True")
    print("=" * 90)

    total_open = 0
    all_commission_counts: Counter[str] = Counter()
    all_reason_counts: Counter[str] = Counter()

    for run in selected_runs:
        run_id = run["id"]
        summary = _load_summary(run["summary_json"])
        open_reviews = conn.execute(
            "SELECT id, reason, context_json FROM pending_reviews WHERE run_id = ? AND status = 'open' ORDER BY id",
            (run_id,),
        ).fetchall()

        total_open += len(open_reviews)
        commission_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        samples_by_reason: dict[str, list[str]] = defaultdict(list)

        for review in open_reviews:
            context = json.loads(review["context_json"] or "{}")
            commission = str(context.get("commission") or "?").strip()
            dni = str(context.get("dni") or "?").strip()
            reason = str(review["reason"] or "?")

            commission_counts[commission] += 1
            reason_counts[reason] += 1
            all_commission_counts[commission] += 1
            all_reason_counts[reason] += 1

            if len(samples_by_reason[reason]) < 3:
                samples_by_reason[reason].append(f"REV-{review['id']} | {commission} | DNI {dni}")

        print(f"\nRun {run_id}")
        print(f"  Started: {run['started_at']}")
        print(f"  pending_review summary: {summary.get('pending_review', 0)}")
        print(f"  reviews_export: {summary.get('reviews_export')}")
        print(f"  Open reviews now: {len(open_reviews)}")

        if commission_counts:
            print("  Por comisión:")
            for commission, count in commission_counts.most_common():
                print(f"    {count:>3} | {commission}")

        if reason_counts:
            print("  Por motivo:")
            for reason, count in reason_counts.most_common():
                print(f"    {count:>3} | {reason}")
                for sample in samples_by_reason[reason]:
                    print(f"          - {sample}")

    print("\n" + "=" * 90)
    print(f"Total open reviews across selected runs: {total_open}")
    print("Top commissions:")
    for commission, count in all_commission_counts.most_common(12):
        print(f"  {count:>3} | {commission}")
    print("Top reasons:")
    for reason, count in all_reason_counts.most_common(12):
        print(f"  {count:>3} | {reason}")

    conn.close()


if __name__ == "__main__":
    main()
