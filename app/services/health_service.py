"""Health check service.

Three modes, all configurable (``ZKAI_HEALTH_CHECK_MODE``):

``manual``     only via ``POST /admin/health/check``
``startup``    one pass at boot + manual
``scheduled``  startup pass plus a background loop every ``health_check_interval``
``off``        never probes automatically

Cost control: a probe only calls ``GET /models`` (free on every supported
provider) unless ``probe_generation`` is enabled, and each provider is probed at
most once per interval. Probes never consume inference quota.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from app.core.config import AppConfig
from app.core.logging import get_logger
from app.credentials.pool import CredentialPool
from app.database.repository import HealthRepository
from app.providers.base import HealthCheckResult, ProviderAdapter
from app.routing.router import Router

logger = get_logger("services.health")


class HealthService:
    """Probe providers/credentials and feed the results back into the pool."""

    def __init__(
        self,
        *,
        config: AppConfig,
        router: Router,
        pool: CredentialPool,
        repository: HealthRepository | None = None,
    ) -> None:
        self.config = config
        self.router = router
        self.pool = pool
        self.repository = repository
        self._task: asyncio.Task | None = None
        self._last_run: float | None = None
        self._last_results: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Probing
    # ------------------------------------------------------------------ #
    async def check_provider(
        self, provider_id: str, *, kind: str = "manual", credentials: bool = True
    ) -> list[HealthCheckResult]:
        """Probe one provider (once, or once per credential)."""
        adapter: ProviderAdapter | None
        try:
            adapter = self.router.adapter(provider_id)
        except Exception as exc:
            logger.warning("health check skipped for %s: %s", provider_id, exc)
            return [
                HealthCheckResult(
                    provider_id=provider_id, ok=False, error_type="provider_unavailable",
                    detail=str(exc),
                )
            ]

        results: list[HealthCheckResult] = []
        pool_credentials = [c for c in self.pool.for_provider(provider_id) if c.is_configured()]
        # Keyless providers (e.g. Ollama) are probed once without a credential.
        targets = pool_credentials if (credentials and pool_credentials) else [None]

        for credential in targets:
            try:
                result = await asyncio.wait_for(
                    adapter.health_check(credential),
                    timeout=self.config.settings.health_check_timeout,
                )
            except TimeoutError:
                result = HealthCheckResult(
                    provider_id=provider_id,
                    credential_id=credential.id if credential else None,
                    ok=False,
                    error_type="timeout",
                    detail=f"probe exceeded {self.config.settings.health_check_timeout}s",
                )
            results.append(result)

            if credential is not None:
                self.pool.apply_health_check(
                    credential.id,
                    healthy=result.ok,
                    detail=result.detail,
                    error_type=result.error_type,
                )
            await self._persist(result, kind=kind)

        self._last_run = time.time()
        self._last_results = [r.to_dict() for r in results]
        return results

    async def _persist(self, result: HealthCheckResult, *, kind: str) -> None:
        if self.repository is None:
            return
        try:
            await self.repository.record(
                provider_id=result.provider_id,
                credential_id=result.credential_id,
                kind=kind,
                ok=result.ok,
                latency_ms=result.latency_ms,
                error_type=result.error_type,
                detail=result.detail,
            )
        except Exception:
            logger.exception("failed to persist health check for %s", result.provider_id)

    async def check_all(
        self, *, kind: str = "manual", provider_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """Probe every enabled provider (serialised to limit upstream load)."""
        async with self._lock:
            selected = provider_ids or [
                provider.id
                for provider in self.config.providers.values()
                if provider.enabled and provider.health_check_enabled
            ]
            started = time.perf_counter()
            results: list[HealthCheckResult] = []
            for provider_id in selected:
                results.extend(await self.check_provider(provider_id, kind=kind))
            duration_ms = round((time.perf_counter() - started) * 1000, 2)

            summary: dict[str, Any] = {
                "kind": kind,
                "checked": len(results),
                "ok": sum(1 for r in results if r.ok),
                "failed": sum(1 for r in results if not r.ok),
                "duration_ms": duration_ms,
                "results": [r.to_dict() for r in results],
            }
            logger.info(
                "health check (%s): %d ok / %d failed in %.0fms",
                kind,
                summary["ok"],
                summary["failed"],
                duration_ms,
            )
            self._last_run = time.time()
            self._last_results = summary["results"]
            return summary

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def startup(self) -> dict[str, Any] | None:
        """Run the startup probe when configured to do so."""
        settings = self.config.settings
        if not settings.health_check_on_startup or settings.health_check_mode in {"off", "manual"}:
            logger.info("startup health check disabled (mode=%s)", settings.health_check_mode)
            return None
        return await self.check_all(kind="startup")

    async def start_scheduler(self) -> None:
        """Start the background loop when ``health_check_mode == scheduled``."""
        settings = self.config.settings
        if settings.health_check_mode != "scheduled":
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="zkai-health-scheduler")
        logger.info(
            "scheduled health checks enabled (every %.0fs)", settings.health_check_interval
        )

    async def stop(self) -> None:
        """Stop the background loop."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        interval = max(30.0, self.config.settings.health_check_interval)
        while True:
            try:
                await asyncio.sleep(interval)
                await self.check_all(kind="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduled health check failed")

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        """Aggregate pool + last-run state for ``/health`` and ``/admin/health``."""
        pool_stats = self.pool.stats()
        availability = self.pool.provider_availability()
        providers: dict[str, Any] = {}
        for provider in self.config.providers.values():
            credentials = self.pool.for_provider(provider.id)
            providers[provider.id] = {
                "type": provider.type.value,
                "enabled": provider.enabled,
                "base_url": provider.base_url,
                "available": availability.get(provider.id, False),
                "credentials_total": len(credentials),
                "credentials_usable": sum(1 for c in credentials if c.is_usable()),
                "credentials_configured": sum(1 for c in credentials if c.is_configured()),
                "last_results": [
                    item
                    for item in self._last_results
                    if item.get("provider_id") == provider.id
                ][:5],
            }
        return {
            "summary": {
                "providers": len(self.config.providers),
                "providers_available": sum(1 for value in availability.values() if value),
                "models": len(self.config.models),
                "credentials": pool_stats["total"],
                "credentials_usable": pool_stats["usable"],
                "last_check_at": self._last_run,
                "mode": self.config.settings.health_check_mode,
            },
            "credentials": pool_stats,
            "providers": providers,
        }
