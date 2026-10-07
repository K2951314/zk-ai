"""UsageRepository: token / cost accounting.

Records per-request usage rows and aggregates them into the summary / daily /
by-alias / backfill-cost views consumed by the console and KPI endpoints.
"""

from __future__ import annotations

import datetime as dt

from app.database.repositories._common import (
    Any,
    Database,
    RequestRecord,
    UsageRecord,
    func,
    logger,
    select,
    utcnow,
)


class UsageRepository:
    """Token / cost accounting."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(
        self,
        *,
        request_id: str | None,
        provider_id: str,
        model: str,
        credential_id: str | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                UsageRecord(
                    request_id=request_id,
                    provider_id=provider_id,
                    model=model,
                    credential_id=credential_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                )
            )

    async def backfill_costs(
        self,
        prices: dict[tuple[str, str], tuple[float, float]],
        fallback: dict[str, tuple[float, float]],
    ) -> dict[str, Any]:
        """Reprice rows with ``cost_usd`` 0/NULL from (model, provider) prices.

        Mirrors ``scripts/backfill_cost.py`` so the console can do it with a
        button. ``prices`` keys are (model_id, provider_id); ``fallback`` keys
        are model_id (first priced deployment). Only zero rows are touched.
        """
        updated = 0
        total = 0.0
        async with self.db.session() as session:
            result = await session.execute(
                select(UsageRecord).where(
                    (UsageRecord.cost_usd.is_(None)) | (UsageRecord.cost_usd == 0)
                )
            )
            for row in result.scalars():
                pair = prices.get((row.model, row.provider_id)) or fallback.get(row.model)
                if pair is None or not (pair[0] or pair[1]):
                    continue
                cost = round(
                    (row.input_tokens * pair[0] + row.output_tokens * pair[1]) / 1_000_000, 8
                )
                if abs(cost) > 1e-12:
                    row.cost_usd = cost
                    updated += 1
                    total += cost
        if updated:
            logger.info("backfilled cost on %d usage row(s), total $%.4f", updated, total)
        return {"updated": updated, "total_cost_usd": round(total, 6)}

    async def summary(self, *, days: int = 7) -> dict[str, Any]:
        cutoff = utcnow() - dt.timedelta(days=days)
        async with self.db.session() as session:
            totals = await session.execute(
                select(
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.input_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.output_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
                ).where(UsageRecord.created_at >= cutoff)
            )
            count, input_tokens, output_tokens, total_tokens, cost = totals.one()

            by_provider = await session.execute(
                select(
                    UsageRecord.provider_id,
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
                )
                .where(UsageRecord.created_at >= cutoff)
                .group_by(UsageRecord.provider_id)
            )
            by_model = await session.execute(
                select(
                    UsageRecord.model,
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
                )
                .where(UsageRecord.created_at >= cutoff)
                .group_by(UsageRecord.model)
                .order_by(func.count(UsageRecord.id).desc())
                .limit(20)
            )
            by_credential = await session.execute(
                select(
                    UsageRecord.credential_id,
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
                )
                .where(UsageRecord.created_at >= cutoff)
                .group_by(UsageRecord.credential_id)
            )

            # 按别名聚合：这是「客户端配了哪条链」的唯一口径，也是定位浪费的入口。
            # 2026-09-26 实测：zk-k3 上 2,956 条请求平均 input 228,745、output 仅 357
            # （640:1），吃掉全站 9.44 亿 input 的 72%——而这个维度此前在控制台看不到，
            # 只能靠 SQL 手工查，所以浪费长期隐形。
            # input/output 比一起查：比高得离谱就说明「每轮都在重读历史」。
            by_alias = await session.execute(
                select(
                    RequestRecord.alias,
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.input_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.output_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
                )
                .join(RequestRecord, RequestRecord.id == UsageRecord.request_id)
                .where(UsageRecord.created_at >= cutoff)
                .group_by(RequestRecord.alias)
                .order_by(func.coalesce(func.sum(UsageRecord.input_tokens), 0).desc())
            )
            return {
                "window_days": days,
                "requests": int(count or 0),
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
                "total_tokens": int(total_tokens or 0),
                "cost_usd": round(float(cost or 0.0), 6),
                "by_provider": [
                    {
                        "provider": row[0],
                        "requests": int(row[1]),
                        "tokens": int(row[2]),
                        "cost_usd": round(float(row[3]), 6),
                    }
                    for row in by_provider
                ],
                "top_models": [
                    {
                        "model": row[0],
                        "requests": int(row[1]),
                        "tokens": int(row[2]),
                        "cost_usd": round(float(row[3]), 6),
                    }
                    for row in by_model
                ],
                "by_credential": [
                    {
                        "credential_id": row[0],
                        "requests": int(row[1]),
                        "tokens": int(row[2]),
                        "cost_usd": round(float(row[3]), 6),
                    }
                    for row in by_credential
                ],
                # ``io_ratio`` = input/output。全站基线约 132:1（2026-09-26 实测），
                # 某一条显著高于它就是在过度重读历史——那是路由配置问题，
                # 不是「这个客户端话多」。前端按 >200:1 标红。
                "by_alias": [
                    {
                        "alias": row[0] or "（无别名）",
                        "requests": int(row[1]),
                        "input_tokens": int(row[2]),
                        "output_tokens": int(row[3]),
                        "cost_usd": round(float(row[4]), 6),
                        "io_ratio": round(int(row[2]) / max(1, int(row[3])), 1),
                    }
                    for row in by_alias
                ],
            }

    async def daily(self, *, days: int = 14) -> list[dict[str, Any]]:
        cutoff = utcnow() - dt.timedelta(days=days)
        async with self.db.session() as session:
            result = await session.execute(
                select(
                    func.date(UsageRecord.created_at).label("day"),
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                )
                .where(UsageRecord.created_at >= cutoff)
                .group_by("day")
                .order_by("day")
            )
            return [
                {"day": str(row[0]), "requests": int(row[1]), "tokens": int(row[2])}
                for row in result
            ]
