"""Statistics and request-log endpoints (``/admin/stats``, ``/admin/requests/*``, ``/admin/usage/*``)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query, status

from app.api.admin._common import router
from app.api.deps import ContainerDep


@router.get("/stats", summary="流量、Token 与错误统计")
async def stats(
    container: ContainerDep,
    days: int = Query(default=7, ge=1, le=90, description="统计窗口（天）"),
    recent: int = Query(default=20, ge=0, le=200, description="附带返回最近多少条请求"),
) -> dict[str, Any]:
    """Token usage, cost estimate, error rates and recent request history."""
    usage = await container.usage_service.summary(days=days)
    return {
        "window_days": days,
        "uptime_seconds": round(container.uptime(), 2),
        "pool": container.pool.stats(),
        "usage": usage,
        "daily": await container.usage_service.daily(days=min(days, 30)),
        "error_rates": await container.health_repository.error_rate(minutes=60),
        "recent_requests": await container.request_repository.recent(limit=recent) if recent else [],
    }


@router.get("/requests", summary="按条件筛选请求记录并分页")
async def list_requests(
    container: ContainerDep,
    status: str | None = Query(default=None, description="success | error | cancelled | pending"),
    alias: str | None = Query(default=None, description="精确的别名"),
    provider: str | None = Query(default=None, description="精确的供应商 ID"),
    credential: str | None = Query(default=None, description="精确的凭据 ID"),
    model: str | None = Query(default=None, description="请求/实际模型名的子串"),
    error_type: str | None = Query(default=None, description="精确的错误类型"),
    q: str | None = Query(default=None, description="关键字：覆盖 ID / 模型 / 凭据 / 错误"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Paginated request log backing the web console."""
    rows, total = await container.request_repository.list_requests(
        status=status,
        alias=alias,
        provider=provider,
        credential=credential,
        model=model,
        error_type=error_type,
        q=q,
        limit=limit,
        offset=offset,
    )
    return {"object": "list", "total": total, "limit": limit, "offset": offset, "data": rows}


@router.get("/requests/{request_id}", summary="请求详情（含每次尝试）")
async def request_detail(request_id: str, container: ContainerDep) -> dict[str, Any]:
    attempts = await container.request_repository.attempts_for(request_id)
    row = await container.request_repository.get_request(request_id)
    if row is None and not attempts:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"请求记录 '{request_id}' 不存在"}},
        )
    return {"request_id": request_id, "request": row, "attempts": attempts}


@router.post("/usage/backfill-cost", summary="为没有成本的历史记录补算等价成本")
async def backfill_cost(container: ContainerDep) -> dict[str, Any]:
    """Reprice usage rows recorded before list prices existed (console button)."""
    return await container.usage_service.backfill_zero_cost()
