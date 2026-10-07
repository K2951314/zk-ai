"""RequestRepository: request + attempt lifecycle.

Logs each incoming request, its per-deployment attempts, and supports the
console's filtered/paginated request log plus history purging.
"""

from __future__ import annotations

import datetime as dt

from app.database.repositories._common import (
    Any,
    AttemptOutcome,
    CursorResult,  # noqa: F401  (used in cast() string annotations for mypy)
    Database,
    RequestAttempt,
    RequestRecord,
    UsageRecord,
    cast,
    delete,
    func,
    logger,
    select,
    to_datetime,
    update,
    utcnow,
)


class RequestRepository:
    """Request + attempt lifecycle."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def start(
        self,
        *,
        request_id: str,
        requested_model: str,
        stream: bool,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                RequestRecord(
                    id=request_id,
                    requested_model=requested_model,
                    stream=stream,
                    status="pending",
                    client_ip=client_ip,
                    user_agent=(user_agent or "")[:255] or None,
                    started_at=utcnow(),
                )
            )

    async def finish(
        self,
        request_id: str,
        *,
        status: str,
        http_status: int | None,
        provider_id: str | None = None,
        resolved_model: str | None = None,
        alias: str | None = None,
        deployment_id: str | None = None,
        credential_id: str | None = None,
        error_type: str | None = None,
        attempt_count: int = 0,
        fallback_used: bool = False,
        routing_reason: str | None = None,
        latency_ms: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        async with self.db.session() as session:
            row = await session.get(RequestRecord, request_id)
            if row is None:
                logger.warning("finish() for unknown request %s", request_id)
                return
            row.status = status
            row.http_status = http_status
            row.provider_id = provider_id or row.provider_id
            row.resolved_model = resolved_model or row.resolved_model
            row.alias = alias or row.alias
            row.deployment_id = deployment_id or row.deployment_id
            row.credential_id = credential_id or row.credential_id
            # Preserve previously-recorded failure classification when a later
            # finalize pass runs without it (streaming can finish twice).
            row.error_type = error_type or row.error_type
            row.attempt_count = attempt_count
            row.fallback_used = fallback_used
            row.routing_reason = ((routing_reason or "")[:2000] or None) or row.routing_reason
            row.latency_ms = latency_ms
            row.input_tokens = input_tokens
            row.output_tokens = output_tokens
            row.total_tokens = input_tokens + output_tokens
            row.cost_usd = cost_usd
            row.finished_at = utcnow()

    async def add_attempts(self, request_id: str, attempts: list[AttemptOutcome]) -> None:
        if not attempts:
            return
        async with self.db.session() as session:
            for attempt in attempts:
                session.add(
                    RequestAttempt(
                        request_id=request_id,
                        attempt_number=attempt.attempt_number,
                        provider_id=attempt.provider,
                        model=attempt.model,
                        deployment_id=attempt.deployment_id,
                        credential_id=attempt.credential_id,
                        started_at=to_datetime(attempt.started_at),
                        finished_at=to_datetime(attempt.finished_at),
                        latency_ms=attempt.latency_ms,
                        status=attempt.status,
                        error_type=attempt.error_type,
                        http_status=attempt.http_status,
                        input_tokens=attempt.input_tokens,
                        output_tokens=attempt.output_tokens,
                        detail=(attempt.detail or "")[:1000] or None,
                    )
                )

    async def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(RequestRecord).order_by(RequestRecord.started_at.desc()).limit(limit)
            )
            return [
                {
                    "id": row.id,
                    "requested_model": row.requested_model,
                    "resolved_model": row.resolved_model,
                    "alias": row.alias,
                    "provider": row.provider_id,
                    "credential_id": row.credential_id,
                    "status": row.status,
                    "http_status": row.http_status,
                    "error_type": row.error_type,
                    "attempts": row.attempt_count,
                    "latency_ms": row.latency_ms,
                    "tokens": row.total_tokens,
                    "started_at": row.started_at,
                }
                for row in result.scalars()
            ]

    async def list_requests(
        self,
        *,
        status: str | None = None,
        alias: str | None = None,
        provider: str | None = None,
        credential: str | None = None,
        model: str | None = None,
        error_type: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Filtered, paginated request log (returns ``(rows, total)``).

        Built for the web console: every filter is optional and exact except
        ``model`` / ``q`` which do a contains-match across the likely columns.
        """
        conditions: list[Any] = []
        if status:
            conditions.append(RequestRecord.status == status)
        if alias:
            conditions.append(RequestRecord.alias == alias)
        if provider:
            conditions.append(RequestRecord.provider_id == provider)
        if credential:
            conditions.append(RequestRecord.credential_id == credential)
        if model:
            pattern = f"%{model}%"
            conditions.append(
                RequestRecord.requested_model.like(pattern) | RequestRecord.resolved_model.like(pattern)
            )
        if error_type:
            conditions.append(RequestRecord.error_type == error_type)
        if q:
            pattern = f"%{q}%"
            conditions.append(
                RequestRecord.id.like(pattern)
                | RequestRecord.requested_model.like(pattern)
                | RequestRecord.resolved_model.like(pattern)
                | RequestRecord.credential_id.like(pattern)
                | RequestRecord.error_type.like(pattern)
            )

        async with self.db.session() as session:
            total_result = await session.execute(
                select(func.count(RequestRecord.id)).where(*conditions)
            )
            total = int(total_result.scalar() or 0)
            result = await session.execute(
                select(RequestRecord)
                .where(*conditions)
                .order_by(RequestRecord.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = [
                {
                    "id": row.id,
                    "requested_model": row.requested_model,
                    "resolved_model": row.resolved_model,
                    "alias": row.alias,
                    "provider": row.provider_id,
                    "deployment_id": row.deployment_id,
                    "credential_id": row.credential_id,
                    "stream": row.stream,
                    "status": row.status,
                    "http_status": row.http_status,
                    "error_type": row.error_type,
                    "attempt_count": row.attempt_count,
                    "fallback_used": row.fallback_used,
                    "latency_ms": row.latency_ms,
                    "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens,
                    "total_tokens": row.total_tokens,
                    "cost_usd": row.cost_usd,
                    "user_agent": row.user_agent,
                    "started_at": row.started_at,
                    "finished_at": row.finished_at,
                }
                for row in result.scalars()
            ]
            return rows, total

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.get(RequestRecord, request_id)
            if row is None:
                return None
            return {
                "id": row.id,
                "requested_model": row.requested_model,
                "resolved_model": row.resolved_model,
                "alias": row.alias,
                "provider": row.provider_id,
                "deployment_id": row.deployment_id,
                "credential_id": row.credential_id,
                "stream": row.stream,
                "status": row.status,
                "http_status": row.http_status,
                "error_type": row.error_type,
                "attempt_count": row.attempt_count,
                "fallback_used": row.fallback_used,
                "routing_reason": row.routing_reason,
                "latency_ms": row.latency_ms,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "total_tokens": row.total_tokens,
                "cost_usd": row.cost_usd,
                "client_ip": row.client_ip,
                "user_agent": row.user_agent,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
            }

    async def attempts_for(self, request_id: str) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(RequestAttempt)
                .where(RequestAttempt.request_id == request_id)
                .order_by(RequestAttempt.attempt_number)
            )
            return [
                {
                    "attempt_number": row.attempt_number,
                    "provider": row.provider_id,
                    "model": row.model,
                    "deployment_id": row.deployment_id,
                    "credential_id": row.credential_id,
                    "latency_ms": row.latency_ms,
                    "status": row.status,
                    "error_type": row.error_type,
                    "http_status": row.http_status,
                    "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens,
                    "detail": row.detail,
                    "started_at": row.started_at,
                    "finished_at": row.finished_at,
                }
                for row in result.scalars()
            ]

    async def close_stale_pending(self, *, older_than_seconds: float = 900.0) -> int:
        """Mark long-stuck ``pending`` rows as cancelled (returns rows closed).

        A request whose client vanished before any finalizer ran would otherwise
        sit at "进行中" in the console forever.
        """
        cutoff = utcnow() - dt.timedelta(seconds=older_than_seconds)
        async with self.db.session() as session:
            result = await session.execute(
                update(RequestRecord)
                .where(RequestRecord.status == "pending", RequestRecord.started_at < cutoff)
                .values(status="cancelled", error_type="client_disconnected", finished_at=utcnow())
            )
            closed = int(cast("CursorResult[Any]", result).rowcount or 0)
            if closed:
                logger.info("closed %d stale pending request(s)", closed)
            return closed

    async def purge_older_than(self, days: int) -> int:
        """Delete request history older than *days* (returns rows removed).

        ``usage_records`` 里指向它们的行**必须一起删**。以前只删 requests，
        于是 usage 永久保留而它的 request 没了——任何要 join requests 的统计
        （按别名、按客户端）都会让这些行凭空消失。2026-09-26 实测：30 天窗口下
        by_alias 比 KPI 少算 15%（1.51 亿 input tokens），而 KPI 不 join 所以
        照单全收，两个数并排显示差的正是最早那一段。

        ⚠ 必须用 ``in_(select(...))`` 子查询，**不能**先 select 出 id 再
        ``in_(id_list)``：SQLite 的 MAX_VARIABLE_NUMBER=32766，网关连续跑十几天
        不重启就会超，而那个异常会被 container 的 ``except Exception`` 吞掉，
        结果是请求历史**永久停止清理**——比现在这个统计缺口严重得多。
        """
        cutoff = utcnow() - dt.timedelta(days=days)
        async with self.db.session() as session:
            # 先删明细：它们是依赖 requests 的那一方。
            await session.execute(
                delete(UsageRecord).where(
                    UsageRecord.request_id.in_(
                        select(RequestRecord.id).where(RequestRecord.started_at < cutoff)
                    )
                )
            )
            result = await session.execute(
                delete(RequestRecord).where(RequestRecord.started_at < cutoff)
            )
            # ``execute`` of a DELETE returns a CursorResult, which carries rowcount.
            return int(cast("CursorResult[Any]", result).rowcount or 0)
