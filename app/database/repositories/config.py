"""ConfigRepository: mirror the YAML configuration into the database.

Handles ``sync_config`` (the startup mirror), credential-runtime persistence,
and the console CRUD for providers / models / aliases / rate-limit overrides.
"""

from __future__ import annotations

from app.core.config import AppConfig
from app.database.repositories._common import (
    Any,
    Credential,
    CredentialRuntime,
    CursorResult,  # noqa: F401  (used in cast() string annotations for mypy)
    Database,
    Deployment,
    Model,
    ModelAlias,
    Provider,
    cast,
    delete,
    logger,
    select,
    to_datetime,
)


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
