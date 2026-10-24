"""Application container: the single place where the object graph is assembled.

Everything is created once at startup and injected through ``app.state``, which
keeps the API layer free of global state and makes tests able to build a fully
wired container with fake providers.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import AppConfig, Settings, load_app_config
from app.core.config_watch import ConfigWatcher, watch_paths
from app.core.logging import get_logger
from app.credentials.cooldown import CooldownPolicy
from app.credentials.pool import CredentialPool
from app.database.db import Database
from app.database.repository import (
    AgentRepository,
    ConfigRepository,
    HealthRepository,
    RequestRepository,
    UsageRepository,
)
from app.retry.policy import RetryPolicy
from app.routing.limits import RateLimiter
from app.routing.router import Router
from app.routing.scheduler import Scheduler
from app.services.agent.service import AgentService
from app.services.health_service import HealthService
from app.services.model_service import ModelService
from app.services.request_service import RequestService
from app.services.usage_service import UsageService

logger = get_logger("container")

#: 限流账本落盘周期。RateLimiter.flush() 自己也按 _FLUSH_INTERVAL 自限流，
#: 所以这个值只决定「最坏多久落一次盘」，双重点节流无害。
_RATE_LIMIT_FLUSH_SECONDS = 30.0

#: 凭据运行时状态（计数器/冷却/禁用原因）镜像周期。这是取证证据不是热路径，
#: 60s 足够；池子只有几十个对象，一次全量写对 SQLite 毫无压力。
_CREDENTIAL_MIRROR_SECONDS = 60.0


@dataclass
class Container:
    """Wired application services."""

    settings: Settings
    config: AppConfig
    database: Database
    pool: CredentialPool
    router: Router
    scheduler: Scheduler
    rate_limiter: RateLimiter
    request_service: RequestService
    model_service: ModelService
    usage_service: UsageService
    health_service: HealthService
    config_repository: ConfigRepository
    request_repository: RequestRepository
    usage_repository: UsageRepository
    health_repository: HealthRepository
    agent_repository: AgentRepository
    agent_service: AgentService
    started_at: float = field(default_factory=time.time)
    ready: bool = False
    #: Set once the file watcher is running; None when disabled or not started.
    config_watcher: ConfigWatcher | None = None
    #: Set while the rate-limit flush loop is running.
    _flush_task: asyncio.Task[None] | None = None
    #: Set while the credential runtime mirror loop is running.
    _cred_mirror_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    async def startup(self) -> None:
        """Init DB, mirror config, probe providers (mode dependent)."""
        await self.database.init()
        self.rate_limiter.load()
        try:
            await self.config_repository.sync_config(self.config)
        except Exception:
            logger.exception("failed to mirror configuration into the database")
        # Re-apply the console quota rules that could not reach providers.yaml
        # (template-only setups); models/aliases are file-authoritative now.
        try:
            from app.core.config import apply_db_overrides

            apply_db_overrides(
                self.config,
                await self.config_repository.provider_rate_limit_overrides(),
            )
            self.router.aliases.replace_all(self.config.aliases.values())
            self.rate_limiter.configure(self.config.providers)
            await self.config_repository.sync_config(self.config)
        except Exception:
            logger.exception("failed to apply DB configuration overrides")
        # Housekeeping: close zombie "pending" rows from crashed requests and
        # prune ancient history so the log does not grow forever.
        try:
            await self.request_repository.close_stale_pending()
            purged = await self.request_repository.purge_older_than(days=14)
            if purged:
                logger.info("purged %d request row(s) older than 14 days", purged)
        except Exception:
            logger.exception("startup database housekeeping failed")
        # ZK-Agent sessions that were mid-flight when the process died.
        try:
            interrupted = await self.agent_service.mark_stale_interrupted()
            if interrupted:
                logger.info("marked %d agent session(s) interrupted", interrupted)
        except Exception:
            logger.exception("agent session housekeeping failed")
        try:
            await self.health_service.startup()
        except Exception:
            logger.exception("startup health check failed")
        await self.health_service.start_scheduler()
        # The inference surface (/v1/*) is open by design when api_token is unset.
        # That is fine on loopback but a real exposure on a shared LAN, so warn loudly.
        host = str(getattr(self.settings, "host", "127.0.0.1"))
        if host not in {"127.0.0.1", "localhost", "::1"} and not getattr(
            self.settings, "api_token", None
        ):
            logger.warning(
                "gateway is listening on %s with NO api_token - every host on the "
                "network can spend your upstream quota. Set ZKAI_API_TOKEN to gate it.",
                host,
            )
        self.ready = True
        logger.info(
            "ZK-AI ready: %d provider(s), %d model(s), %d alias(es)",
            len(self.config.providers),
            len(self.config.models),
            len(self.config.aliases),
        )

    async def shutdown(self) -> None:
        """Release every external resource."""
        self.ready = False
        await self.stop_config_watch()
        await self.stop_rate_limit_flush()
        await self.stop_credential_mirror()
        # 关闭前最后镜像一次凭据状态：断路器刚禁用的 Key、正在冷却的 Key，
        # 不等下一个 60s 周期就落盘。
        try:
            await self.config_repository.mirror_credential_runtime(self.pool.all())
        except Exception:  # pragma: no cover - defensive
            logger.exception("关闭前镜像凭据运行时状态失败")
        self.rate_limiter.flush(force=True)
        await self.health_service.stop()
        await self.router.aclose()
        await self.database.dispose()
        logger.info("ZK-AI stopped")

    # ------------------------------------------------------------------ #
    # 限流账本的周期落盘
    # ------------------------------------------------------------------ #
    def start_rate_limit_flush(self) -> None:
        """Persist the sliding-window ledger on a timer, not only at shutdown.

        Why this exists (2026-09-29, third review): ``RateLimiter.flush()`` was
        called from exactly one place - ``shutdown()`` - and this gateway has
        **never shut down gracefully** (``grep -c "ZK-AI stopped" data/gateway.log``
        -> 0). So ``data/rate_limits.json`` sat frozen at 2026-09-25 while thousands
        of SenseNova requests went by, and the per-account token guard in
        providers.yaml ("stop offering this account's keys once the line is hit")
        had been a no-op for four days. A ledger nobody flushes is worse than no
        ledger: it looks like a working quota guard.

        ``flush()`` already refuses to write when nothing changed and self-limits to
        ``_FLUSH_INTERVAL``, so a 30s tick costs a no-op call most of the time.
        """
        if self._flush_task is not None:
            return
        self._flush_task = asyncio.create_task(
            self._rate_limit_flush_loop(), name="zkai-rate-limit-flush"
        )
        logger.info("限流账本周期落盘已启动：每 %.0fs", _RATE_LIMIT_FLUSH_SECONDS)

    async def _rate_limit_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(_RATE_LIMIT_FLUSH_SECONDS)
            try:
                self.rate_limiter.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("限流账本落盘失败（下个周期再试）")

    async def stop_rate_limit_flush(self) -> None:
        task, self._flush_task = self._flush_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ------------------------------------------------------------------ #
    # 凭据运行时状态的周期镜像
    # ------------------------------------------------------------------ #
    def start_credential_mirror(self) -> None:
        """Persist live credential counters on a timer.

        Why this exists (2026-10-23 对抗式审查 #7): ``mirror_credential_runtime``
        existed with zero callers — a credential that was rate-limited into a
        cooldown, or disabled by the circuit breaker, came back **healthy** after
        every restart. The operator saw "这把 Key 昨晚被限流了" in the console,
        restarted, and the evidence was gone. Counters are in-memory truth; the
        DB row is the only thing that survives a process that (per the rate-limit
        flush comment above) essentially never shuts down gracefully.

        Same shape as the rate-limit flush: self-limiting, no-op when nothing
        changed is NOT guaranteed here, so the interval is deliberately coarse
        (60s) — this is forensic evidence, not a hot path.
        """
        if self._cred_mirror_task is not None:
            return
        self._cred_mirror_task = asyncio.create_task(
            self._credential_mirror_loop(), name="zkai-credential-mirror"
        )
        logger.info("凭据运行时状态周期镜像已启动：每 %.0fs", _CREDENTIAL_MIRROR_SECONDS)

    async def _credential_mirror_loop(self) -> None:
        while True:
            await asyncio.sleep(_CREDENTIAL_MIRROR_SECONDS)
            try:
                await self.config_repository.mirror_credential_runtime(self.pool.all())
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("凭据运行时状态镜像失败（下个周期再试）")

    async def stop_credential_mirror(self) -> None:
        task, self._cred_mirror_task = self._cred_mirror_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ------------------------------------------------------------------ #
    # Configuration reload (shared by the console endpoint and the file watch)
    # ------------------------------------------------------------------ #
    async def reload_config(self) -> dict[str, Any]:
        """Re-read the YAML files and rebuild every derived object in place.

        One implementation, two callers: ``POST /admin/config/reload`` (the
        operator presses the button) and :class:`app.core.config_watch.ConfigWatcher`
        (the file changed underneath us). They must not drift - a watcher that
        rebuilt less than the button would leave "it works after I press reload"
        looking like a bug.

        Raises whatever ``load_app_config`` raises; callers decide what a bad
        config means (the endpoint turns it into a 400, the watcher keeps the
        previous config and logs).
        """
        from app.routing.aliases import AliasRegistry

        # YAML 解析 / router 重建 / pool 注册都是阻塞 CPU+IO——放在事件循环里
        # 会让重载瞬间（配置文件越大越久）全部在途请求 Stall。丢进线程跑，
        # 事件循环继续服务请求。（2026-10-23 对抗式审查 #6）
        def _rebuild() -> Any:
            config = load_app_config(self.settings)
            self.router.reload(config, alias_registry=AliasRegistry(config.aliases.values()))
            # 冷却策略（cooldown: 段）也在热更新范围内。2026-10-13 实测事故：
            # 运营者把 throughput 从 account 改成 credential，日志里却仍出现
            # "429 (throughput) has account-wide cooldown -> failing over instead of
            # rotating keys"——改前改后行为一模一样。根因是 CooldownPolicy 只在
            # build_container 里构造过一次，reload 重建了 router/别名/凭据却没有
            # 重建它，于是「改完 YAML 自动生效」对这一个文件段是句空话（必须整进程
            # 重启才生效）。后果正是运营者报的那个：商汤一把 Key 429 就停靠同账号
            # 兄弟 Key 并直接故障转移，其余 8 把健康 Key 一次都没被试过。
            # 用同一个 from_mapping，与 build_container 的构造路径完全一致。
            self.pool.policy = CooldownPolicy.from_mapping(config.raw.get("cooldown"))
            self.pool.tracker.policy = self.pool.policy
            # credential_affinity 段同理（池子只在建池时读它一次）：reload
            # 不补读的话，改 ttl/max_sessions/enabled 都要重启进程才生效。
            self.pool.apply_affinity_config(config.raw.get("credential_affinity"))
            for provider in config.providers.values():
                self.pool.register_provider(provider)
            pruned = self.pool.reconcile(config.providers)
            return config, pruned

        config, pruned = await asyncio.to_thread(_rebuild)

        # Re-apply quota rules that could not reach providers.yaml (file is
        # otherwise authoritative: models/aliases edits were written back to it).
        from app.core.config import apply_db_overrides

        apply_db_overrides(
            config,
            await self.config_repository.provider_rate_limit_overrides(),
        )

        self.config = config
        self.model_service.config = config
        self.health_service.config = config
        self.usage_service.config = config
        self.rate_limiter.configure(config.providers)
        if pruned:
            logger.info("config reload pruned %d stale credential(s) from the pool", pruned)

        # 重载后重新镜像 DB：YAML 里新增/改名的供应商、凭据、模型（市场加模型、
        # 手改文件）此前只进内存，DB 镜像要等下次重启才更新——控制台列表和统计
        # 一直显示旧数据。（2026-10-23 对抗式审查 #5）
        # 失败只记日志不中断重载：内存配置已经生效，DB 镜像下个周期/重启会补上。
        try:
            await self.config_repository.sync_config(config)
        except Exception:  # pragma: no cover - defensive
            logger.exception("config reload: failed to re-mirror configuration into the database")

        # The watcher's baseline must move with the config it just loaded, or the
        # next poll would see the same change again and reload forever.
        self.refresh_config_watch()
        return {
            "reloaded": True,
            "models": len(config.models),
            "aliases": sorted(config.aliases),
            "providers": sorted(config.providers),
            "warnings": config.warnings,
        }

    # ------------------------------------------------------------------ #
    # File watch (see app/core/config_watch.py for why this exists)
    # ------------------------------------------------------------------ #
    def start_config_watch(self) -> None:
        """Begin watching the loaded YAML files, unless disabled."""
        if self.config_watcher is not None or not self.settings.watch_config:
            return
        paths = watch_paths(self.settings.resolved_config_dir, self.config.source_files)
        if not paths:
            logger.info("配置监听未启动：没有找到已加载的 YAML 文件")
            return
        # Reuse the *async* reload: the watcher runs on the event loop and a
        # blocking sync wrapper here would deadlock against the DB call inside.
        self.config_watcher = ConfigWatcher(
            paths=paths,
            on_change=self._on_config_files_changed,
            interval=self.settings.watch_config_interval,
        )
        self.config_watcher.start()

    def refresh_config_watch(self) -> None:
        """Re-seed the watcher's file signatures after a reload (or a write)."""
        if self.config_watcher is not None:
            self.config_watcher.reseed()

    async def stop_config_watch(self) -> None:
        watcher, self.config_watcher = self.config_watcher, None
        if watcher is not None:
            await watcher.stop()

    async def _on_config_files_changed(self, paths: list[Path]) -> None:
        names = "、".join(p.name for p in paths)
        logger.info("检测到配置文件改动：%s", names)
        try:
            summary = await self.reload_config()
        except Exception as exc:
            logger.error("自动 reload 失败（保留上一份配置）：%s", exc)
            raise
        logger.info(
            "自动 reload 完成：%d 模型 / %d 别名 / %d 供应商",
            summary["models"],
            len(summary["aliases"]),
            len(summary["providers"]),
        )

    # ------------------------------------------------------------------ #
    def uptime(self) -> float:
        return time.time() - self.started_at

    def snapshot(self) -> dict[str, Any]:
        """Cheap overview used by ``/health`` and ``/admin/health``."""
        return {
            "app": self.settings.app_name,
            "version": self.settings.version,
            "environment": self.settings.environment,
            "uptime_seconds": round(self.uptime(), 2),
            "ready": self.ready,
            "config": self.config.describe(),
        }


async def build_container(
    settings: Settings | None = None,
    *,
    config: AppConfig | None = None,
    database: Database | None = None,
    pool: CredentialPool | None = None,
    router: Router | None = None,
    policy: RetryPolicy | None = None,
    start_services: bool = True,
) -> Container:
    """Assemble the container.

    Parameters allow tests to inject fakes (adapters via ``router``, an in-memory
    database, a pre-filled pool) without touching the real network.
    """
    settings = settings or Settings()
    config = config or load_app_config(settings)
    # config.yaml may override process settings; the effective set lives on AppConfig.
    settings = config.settings

    database = database or Database(config.settings.resolved_database_url, echo=settings.db_echo)

    if pool is None:
        rotation = str(config.raw.get("credential_rotation", "priority"))
        affinity = config.raw.get("credential_affinity") or {}
        pool = CredentialPool(
            rotation=rotation,
            policy=CooldownPolicy.from_mapping(config.raw.get("cooldown")),
            allow_inline_secrets=settings.allow_inline_secrets,
            affinity_enabled=bool(affinity.get("enabled", True)),
            affinity_ttl=float(affinity.get("ttl_seconds", 1800.0)),
            affinity_max_sessions=int(affinity.get("max_sessions", 4096)),
        )
        for provider in config.providers.values():
            pool.register_provider(provider)
    if pool.rate_limiter is None:
        # environment="test" containers carry the synthetic "fake" provider's
        # counters; writing them into the operator's real data dir is pollution
        # (a test container also has no data_dir of its own).
        state_file = (
            None
            if settings.environment == "test"
            else settings.resolved_data_dir / "rate_limits.json"
        )
        pool.rate_limiter = RateLimiter(state_file)
    rate_limiter = pool.rate_limiter
    rate_limiter.configure(config.providers)

    router = router or Router(config)
    policy = policy or config.retry
    scheduler = Scheduler(
        router=router,
        pool=pool,
        policy=policy,
        request_timeout=settings.request_timeout,
        max_input_tokens=settings.max_input_tokens,
        max_request_seconds=settings.max_request_seconds,
    )

    config_repository = ConfigRepository(database)
    request_repository = RequestRepository(database)
    usage_repository = UsageRepository(database)
    health_repository = HealthRepository(database)
    agent_repository = AgentRepository(database)

    usage_service = UsageService(config, usage_repository)
    health_service = HealthService(
        config=config, router=router, pool=pool, repository=health_repository
    )
    model_service = ModelService(config, router)
    request_service = RequestService(
        scheduler=scheduler,
        request_repository=request_repository,
        usage_service=usage_service,
        trim_history_tokens=settings.trim_history_tokens,
    )
    agent_service = AgentService(
        settings=settings,
        config=config,
        request_service=request_service,
        repository=agent_repository,
    )

    container = Container(
        settings=settings,
        config=config,
        database=database,
        pool=pool,
        router=router,
        scheduler=scheduler,
        rate_limiter=rate_limiter,
        request_service=request_service,
        model_service=model_service,
        usage_service=usage_service,
        health_service=health_service,
        config_repository=config_repository,
        request_repository=request_repository,
        usage_repository=usage_repository,
        health_repository=health_repository,
        agent_repository=agent_repository,
        agent_service=agent_service,
    )

    if start_services:
        await container.startup()
    return container
