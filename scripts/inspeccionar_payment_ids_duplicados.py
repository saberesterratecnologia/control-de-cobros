from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path


DB_PATH = Path("data/context.db")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT id, run_id, reason, context_json
        FROM pending_reviews
        WHERE status = 'open'
        ORDER BY id
        """
    ).fetchall()

    by_payment: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        context = json.loads(row["context_json"] or "{}")
        payment_id = context.get("payment_id")
        if not payment_id:
            continue
        by_payment[str(payment_id)].append(
            {
                "review_id": int(row["id"]),
                "reason": str(row["reason"]),
                "commission": str(context.get("commission") or "").strip(),
                "dni": str(context.get("dni") or "").strip(),
                "monto": str(context.get("monto") or ""),
                "fecha": str(context.get("fecha") or ""),
            }
        )

    duplicates = [(pid, items) for pid, items in by_payment.items() if len(items) > 1]
    duplicates.sort(key=lambda item: (-len(item[1]), item[0]))

    print(f"payment_id appearing in multiple open reviews: {len(duplicates)}")
    for payment_id, items in duplicates[:40]:
        print(f"\npayment_id={payment_id} | {len(items)} reviews")
        for item in items:
            print(
                f"  REV-{item['review_id']} | {item['commission']} | DNI {item['dni']} | "
                f"reason={item['reason']} | monto={item['monto']} | fecha={item['fecha']}"
            )

    conn.close()


if __name__ == "__main__":
    main()
