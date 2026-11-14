"""``GET /health`` - liveness + readiness in one payload."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ContainerDep
from app.core.logging import get_logger

logger = get_logger("api.health")

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
        # 部署形态：客户端接入面板与消耗器页都按这一块分流（本机写入 vs 公网调用）。
        # is_remote 是核心判据——控制台据此决定要不要把 127.0.0.1 的配置抄给用户。
        # 2026-11-08 审查：这里曾经带出 listen 与四个绝对路径（burner_log_path /
        # burner_state_path / config_dir / data_dir）——/health 是控制台有意
        # 不带令牌读的端点（Caddy 白名单），公网部署时等于把文件系统布局送出去。
        # 需要路径真相的地方走 /zkadmin/chatgpt 的 deploy 段（在令牌之后）。
        "deploy": {
            "is_remote": container.settings.is_remote_deploy,
            "exposure": container.settings.exposure or (
                "server" if container.settings.is_remote_deploy else "local"),
            "public_base_url": container.settings.public_base_url_effective,
            "client_base_url": container.settings.client_base_url,
        },
        "database": {"ok": database_ok, "dialect": container.database.url.split(":")[0]},
        "providers": {
            "total": len(container.config.providers),
            "available": providers_ready,
            "detail": availability,
        },
        "credentials": pool_stats,
        "models": len(container.config.models),
        # 有几个模型**此刻真的能接活**：部署未被冷却/隔离，且其供应商至少
        # 还有一把可用 Key。2026-11-14 加——`models: 10` 只报数量，
        # 运营者从 /health 看不出「这 10 个里有 7 个是死的」。
        # 实测同一时刻 `models=10` 而实际只有 2 个能返回 200：
        # sensenova 全限流、AMD Key 冷却 536s、workbuddy 代理没起。
        # 判据复用现成两处（`unavailable_deployments` + `provider_availability`），
        # 不另造标准——那正是「检查说行、实际调不通」的裂缝来源。
        "models_servable": _servable_models(container),
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


def _servable_models(container: ContainerDep) -> dict[str, int]:
    """此刻真能接活的模型数（与总数对照看）。

    判据刻意**复用**现成的两处判断，不另造标准：
    - ``scheduler.unavailable_deployments()`` —— 部署是否在冷却/隔离中；
    - ``pool.provider_availability()`` —— 供应商至少还有一把可用 Key。

    为什么不加「发一次真请求探测」：/health 是无令牌可读的端点、会被控制台
    高频轮询，每次探测都是真钱。这里答的是「按网关自己的账本，现在能不能
    路由」，不是「上游此刻一定 200」——后者只有真请求才知道，别让这个数
    冒充成那个意思。
    """
    unavailable = container.scheduler.unavailable_deployments()
    provider_ok = container.pool.provider_availability()
    servable = 0
    for model in container.config.models.values():
        if not model.enabled:
            continue
        if any(
            dep.enabled
            and dep.id not in unavailable
            and provider_ok.get(dep.provider_id, False)
            for dep in model.deployments
        ):
            servable += 1
    total = sum(1 for m in container.config.models.values() if m.enabled)
    return {"servable": servable, "enabled": total}


def _config_watch(container: ContainerDep) -> dict | None:
    """Watcher status, or ``None`` when it is disabled - the console shows that
    as "改完配置要手动 reload", which is the truth in that mode."""
    watcher = container.config_watcher
    return watcher.status() if watcher is not None else None


def _stale_config_files() -> list[str]:
    try:
        from app.core.config_writer import stale_config_files

        return stale_config_files()
    except Exception:  # 体检失败不该让 /health 挂掉
        # 但也不能一个字不留：这是「外部改过配置还没 reload」的体检器，
        # 静默失明 = 控制台永远显示「没有陈旧文件」，运营者被假象安抚
        # （2026-11-08 审查：同型事故在 /health 的 config 段发生过一次）。
        logger.exception("stale_config_files 体检失败")
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
