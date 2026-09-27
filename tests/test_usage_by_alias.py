"""按别名聚合的用量统计（控制台「按别名」表的后端契约）。

2026-09-26 加这个维度的原因：全站 9.44 亿 input tokens 里有 72% 压在
``zk-k3`` 一条链上（2,956 条请求、平均 input 228,745、output 仅 357，
io 比 640:1），而这个事实此前**在控制台看不到**——只能靠 SQL 手工查，
所以浪费长期隐形。按别名聚合就是让「客户端选了哪条链」变成一屏可见。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from app.database.db import Database
from app.database.models import RequestRecord, UsageRecord
from app.database.repository import RequestRepository, UsageRepository


def _next_seq() -> int:
    """保证同参数的记录不会撞 requests.id 主键。"""
    _next_seq.n += 1
    return _next_seq.n


_next_seq.n = 0  # type: ignore[attr-defined]


async def _repo() -> UsageRepository:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    return UsageRepository(database)


async def _record(
    repo: UsageRepository, *, alias: str, prompt: int, completion: int, model: str = "m"
) -> None:
    """同时造一条 request（带 alias）和一条 usage——alias 维度靠这两表 join。"""
    request_id = f"req_{alias}_{prompt}_{completion}_{_next_seq()}"
    database = repo.db
    async with database.session() as session:
        session.add(
            RequestRecord(
                id=request_id,
                # requested_model 是 NOT NULL，而且语义是「客户端点了哪个模型」，
                # 不能用 alias 顶（那会把 None 传进去违反约束）。
                requested_model="m",
                resolved_model=model,
                alias=alias,
                provider_id="p",
                status="success",
                started_at=datetime.now(UTC),
                input_tokens=prompt,
                output_tokens=completion,
            )
        )
        session.add(
            UsageRecord(
                request_id=request_id,
                provider_id="p",
                model=model,
                input_tokens=prompt,
                output_tokens=completion,
                total_tokens=prompt + completion,
                cost_usd=0.0,
            )
        )


async def test_by_alias_aggregates_and_reports_io_ratio() -> None:
    """io_ratio 是这个维度的核心：比高得离谱 = 每轮都在重读历史。"""
    repo = await _repo()
    await _record(repo, alias="zk-k3", prompt=600_000, completion=1_000)
    await _record(repo, alias="zk-k3", prompt=600_000, completion=1_000)
    await _record(repo, alias="zk-auto", prompt=30_000, completion=6_000)

    summary = await repo.summary(days=7)
    by_alias = summary["by_alias"]
    top = by_alias[0]
    # input 多的排前面（这是按 input 降序）
    assert top["alias"] == "zk-k3"
    assert top["requests"] == 2
    assert top["input_tokens"] == 1_200_000
    assert top["output_tokens"] == 2_000
    assert top["io_ratio"] == 600.0

    healthy = next(r for r in by_alias if r["alias"] == "zk-auto")
    assert healthy["io_ratio"] == 5.0


async def test_by_alias_empty_window_is_a_list_not_an_error() -> None:
    """没有数据时返回空列表，别让控制台整页崩掉。"""
    repo = await _repo()
    summary = await repo.summary(days=1)
    assert summary["by_alias"] == []


async def test_by_alias_labels_a_null_alias() -> None:
    """直接调 /v1/chat 的请求没有别名，不能显示成 None。"""
    repo = await _repo()
    await _record(repo, alias=None, prompt=1_000, completion=100)  # type: ignore[arg-type]

    summary = await repo.summary(days=7)
    row = summary["by_alias"][0]
    assert row["alias"] == "（无别名）"


async def test_by_alias_ignores_usage_outside_the_window() -> None:
    """时间窗口必须真的生效，否则「近 7 天」会永远显示全部历史。"""
    repo = await _repo()
    await _record(repo, alias="zk-k3", prompt=500_000, completion=500)
    # 手工把这条 usage 的创建时间推到 30 天前
    old = datetime.now(UTC) - timedelta(days=30)
    async with repo.db.session() as session:
        await session.execute(update(UsageRecord).values(created_at=old))

    summary = await repo.summary(days=7)
    assert summary["by_alias"] == [], "30 天前的记录不该出现在近 7 天窗口里"


async def test_purge_also_removes_the_usage_rows_it_orphans() -> None:
    """清理 requests 时，指向它的 usage 行必须一起删。

    以前只删 requests，于是 usage 永久保留而它的 request 没了——任何要 join
    requests 的统计（按别名、按客户端）都让这些行凭空消失。实测 30 天窗口下
    by_alias 比 KPI 少算 15%。而且缺口是**结构性**的：30 天窗口减 14 天保留线
    等于一个永久的 16 天缺口环，会涨到 50%+。
    """
    from datetime import datetime, timedelta

    from app.database.models import RequestRecord

    repo = await _repo()
    await _record(repo, alias="zk-k3", prompt=500_000, completion=500)
    await _record(repo, alias="zk-auto", prompt=1000, completion=1000)

    database = repo.db
    # 把两条都推到 30 天前，然后按 7 天窗口清理
    old = datetime.now(UTC) - timedelta(days=30)
    async with database.session() as session:
        await session.execute(update(RequestRecord).values(started_at=old))
        await session.execute(update(UsageRecord).values(created_at=old))

    requests = RequestRepository(repo.db)
    removed = await requests.purge_older_than(7)
    assert removed == 2, "返回值应该仍是 requests 的删除行数"

    summary = await repo.summary(days=90)
    assert summary["by_alias"] == [], "usage 行该跟着 requests 一起消失"

    # 不该留孤儿（这会直接让 by_alias 少算）
    async with database.session() as session:
        orphans = await session.execute(
            select(func.count()).select_from(UsageRecord).where(
                ~UsageRecord.request_id.in_(select(RequestRecord.id))
            )
        )
        assert orphans.scalar_one() == 0
