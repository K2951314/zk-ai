"""Backfill equivalent cost on historical usage rows.

Cost is computed at *record* time from each deployment's list price, so usage
written before prices existed sits at ``cost_usd = 0``. This script recomputes
those rows from their token counts and the current ``models.yaml`` prices, so the
console's "7 天等价成本" reflects real spend-to-date instead of a false zero.

Safe by design:
  * dry-run by default; pass ``--apply`` to write.
  * only touches rows whose ``cost_usd`` is 0/NULL (idempotent - rerun freely).
  * prices a row by the deployment that actually served it (model + provider),
    falling back to the model's first priced deployment.
  * never rewrites the request history or attempts, only the usage tally.

Usage:
    python scripts/backfill_cost.py            # preview
    python scripts/backfill_cost.py --apply    # write
    python scripts/backfill_cost.py --apply --include-paid   # also reprice >0 rows
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Ensure the process env doesn't shadow the .env secrets used to locate the DB.
os.environ.setdefault("PYTHONUTF8", "1")

from app.core.config import Settings, load_app_config


def build_price_map(config) -> dict[tuple[str, str], tuple[float, float]]:
    """(model_id, provider_id) -> (input_cost_per_mtok, output_cost_per_mtok)."""
    prices: dict[tuple[str, str], tuple[float, float]] = {}
    fallback: dict[str, tuple[float, float]] = {}
    for model in config.models.values():
        for dep in model.deployments:
            pair = (dep.input_cost_per_mtok, dep.output_cost_per_mtok)
            prices[(model.id, dep.provider_id)] = pair
            if model.id not in fallback and (pair[0] or pair[1]):
                fallback[model.id] = pair
    return prices, fallback  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill usage cost from list prices.")
    parser.add_argument("--apply", action="store_true", help="write changes (default: preview)")
    parser.add_argument("--include-paid", action="store_true",
                        help="also reprice rows that already have cost_usd > 0")
    args = parser.parse_args()

    settings = Settings()
    config = load_app_config(settings)
    prices, fallback = build_price_map(config)

    db_path = Path("data/zkai.db")
    if not db_path.is_file():
        print(f"[x] 数据库不存在：{db_path.resolve()}")
        return 1

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    cur = con.cursor()
    # Parameterized (no string-built SQL): `? = 1` reprice-all switch, else only
    # zero-cost rows — keeps the scan idempotent and injection-free.
    rows = cur.execute(
        "SELECT rowid, model, provider_id, input_tokens, output_tokens, cost_usd "
        "FROM usage_records WHERE ? = 1 OR cost_usd IS NULL OR cost_usd = 0",
        (1 if args.include_paid else 0,),
    ).fetchall()

    updated = 0
    total_before = total_after = 0.0
    for rowid, model, provider_id, tin, tout, old in rows:
        pair = prices.get((model, provider_id)) or fallback.get(model) or (0.0, 0.0)
        new = round(((tin or 0) * pair[0] + (tout or 0) * pair[1]) / 1_000_000, 8)
        total_before += old or 0.0
        total_after += new
        if abs(new - (old or 0.0)) > 1e-12:
            updated += 1
            if args.apply:
                cur.execute("UPDATE usage_records SET cost_usd = ? WHERE rowid = ?", (new, rowid))
        if updated and updated % 500 == 0:
            print(f"  ...已处理 {updated} 行")

    if args.apply:
        con.commit()
    con.close()

    verb = "已回填" if args.apply else "预览（未写入）"
    print(
        f"[{verb}] 扫描 {len(rows)} 行，"
        f"{'将' if not args.apply else '已'}更新 {updated} 行；"
        f"成本合计 ${total_before:.4f} → ${total_after:.4f}（≈¥{total_after * 7.2:.2f}）"
    )
    if not args.apply and updated:
        print("    加 --apply 落库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
