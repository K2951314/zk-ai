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
    }
