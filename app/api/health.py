"""``GET /health`` - liveness + readiness in one payload."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ContainerDep

router = APIRouter(tags=["health"])


@router.get("/health", summary="网关健康状态")
async def health(container: ContainerDep) -> dict:
    """Return overall status, provider/credential availability and DB state."""
    database_ok = await container.database.health()
    pool_stats = container.pool.stats()
    availability = container.pool.provider_availability()

    providers_ready = sum(1 for ready in availability.values() if ready)
    if not container.ready:
        state = "starting"
    elif database_ok and providers_ready > 0:
        state = "healthy"
    elif providers_ready == 0:
        state = "degraded"  # gateway is up, but no provider can serve traffic
    else:
        state = "degraded"

    return {
        "status": state,
        "app": container.settings.app_name,
        "version": container.settings.version,
        "environment": container.settings.environment,
        "uptime_seconds": round(container.uptime(), 2),
        "database": {"ok": database_ok, "dialect": container.database.url.split(":")[0]},
        "providers": {
            "total": len(container.config.providers),
            "available": providers_ready,
            "detail": availability,
        },
        "credentials": pool_stats,
        "models": len(container.config.models),
        "aliases": sorted(container.config.aliases),
        "health_check_mode": container.settings.health_check_mode,
        # 被自动隔离的部署（连续失败到阈值）。留空 = 一切正常。
        # 为什么值得暴露：隔离是**自动**的，运营者必须能看见「谁被屏蔽了、
        # 为什么、还有多久」——否则一个渠道悄悄消失会让人以为是路由 bug。
        "quarantined_deployments": container.scheduler.quarantined_deployments(),
    }
