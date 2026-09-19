"""Repositories: all database access lives here.

Every repository takes the :class:`~app.database.db.Database` and opens a short
transactional session per operation. Nothing in this module knows about
credentials: only ``credential_id`` strings and non-reversible fingerprints are
ever persisted.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from sqlalchemy import CursorResult, case, delete, func, select, update

from app.core.config import AppConfig
from app.core.logging import get_logger
from app.database.db import Database
from app.database.models import (
    AgentMessage,
    AgentSession,
    Credential,
    Deployment,
    HealthCheck,
    Model,
    ModelAlias,
    Provider,
    RequestAttempt,
    RequestRecord,
    UsageRecord,
    to_datetime,
    utcnow,
)
from app.models.credential import CredentialRuntime
from app.models.response import AttemptOutcome

logger = get_logger("database.repository")


class ConfigRepository:
    """Mirror the YAML configuration into the database for querying/joins."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def sync_config(self, config: AppConfig) -> dict[str, int]:
        """Upsert providers / models / credentials / deployments / aliases."""
        counts = {"providers": 0, "models": 0, "credentials": 0, "deployments": 0, "aliases": 0}
        async with self.db.session() as session:
            for provider in config.providers.values():
                provider_row = await session.get(Provider, provider.id)
                if provider_row is None:
                    provider_row = Provider(id=provider.id)
                    session.add(provider_row)
                provider_row.type = provider.type.value
                provider_row.base_url = provider.base_url
                provider_row.enabled = provider.enabled
                provider_row.timeout = provider.timeout
                provider_row.max_retries = provider.max_retries
                preserved_source = (provider_row.extra or {}).get("rate_limits_source")
                if preserved_source == "console":
                    # sync runs *before* overrides are re-applied at startup: a plain
                    # mirror would wipe the console rules we are about to read back.
                    prior = ((provider_row.extra or {}).get("options") or {}).get("rate_limits")
                    merged_options = dict(provider.options)
                    if isinstance(prior, list):
                        merged_options["rate_limits"] = prior
                else:
                    merged_options = provider.options
                provider_row.extra = {
                    "api_version": provider.api_version,
                    "options": merged_options,
                    "referer": provider.referer,
                    "app_title": provider.app_title,
                    "rate_limits_source": preserved_source,
                }
                counts["providers"] += 1

                for credential in provider.credentials:
                    cred_row = await session.get(Credential, credential.id)
                    if cred_row is None:
                        cred_row = Credential(id=credential.id)
                        session.add(cred_row)
                    cred_row.provider_id = provider.id
                    cred_row.enabled = credential.enabled
                    cred_row.priority = credential.priority
                    cred_row.weight = credential.weight
                    cred_row.secret_ref = credential.env_reference()
                    cred_row.tags = list(credential.tags)
                    counts["credentials"] += 1

            for model in config.models.values():
                model_row = await session.get(Model, model.id)
                if model_row is None:
                    model_row = Model(id=model.id)
                    session.add(model_row)
                model_row.display_name = model.display_name
                model_row.owned_by = model.owned_by
                model_row.description = model.description
                model_row.enabled = model.enabled
                model_row.context_window = model.context_window
                model_row.capabilities = model.capabilities.as_dict()
                counts["models"] += 1

                for deployment in model.deployments:
                    dep_row = await session.get(Deployment, deployment.id)
                    if dep_row is None:
                        dep_row = Deployment(id=deployment.id)
                        session.add(dep_row)
                    dep_row.model_id = model.id
                    dep_row.provider_id = deployment.provider_id
                    dep_row.upstream_model = deployment.model
                    dep_row.enabled = deployment.enabled
                    dep_row.priority = deployment.priority
                    dep_row.weight = deployment.weight
                    dep_row.context_window = deployment.context_window
                    dep_row.max_output_tokens = deployment.max_output_tokens
                    dep_row.capabilities = deployment.capabilities
                    dep_row.input_cost_per_mtok = deployment.input_cost_per_mtok
                    dep_row.output_cost_per_mtok = deployment.output_cost_per_mtok
                    dep_row.tags = list(deployment.tags)
                    counts["deployments"] += 1

            for alias in config.aliases.values():
                alias_row = await session.get(ModelAlias, alias.name)
                if alias_row is None:
                    alias_row = ModelAlias(name=alias.name)
                    session.add(alias_row)
                alias_row.targets = list(alias.targets)
                alias_row.strategy = alias.strategy.value
                alias_row.enabled = alias.enabled
                alias_row.requires = dict(alias.requires)
                alias_row.weights = dict(alias.weights)
                alias_row.fallback_to_local = alias.fallback_to_local
                alias_row.description = alias.description
                counts["aliases"] += 1

            # Mirror must be a *sync*, not only an upsert: rows removed from the
            # YAML (a key rotated out, a provider retired) must disappear from the
            # mirror too, otherwise the console and the live config drift apart.
            kept_providers = set(config.providers)
            kept_credentials = {
                credential.id for provider in config.providers.values()
                for credential in provider.credentials
            }
            kept_models = set(config.models)
            kept_deployments = {
                deployment.id for model in config.models.values()
                for deployment in model.deployments
            }
            kept_aliases = set(config.aliases)
            removed = 0
            for table, keep, key_attr in (
                (Deployment, kept_deployments, Deployment.id),
                (Credential, kept_credentials, Credential.id),
                (ModelAlias, kept_aliases, ModelAlias.name),
                (Model, kept_models, Model.id),
                (Provider, kept_providers, Provider.id),
            ):
                if not keep:
                    continue  # empty config should never wipe a whole table
                result = await session.execute(delete(table).where(key_attr.notin_(keep)))
                removed += int(cast("CursorResult[Any]", result).rowcount or 0)
            if removed:
                counts["removed"] = removed
        logger.info("configuration mirrored to database: %s", counts)
        return counts

    async def mirror_credential_runtime(self, credentials: list[CredentialRuntime]) -> None:
        """Persist live credential counters (never secrets)."""
        async with self.db.session() as session:
            for runtime in credentials:
                row = await session.get(Credential, runtime.id)
                if row is None:
                    continue
                row.status = runtime.status.value
                row.enabled = runtime.enabled
                row.success_count = runtime.success_count
                row.failure_count = runtime.failure_count
                row.rate_limit_count = runtime.rate_limit_count
                row.secret_fingerprint = runtime.fingerprint()
                row.last_used_at = to_datetime(runtime.last_used_at)
                row.last_success_at = to_datetime(runtime.last_success_at)
                row.last_error_at = to_datetime(runtime.last_error_at)
                row.last_error_type = runtime.last_error_type
                row.cooldown_until = to_datetime(runtime.cooldown_until)
                row.disabled_reason = runtime.disabled_reason

    async def list_providers(self) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(select(Provider).order_by(Provider.id))
            return [
                {
                    "id": row.id,
                    "type": row.type,
                    "base_url": row.base_url,
                    "enabled": row.enabled,
                    "timeout": row.timeout,
                    "max_retries": row.max_retries,
                    "extra": row.extra,
                }
                for row in result.scalars()
            ]

    async def list_credentials(self) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(select(Credential).order_by(Credential.id))
            return [
                {
                    "id": row.id,
                    "provider_id": row.provider_id,
                    "status": row.status,
                    "enabled": row.enabled,
                    "priority": row.priority,
                    "weight": row.weight,
                    "secret_ref": row.secret_ref,
                    "fingerprint": row.secret_fingerprint,
                    "success_count": row.success_count,
                    "failure_count": row.failure_count,
                    "rate_limit_count": row.rate_limit_count,
                    "last_used_at": row.last_used_at,
                    "cooldown_until": row.cooldown_until,
                    "disabled_reason": row.disabled_reason,
                }
                for row in result.scalars()
            ]

    async def list_models(self) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(select(Model).order_by(Model.id))
            return [
                {
                    "id": row.id,
                    "enabled": row.enabled,
                    "context_window": row.context_window,
                    "capabilities": row.capabilities,
                }
                for row in result.scalars()
            ]

    async def list_aliases(self) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(select(ModelAlias).order_by(ModelAlias.name))
            return [
                {
                    "name": row.name,
                    "targets": row.targets,
                    "strategy": row.strategy,
                    "enabled": row.enabled,
                    "requires": row.requires,
                    "weights": row.weights,
                }
                for row in result.scalars()
            ]

    async def upsert_alias(
        self,
        name: str,
        *,
        targets: list[str],
        strategy: str = "capability",
        enabled: bool = True,
        weights: dict[str, float] | None = None,
        requires: dict[str, float] | None = None,
        description: str | None = None,
    ) -> None:
        async with self.db.session() as session:
            row = await session.get(ModelAlias, name)
            if row is None:
                row = ModelAlias(name=name)
                session.add(row)
            row.targets = list(targets)
            row.strategy = strategy
            row.enabled = enabled
            row.weights = dict(weights or {})
            row.requires = dict(requires or {})
            row.description = description

    async def upsert_model(self, model: Any) -> None:
        """Persist a runtime-created/edited model (ModelConfig) and its deployments.

        Mirrors ``upsert_alias``: the row stays in the DB across restarts and is
        re-applied on top of the YAML by :meth:`model_overrides`.
        """
        async with self.db.session() as session:
            row = await session.get(Model, model.id)
            if row is None:
                row = Model(id=model.id)
                session.add(row)
            row.display_name = model.display_name
            row.owned_by = model.owned_by
            row.description = model.description
            row.enabled = model.enabled
            row.context_window = model.context_window
            row.capabilities = model.capabilities.as_dict()

            kept: set[str] = set()
            for deployment in model.deployments:
                dep_row = await session.get(Deployment, deployment.id)
                if dep_row is None:
                    dep_row = Deployment(id=deployment.id)
                    session.add(dep_row)
                dep_row.model_id = model.id
                dep_row.provider_id = deployment.provider_id
                dep_row.upstream_model = deployment.model
                dep_row.enabled = deployment.enabled
                dep_row.priority = deployment.priority
                dep_row.weight = deployment.weight
                dep_row.context_window = deployment.context_window
                dep_row.max_output_tokens = deployment.max_output_tokens
                dep_row.capabilities = dict(deployment.capabilities)
                dep_row.input_cost_per_mtok = deployment.input_cost_per_mtok
                dep_row.output_cost_per_mtok = deployment.output_cost_per_mtok
                dep_row.tags = list(deployment.tags)
                kept.add(deployment.id)
            if kept:
                await session.execute(
                    delete(Deployment).where(
                        Deployment.model_id == model.id, Deployment.id.notin_(kept)
                    )
                )

    async def delete_model(self, model_id: str) -> bool:
        async with self.db.session() as session:
            result = await session.execute(delete(Model).where(Model.id == model_id))
            return bool(cast("CursorResult[Any]", result).rowcount or 0)

    async def delete_alias(self, name: str) -> bool:
        async with self.db.session() as session:
            result = await session.execute(delete(ModelAlias).where(ModelAlias.name == name))
            return bool(cast("CursorResult[Any]", result).rowcount or 0)

    async def set_provider_rate_limits(self, provider_id: str, rules: list[dict[str, Any]]) -> bool:
        """Persist console-edited quota rules; they shadow YAML until reset."""
        async with self.db.session() as session:
            row = await session.get(Provider, provider_id)
            if row is None:
                # Not mirrored yet (e.g. config was never synced): create the row.
                row = Provider(id=provider_id, type="openai", base_url="", enabled=True)
                session.add(row)
            extra = dict(row.extra or {})
            options = dict(extra.get("options") or {})
            options["rate_limits"] = list(rules)
            extra["options"] = options
            extra["rate_limits_source"] = "console"
            row.extra = extra
            return True

    async def clear_provider_rate_limits(
        self, provider_id: str, yaml_rules: list[dict[str, Any]]
    ) -> bool:
        """Drop the console flag and store the YAML value back (reset path)."""
        async with self.db.session() as session:
            row = await session.get(Provider, provider_id)
            if row is None:
                return False
            extra = dict(row.extra or {})
            options = dict(extra.get("options") or {})
            options["rate_limits"] = list(yaml_rules)
            extra["options"] = options
            extra.pop("rate_limits_source", None)
            row.extra = extra
            return True

    async def provider_rate_limit_overrides(self) -> dict[str, list[dict[str, Any]]]:
        """Providers whose quota rules were set through the console."""
        async with self.db.session() as session:
            result = await session.execute(select(Provider))
            overrides: dict[str, list[dict[str, Any]]] = {}
            for row in result.scalars():
                extra = row.extra or {}
                if extra.get("rate_limits_source") == "console":
                    rules = (extra.get("options") or {}).get("rate_limits")
                    if isinstance(rules, list):
                        overrides[row.id] = rules
            return overrides

    async def set_credential_enabled(
        self, credential_id: str, enabled: bool, reason: str | None = None
    ) -> bool:
        async with self.db.session() as session:
            row = await session.get(Credential, credential_id)
            if row is None:
                return False
            row.enabled = enabled
            row.status = "healthy" if enabled else "disabled"
            row.disabled_reason = reason
            return True


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
        """Delete request history older than *days* (returns rows removed)."""
        cutoff = utcnow() - dt.timedelta(days=days)
        async with self.db.session() as session:
            result = await session.execute(
                delete(RequestRecord).where(RequestRecord.started_at < cutoff)
            )
            # ``execute`` of a DELETE returns a CursorResult, which carries rowcount.
            return int(cast("CursorResult[Any]", result).rowcount or 0)


class UsageRepository:
    """Token / cost accounting."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(
        self,
        *,
        request_id: str | None,
        provider_id: str,
        model: str,
        credential_id: str | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                UsageRecord(
                    request_id=request_id,
                    provider_id=provider_id,
                    model=model,
                    credential_id=credential_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                )
            )

    async def backfill_costs(
        self,
        prices: dict[tuple[str, str], tuple[float, float]],
        fallback: dict[str, tuple[float, float]],
    ) -> dict[str, Any]:
        """Reprice rows with ``cost_usd`` 0/NULL from (model, provider) prices.

        Mirrors ``scripts/backfill_cost.py`` so the console can do it with a
        button. ``prices`` keys are (model_id, provider_id); ``fallback`` keys
        are model_id (first priced deployment). Only zero rows are touched.
        """
        updated = 0
        total = 0.0
        async with self.db.session() as session:
            result = await session.execute(
                select(UsageRecord).where(
                    (UsageRecord.cost_usd.is_(None)) | (UsageRecord.cost_usd == 0)
                )
            )
            for row in result.scalars():
                pair = prices.get((row.model, row.provider_id)) or fallback.get(row.model)
                if pair is None or not (pair[0] or pair[1]):
                    continue
                cost = round(
                    (row.input_tokens * pair[0] + row.output_tokens * pair[1]) / 1_000_000, 8
                )
                if abs(cost) > 1e-12:
                    row.cost_usd = cost
                    updated += 1
                    total += cost
        if updated:
            logger.info("backfilled cost on %d usage row(s), total $%.4f", updated, total)
        return {"updated": updated, "total_cost_usd": round(total, 6)}

    async def summary(self, *, days: int = 7) -> dict[str, Any]:
        cutoff = utcnow() - dt.timedelta(days=days)
        async with self.db.session() as session:
            totals = await session.execute(
                select(
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.input_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.output_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
                ).where(UsageRecord.created_at >= cutoff)
            )
            count, input_tokens, output_tokens, total_tokens, cost = totals.one()

            by_provider = await session.execute(
                select(
                    UsageRecord.provider_id,
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
                )
                .where(UsageRecord.created_at >= cutoff)
                .group_by(UsageRecord.provider_id)
            )
            by_model = await session.execute(
                select(
                    UsageRecord.model,
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
                )
                .where(UsageRecord.created_at >= cutoff)
                .group_by(UsageRecord.model)
                .order_by(func.count(UsageRecord.id).desc())
                .limit(20)
            )
            by_credential = await session.execute(
                select(
                    UsageRecord.credential_id,
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
                )
                .where(UsageRecord.created_at >= cutoff)
                .group_by(UsageRecord.credential_id)
            )
            return {
                "window_days": days,
                "requests": int(count or 0),
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
                "total_tokens": int(total_tokens or 0),
                "cost_usd": round(float(cost or 0.0), 6),
                "by_provider": [
                    {
                        "provider": row[0],
                        "requests": int(row[1]),
                        "tokens": int(row[2]),
                        "cost_usd": round(float(row[3]), 6),
                    }
                    for row in by_provider
                ],
                "top_models": [
                    {
                        "model": row[0],
                        "requests": int(row[1]),
                        "tokens": int(row[2]),
                        "cost_usd": round(float(row[3]), 6),
                    }
                    for row in by_model
                ],
                "by_credential": [
                    {
                        "credential_id": row[0],
                        "requests": int(row[1]),
                        "tokens": int(row[2]),
                        "cost_usd": round(float(row[3]), 6),
                    }
                    for row in by_credential
                ],
            }

    async def daily(self, *, days: int = 14) -> list[dict[str, Any]]:
        cutoff = utcnow() - dt.timedelta(days=days)
        async with self.db.session() as session:
            result = await session.execute(
                select(
                    func.date(UsageRecord.created_at).label("day"),
                    func.count(UsageRecord.id),
                    func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                )
                .where(UsageRecord.created_at >= cutoff)
                .group_by("day")
                .order_by("day")
            )
            return [
                {"day": str(row[0]), "requests": int(row[1]), "tokens": int(row[2])}
                for row in result
            ]


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


class AgentRepository:
    """ZK-Agent sessions and their transcript rows (LLM replay history).

    Concurrency note: the agent loop, console pollers and SSE snapshots hit
    these rows simultaneously. Transactions are serialized process-wide by
    ``Database.session()`` (one pooled aiosqlite connection per engine, where
    overlapping sessions corrupt each other's implicit transaction), so no
    per-repository locking is needed here.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_session(
        self,
        *,
        session_id: str,
        title: str,
        model: str,
        workspace: str,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                AgentSession(id=session_id, title=title, model=model, workspace=workspace)
            )
            await session.commit()

    async def list_sessions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(AgentSession).order_by(AgentSession.updated_at.desc()).limit(limit)
            )
            return [self._session_dict(row) for row in result.scalars()]

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.get(AgentSession, session_id)
            return self._session_dict(row) if row else None

    async def update_status(
        self,
        session_id: str,
        status: str,
        *,
        error: str | None = None,
        steps: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        title: str | None = None,
    ) -> None:
        async with self.db.session() as session:
            row = await session.get(AgentSession, session_id)
            if row is None:
                return
            row.status = status
            if error is not None:
                row.error = error
            if steps is not None:
                row.steps = steps
            if input_tokens is not None:
                row.input_tokens = input_tokens
            if output_tokens is not None:
                row.output_tokens = output_tokens
            if title is not None:
                row.title = title[:200]
            await session.commit()

    async def delete_session(self, session_id: str) -> bool:
        async with self.db.session() as session:
            row = await session.get(AgentSession, session_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def running_session_ids(self) -> list[str]:
        async with self.db.session() as session:
            result = await session.execute(
                select(AgentSession.id).where(
                    AgentSession.status.in_(("running", "waiting_approval"))
                )
            )
            return list(result.scalars())

    # ---- messages --------------------------------------------------------- #

    async def add_message(
        self,
        session_id: str,
        *,
        role: str,
        kind: str,
        content: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        """Append one transcript row; returns its seq number."""
        async with self.db.session() as session:
            seq_row = await session.execute(
                select(func.max(AgentMessage.seq)).where(
                    AgentMessage.session_id == session_id
                )
            )
            seq = int(seq_row.scalar() or 0) + 1
            session.add(
                AgentMessage(
                    session_id=session_id,
                    seq=seq,
                    role=role,
                    kind=kind,
                    content=content,
                    data=data,
                )
            )
            await session.commit()
            return seq

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(AgentMessage)
                .where(AgentMessage.session_id == session_id)
                .order_by(AgentMessage.seq)
            )
            return [
                {
                    "seq": row.seq,
                    "role": row.role,
                    "kind": row.kind,
                    "content": row.content,
                    "data": row.data,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in result.scalars()
            ]

    @staticmethod
    def _session_dict(row: AgentSession) -> dict[str, Any]:
        return {
            "id": row.id,
            "title": row.title,
            "model": row.model,
            "workspace": row.workspace,
            "status": row.status,
            "steps": row.steps,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


__all__ = [
    "AgentRepository",
    "ConfigRepository",
    "HealthRepository",
    "RequestRepository",
    "UsageRepository",
]
