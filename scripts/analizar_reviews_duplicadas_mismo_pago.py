from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


DB_PATH = Path("data/context.db")


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, reason, context_json FROM pending_reviews WHERE status = 'open' ORDER BY id"
    ).fetchall()

    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        context = json.loads(row["context_json"] or "{}")
        commission = str(context.get("commission") or "").strip()
        dni = str(context.get("dni") or "").strip()
        payment_id = context.get("payment_id")
        if not commission or not dni or not payment_id:
            continue
        key = (commission, dni, str(payment_id))
        groups[key].append(
            {
                "review_id": int(row["id"]),
                "reason": str(row["reason"]),
                "monto": str(context.get("monto") or ""),
                "fecha": str(context.get("fecha") or ""),
            }
        )

    dup_groups = [(key, items) for key, items in groups.items() if len(items) > 1]
    dup_groups.sort(key=lambda item: (-len(item[1]), item[0][0], item[0][1], item[0][2]))

    total_reviews = sum(len(items) for _key, items in dup_groups)
    total_keep = len(dup_groups)
    total_delete = total_reviews - total_keep

    print(f"Duplicate same-payment groups: {len(dup_groups)}")
    print(f"Reviews inside those groups: {total_reviews}")
    print(f"Safe cleanup estimate: keep {total_keep}, delete {total_delete}")

    reason_pairs = Counter()
    for _key, items in dup_groups:
        for item in items:
            reason_pairs[item["reason"]] += 1

    print("\nTop reasons inside duplicate groups:")
    for reason, count in reason_pairs.most_common(10):
        print(f"  {count:>3} | {reason}")

    print("\nSample groups:")
    for (commission, dni, payment_id), items in dup_groups[:20]:
        print(f"\n  {commission} | DNI {dni} | payment_id={payment_id} | {len(items)} reviews")
        for item in items:
            print(
                f"    REV-{item['review_id']} | reason={item['reason']} | "
                f"monto={item['monto']} | fecha={item['fecha']}"
            )

    conn.close()


if __name__ == "__main__":
    main()
