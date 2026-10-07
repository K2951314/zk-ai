"""HealthRepository: provider / credential probe history.

Stores the outcome of directory + inference probes and exposes recent /
latest-by-provider / error-rate views used by the health page and router.
"""

from __future__ import annotations

import datetime as dt

from app.database.repositories._common import (
    Any,
    Database,
    HealthCheck,
    RequestAttempt,
    case,
    func,
    select,
    utcnow,
)


class HealthRepository:
    """Provider / credential probe history."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(
        self,
        *,
        provider_id: str,
        credential_id: str | None,
        kind: str,
        ok: bool,
        latency_ms: float = 0.0,
        error_type: str | None = None,
        detail: str | None = None,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                HealthCheck(
                    provider_id=provider_id,
                    credential_id=credential_id,
                    kind=kind,
                    ok=ok,
                    latency_ms=latency_ms,
                    error_type=error_type,
                    detail=(detail or "")[:500] or None,
                )
            )

    async def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(HealthCheck).order_by(HealthCheck.checked_at.desc()).limit(limit)
            )
            return [
                {
                    "provider_id": row.provider_id,
                    "credential_id": row.credential_id,
                    "kind": row.kind,
                    "ok": row.ok,
                    "latency_ms": row.latency_ms,
                    "error_type": row.error_type,
                    "detail": row.detail,
                    "checked_at": row.checked_at,
                }
                for row in result.scalars()
            ]

    async def latest_by_provider(self) -> dict[str, dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(HealthCheck).order_by(HealthCheck.checked_at.desc()).limit(200)
            )
            latest: dict[str, dict[str, Any]] = {}
            for row in result.scalars():
                latest.setdefault(
                    row.provider_id,
                    {
                        "provider_id": row.provider_id,
                        "ok": row.ok,
                        "latency_ms": row.latency_ms,
                        "error_type": row.error_type,
                        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
                    },
                )
            return latest

    async def error_rate(self, *, minutes: int = 15) -> dict[str, Any]:
        """Recent failure ratio from the attempt table (used for health scoring)."""
        cutoff = utcnow() - dt.timedelta(minutes=minutes)
        failure_flag = case((RequestAttempt.status != "success", 1), else_=0)
        async with self.db.session() as session:
            result = await session.execute(
                select(
                    RequestAttempt.provider_id,
                    func.count(RequestAttempt.id),
                    func.coalesce(func.sum(failure_flag), 0),
                )
                .where(RequestAttempt.started_at >= cutoff)
                .group_by(RequestAttempt.provider_id)
            )
            return {
                row[0]: {
                    "attempts": int(row[1]),
                    "failures": int(row[2]),
                    "failure_rate": round(float(row[2]) / float(row[1]), 4) if row[1] else 0.0,
                }
                for row in result
            }
