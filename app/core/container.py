"""Application container: the single place where the object graph is assembled.

Everything is created once at startup and injected through ``app.state``, which
keeps the API layer free of global state and makes tests able to build a fully
wired container with fake providers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import AppConfig, Settings, load_app_config
from app.core.logging import get_logger
from app.credentials.cooldown import CooldownPolicy
from app.credentials.pool import CredentialPool
from app.database.db import Database
from app.database.repository import (
    ConfigRepository,
    HealthRepository,
    RequestRepository,
    UsageRepository,
)
from app.retry.policy import RetryPolicy
from app.routing.limits import RateLimiter
from app.routing.router import Router
from app.routing.scheduler import Scheduler
from app.services.health_service import HealthService
from app.services.model_service import ModelService
from app.services.request_service import RequestService
from app.services.usage_service import UsageService

logger = get_logger("container")


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
    started_at: float = field(default_factory=time.time)
    ready: bool = False

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
        self.rate_limiter.flush(force=True)
        await self.health_service.stop()
        await self.router.aclose()
        await self.database.dispose()
        logger.info("ZK-AI stopped")

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
        pool.rate_limiter = RateLimiter(settings.resolved_data_dir / "rate_limits.json")
    rate_limiter = pool.rate_limiter
    rate_limiter.configure(config.providers)

    router = router or Router(config)
    policy = policy or config.retry
    scheduler = Scheduler(
        router=router,
        pool=pool,
        policy=policy,
        request_timeout=settings.request_timeout,
    )

    config_repository = ConfigRepository(database)
    request_repository = RequestRepository(database)
    usage_repository = UsageRepository(database)
    health_repository = HealthRepository(database)

    usage_service = UsageService(config, usage_repository)
    health_service = HealthService(
        config=config, router=router, pool=pool, repository=health_repository
    )
    model_service = ModelService(config, router)
    request_service = RequestService(
        scheduler=scheduler,
        request_repository=request_repository,
        usage_service=usage_service,
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
    )

    if start_services:
        await container.startup()
    return container
