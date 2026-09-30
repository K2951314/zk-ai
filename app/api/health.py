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
        # 配置文件监听：非空 = 「改完 YAML 不用再 reload」。last_error 非空表示
        # 上一次编辑**没有生效**（多半是 YAML 写坏了），原因就在这里，不用猜。
        "config_watch": _config_watch(container),
        # 配置层面的坏味道。控制台首页「需要处理」面板读的正是这一段
        # （app/web/index.html 的 health.config.warnings）——2026-09-29 发现
        # 这个键以前**根本没返回**，于是那一行面板永远是空的：功能写了、
        # 没人看见。三条现在都有真内容。
        "config": {
            # loader 的告警（不可解析 target、悬空引用等）
            "warnings": list(container.config.warnings),
            # 有人在外面改过配置、网关还没 reload —— 控制台一保存就覆盖回去
            "stale_files": _stale_config_files(),
            # 凭据要读的 Key 不在 .env 里：本机好用，换机会静默丢掉
            # （2026-09-29 的 SENSENOVA_API_KEY）。此刻不报，换机那天才报就晚了。
            "credential_env_gaps": _credential_env_gaps(container),
        },
    }


def _config_watch(container: ContainerDep) -> dict | None:
    """Watcher status, or ``None`` when it is disabled - the console shows that
    as "改完配置要手动 reload", which is the truth in that mode."""
    watcher = container.config_watcher
    return watcher.status() if watcher is not None else None


def _stale_config_files() -> list[str]:
    try:
        from app.core.config_writer import stale_config_files

        return stale_config_files()
    except Exception:  # pragma: no cover - 体检失败不该让 /health 挂掉
        return []


def _credential_env_gaps(container: ContainerDep) -> dict[str, list[str]]:
    """{环境变量名: 读它的凭据 id}，只列不在 .env 里的（见 config.credential_env_gaps）。

    故意不在启动时算一次：`.env` 随时可能被补上，过期答案比没有更糟。
    文件只有几十行，健康检查的轮询频率下这点 I/O 可以忽略。
    """
    try:
        from app.core.config import credential_env_gaps

        return credential_env_gaps(container.config)
    except Exception:  # pragma: no cover - 同上
        return {}
