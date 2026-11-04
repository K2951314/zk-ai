"""EventRepository: 自动化中台的事件表。

三条约束（改之前先想清楚）：

1. **去重发生在写入时，不是查询时。** 先查「同 dedupe_key 且还在窗口内的最近一条」，
   命中就直接返回那一条并把 ``dup_count`` 加一，不新建行。查询侧再去重意味着
   同一件事会先占一行、再被过滤掉 —— 看板上永远有一堆看不见的幽灵任务。
2. **认领是原子的。** ``claim`` 在一个事务里 SELECT + UPDATE，SQLite 单写者 +
   ``Database.session()`` 的进程级锁保证了不会两个 worker 抢到同一条。
3. **超时认领要能回收。** worker 锁屏/崩溃会留下 ``running`` 且 ``claimed_at`` 很旧的
   行，``reclaim_stale`` 把它们退回 ``received``，否则任务永远卡在那里没人发现。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import aliased

from app.database.repositories._common import (
    Any,
    Database,
    EventRecord,
    delete,
    func,
    select,
    update,
    utcnow,
)

#: 未结束的状态 —— 只有这些会被 ``reclaim_stale`` 回收。
OPEN_STATES: tuple[str, ...] = ("received", "running")


class EventRepository:
    """事件表的全部读写。业务判断在 ``EventService``，这里只管数据。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    async def find_recent(self, dedupe_key: str, *, within_seconds: float) -> dict[str, Any] | None:
        """同语义键、且在窗口内的最近一条（用于去重）。"""
        cutoff = utcnow() - dt.timedelta(seconds=within_seconds)
        async with self.db.session() as session:
            result = await session.execute(
                select(EventRecord)
                .where(EventRecord.dedupe_key == dedupe_key)
                .where(EventRecord.received_at >= cutoff)
                .order_by(EventRecord.id.desc())
                .limit(1)
            )
            row = result.scalars().first()
            return self._as_dict(row) if row else None

    async def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        async with self.db.session() as session:
            row = EventRecord(**fields)
            session.add(row)
            await session.flush()
            await session.commit()
            return self._as_dict(row)

    async def bump_duplicate(self, event_id: int) -> dict[str, Any] | None:
        """同一条语义事件又来了：只加计数，不新建行。返回更新后的那条。"""
        async with self.db.session() as session:
            row = await session.get(EventRecord, event_id)
            if row is None:
                return None
            payload = dict(row.payload or {})
            payload["dup_count"] = int(payload.get("dup_count", 1)) + 1
            row.payload = payload
            row.updated_at = utcnow()
            await session.commit()
            return self._as_dict(row)

    async def claim(self, *, worker: str) -> dict[str, Any] | None:
        """原子认领一条 ``received``；没有就返回 None（调用方负责再等）。

        「组内并发已满」的事件**不认领但也不丢弃**：它留在 ``received`` 里排队，
        下轮再来。这比拒绝入库好——拒绝了就等于把「同一条意见的第 4 个补充」
        直接扔掉，而它其实是有效事件。
        """
        sibling = aliased(EventRecord)
        running_in_group = (
            select(func.count())
            .select_from(sibling)
            .where(sibling.state == "running")
            .where(sibling.group_name == EventRecord.group_name)
            .scalar_subquery()
        )
        async with self.db.session() as session:
            result = await session.execute(
                select(EventRecord)
                .where(EventRecord.state == "received")
                # 约了时间复查的（回执 later）还没到点，不许认领。
                # 它一直躺在 queue 里，但不占进程、不花 Token ——
                # 直到 not_before 到了才轮得到它。
                .where(
                    EventRecord.not_before.is_(None) | (EventRecord.not_before <= utcnow())
                )
                .where(
                    (EventRecord.group_name.is_(None)) | (running_in_group < EventRecord.parallel)
                )
                .order_by(EventRecord.id.asc())
                .limit(1)
            )
            row = result.scalars().first()
            if row is None:
                return None
            row.state = "running"
            row.claimed_by = worker
            row.claimed_at = utcnow()
            row.attempts = int(row.attempts or 0) + 1
            await session.commit()
            return self._as_dict(row)

    async def finish(
        self,
        event_id: int,
        *,
        state: str,
        verdict: str | None = None,
        need_decision: bool = False,
        cost_tokens: int = 0,
        cost_cny: float = 0.0,
        session_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.get(EventRecord, event_id)
            if row is None:
                return None
            row.state = state
            row.verdict = verdict if verdict is not None else row.verdict
            row.need_decision = need_decision
            row.cost_tokens = int(cost_tokens or 0)
            row.cost_cny = float(cost_cny or 0.0)
            if session_id:
                row.session_id = session_id
            if error:
                row.verdict = f"{row.verdict or ''}\n\n【执行失败】{error}".strip()
            row.ended_at = utcnow()
            row.updated_at = utcnow()
            await session.commit()
            return self._as_dict(row)

    async def mark_notified(self, event_id: int) -> None:
        async with self.db.session() as session:
            row = await session.get(EventRecord, event_id)
            if row is None:
                return
            row.notified = True
            await session.commit()

    async def set_state(self, event_id: int, state: str) -> None:
        async with self.db.session() as session:
            row = await session.get(EventRecord, event_id)
            if row is None:
                return
            row.state = state
            row.updated_at = utcnow()
            await session.commit()

    async def reclaim_stale(
        self, *, older_than_seconds: float, max_attempts: int = 3
    ) -> tuple[int, int]:
        """把「认领了但超时没交活」的事件退回待认领；超过重试上限的判死。

        为什么要有上限（2026-11-03）：原来无条件退回，``attempts`` 只增不读——
        一条永远失败的事件会每 ``claim_timeout``（默认 2400s）重投一次，
        每次都推一张失败卡片。设计文档 §8 写着「attempts 上限 3 次」，
        但一直没落地。故障风暴时这条路会把飞书刷爆，正是文章作者说的
        「一天被推几十条差点崩」。

        返回 ``(退回条数, 判死条数)``。判死的进 ``failed`` 而不是删除——
        留痕比清干净重要，运营者要能看见「这条试了 3 次都不行」。
        """
        cutoff = utcnow() - dt.timedelta(seconds=older_than_seconds)
        stale = (
            EventRecord.state == "running",
            EventRecord.claimed_at.is_not(None),
            EventRecord.claimed_at < cutoff,
        )
        async with self.db.session() as session:
            dead = await session.execute(
                update(EventRecord)
                .where(*stale)
                .where(EventRecord.attempts >= max_attempts)
                .values(
                    state="failed",
                    claimed_by=None,
                    claimed_at=None,
                    verdict=EventRecord.verdict,
                    ended_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            reclaimed = await session.execute(
                update(EventRecord)
                .where(*stale)
                .where(EventRecord.attempts < max_attempts)
                .values(state="received", claimed_by=None, claimed_at=None, updated_at=utcnow())
            )
            await session.commit()
            return (
                int(getattr(reclaimed, "rowcount", 0) or 0),
                int(getattr(dead, "rowcount", 0) or 0),
            )


    # ------------------------------------------------------------------ #
    # 约时间复查（回执里的 later）
    # ------------------------------------------------------------------ #
    async def schedule_later(self, event_id: int, key: str, at: dt.datetime, about: str) -> int:
        """记一条「到 at 再核实」的复查，返回它的编号。

        同一个 key 重复排期时**覆盖**而不是新增（Agent 复查后常会往后推），
        否则同一件事的复查会越堆越多，每次醒来都重复问一遍。
        实现：把旧的那条改成 superseded（不是删除 —— 「往后推过几次」
        本身是排期健康状况的信号），再开一条新的。
        """
        from app.database.models import EventLaterRecord

        async with self.db.session() as session:
            old = await session.execute(
                select(EventLaterRecord)
                .where(EventLaterRecord.event_id == event_id)
                .where(EventLaterRecord.key == key)
                .where(EventLaterRecord.state == "pending")
            )
            for row in old.scalars():
                row.state = "superseded"
            record = EventLaterRecord(event_id=event_id, key=key, at=at, about=about, state="pending")
            session.add(record)
            await session.flush()
            new_id = int(record.id)
            await session.commit()
            return new_id

    async def cancel_later(self, event_id: int, key: str) -> int:
        """撤销一条还没到期的复查，返回撤掉几条。"""
        from app.database.models import EventLaterRecord

        async with self.db.session() as session:
            result = await session.execute(
                update(EventLaterRecord)
                .where(EventLaterRecord.event_id == event_id)
                .where(EventLaterRecord.key == key)
                .where(EventLaterRecord.state == "pending")
                .values(state="cancelled")
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def take_due_laters(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """取走到点（或已过期）的复查，原子地标成 fired 并返回它们。

        为什么用「取走」而不是「查询」：采集器会重复轮询，如果只是
        SELECT，同一条复查会被转成事件 N 次。这里在一个事务里
        UPDATE ... WHERE state='pending' AND at<=now 再回读，
        取到即消费，天然幂等。
        """
        from app.database.models import EventLaterRecord

        now = utcnow()
        async with self.db.session() as session:
            due = await session.execute(
                select(EventLaterRecord.id)
                .where(EventLaterRecord.state == "pending")
                .where(EventLaterRecord.at <= now)
                .order_by(EventLaterRecord.at.asc())
                .limit(limit)
            )
            ids = [int(item) for item in due.scalars()]
            if not ids:
                return []
            # 只有还停在 pending 的那几条会被改走 —— 两个 worker 同时抢，
            # 第二个的 rowcount 会少，回读也只拿到自己改成的。
            claimed = await session.execute(
                update(EventLaterRecord)
                .where(EventLaterRecord.id.in_(ids))
                .where(EventLaterRecord.state == "pending")
                .values(state="fired", fired_at=now)
                .returning(EventLaterRecord.id)
            )
            fired_ids = [int(item) for item in claimed.scalars()]
            if not fired_ids:
                return []
            rows = await session.execute(
                select(EventLaterRecord).where(EventLaterRecord.id.in_(fired_ids))
            )
            result = [self._later_dict(row) for row in rows.scalars()]
            await session.commit()
            return result

    @staticmethod
    def _later_dict(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "event_id": row.event_id,
            "key": row.key,
            "at": _iso(row.at),
            "about": row.about or "",
            "state": row.state,
        }

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    async def list(
        self,
        *,
        state: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = select(EventRecord).order_by(EventRecord.id.desc())
        if state:
            query = query.where(EventRecord.state == state)
        if source:
            query = query.where(EventRecord.source == source)
        query = query.limit(limit).offset(offset)
        async with self.db.session() as session:
            result = await session.execute(query)
            return [self._as_dict(row) for row in result.scalars()]

    async def get(self, event_id: int) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.get(EventRecord, event_id)
            return self._as_dict(row) if row else None

    async def stats(self, *, days: int = 7) -> dict[str, Any]:
        """看板用：按状态、按来源、按规则聚合。"""
        cutoff = utcnow() - dt.timedelta(days=days)
        async with self.db.session() as session:
            by_state = await session.execute(
                select(EventRecord.state, func.count())
                .where(EventRecord.received_at >= cutoff)
                .group_by(EventRecord.state)
            )
            by_source = await session.execute(
                select(EventRecord.source, func.count())
                .where(EventRecord.received_at >= cutoff)
                .group_by(EventRecord.source)
            )
            by_rule = await session.execute(
                select(EventRecord.rule, func.count(), func.coalesce(func.sum(EventRecord.cost_cny), 0.0))
                .where(EventRecord.received_at >= cutoff)
                .where(EventRecord.rule.is_not(None))
                .group_by(EventRecord.rule)
            )
            open_count = await session.execute(
                select(func.count()).where(EventRecord.state.in_(OPEN_STATES))
            )
            return {
                "window_days": days,
                "by_state": {str(k): int(v) for k, v in by_state.all()},
                "by_source": {str(k): int(v) for k, v in by_source.all()},
                "by_rule": [
                    {"rule": str(rule), "count": int(count), "cost_cny": round(float(cost), 4)}
                    for rule, count, cost in by_rule.all()
                ],
                "open": int(open_count.scalar() or 0),
            }

    async def purge_older_than(self, *, days: int) -> int:
        """保留期外的已结束事件清掉（未结束的永远不清）。"""
        cutoff = utcnow() - dt.timedelta(days=days)
        async with self.db.session() as session:
            result = await session.execute(
                delete(EventRecord)
                .where(EventRecord.received_at < cutoff)
                .where(EventRecord.state.not_in(OPEN_STATES))
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _as_dict(row: EventRecord) -> dict[str, Any]:
        return {
            "id": row.id,
            "source": row.source,
            "type": row.type,
            "object": row.object,
            "summary": row.summary,
            "link": row.link,
            "payload": row.payload or {},
            "dedupe_key": row.dedupe_key,
            "state": row.state,
            "rule": row.rule,
            "group_name": row.group_name,
            "parallel": int(row.parallel or 1),
            "role": row.role,
            "cwd": row.cwd,
            "task": row.task,
            "session_id": row.session_id,
            "verdict": row.verdict,
            "need_decision": bool(row.need_decision),
            "notified": bool(row.notified),
            "cost_tokens": int(row.cost_tokens or 0),
            "cost_cny": float(row.cost_cny or 0.0),
            "attempts": int(row.attempts or 0),
            "hops": int(getattr(row, "hops", 0) or 0),
            "parent_id": getattr(row, "parent_id", None),
            "claimed_by": row.claimed_by,
            "received_at": _iso(row.received_at),
            "ended_at": _iso(row.ended_at),
        }


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = ["OPEN_STATES", "EventRepository"]
