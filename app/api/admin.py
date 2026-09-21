"""Administration API (``/admin/*``).

Everything an operator needs: inspect providers/models/credentials, force health
checks, enable/disable keys, explain routing decisions and read statistics.

Protected by ``ZKAI_ADMIN_TOKEN`` when configured (see :func:`app.api.deps.require_admin`).
Credential responses never contain secret material - only a masked fingerprint.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, require_admin
from app.core.config import PROJECT_ROOT, apply_db_overrides, load_app_config
from app.core.config_writer import (
    append_credential_to_provider,
    delete_credential_from_provider,
    delete_list_entry,
    looks_like_secret,
    sync_provider_rate_limits,
    upsert_env_var,
    upsert_list_entry,
)
from app.core.config_writer import delete_provider as delete_provider_file
from app.core.config_writer import upsert_provider as upsert_provider_file
from app.core.logging import get_logger
from app.models.provider import (
    AliasStrategy,
    DeploymentConfig,
    ModelAliasConfig,
    ModelConfig,
)
from app.models.request import ChatCompletionRequest, ChatMessage
from app.routing.aliases import AliasRegistry
from app.routing.limits import RateLimitRule

logger = get_logger("api.admin")

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# --------------------------------------------------------------------------- #
# YAML write-through helpers (console edits must update the real files so a
# reload/restart can never resurrect entries the operator deleted)
# --------------------------------------------------------------------------- #
def _strip_nulls(data: Any) -> Any:
    """Drop ``None`` fields (recursively) for tidy YAML output."""
    if isinstance(data, dict):
        return {k: _strip_nulls(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [_strip_nulls(item) for item in data]
    return data


def _source_file(container: ContainerDep, stem: str) -> Path | None:
    """The real YAML the gateway loaded for *stem*; None = template-only setups.

    ``.example.yaml`` files are committable templates and are never rewritten;
    in that case the DB override keeps the change alive and the warning surfaces.
    """
    name = container.config.source_files.get(stem)
    if not name or ".example." in name:
        return None
    path = container.settings.resolved_config_dir / name
    return path if path.suffix in {".yaml", ".yml"} else None


def _file_write_failed(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "error": {
                "message": f"配置文件写入失败，本次改动已取消（避免界面与文件不一致）：{exc}",
                "type": "config_write_failed",
            }
        },
    )


def _write_model_file(container: ContainerDep, model: ModelConfig) -> str | None:
    path = _source_file(container, "models") or _source_file(container, "config")
    if path is None:
        return None
    try:
        upsert_list_entry(
            path, "models", "id", model.id, _strip_nulls(model.model_dump(mode="json"))
        )
    except (OSError, ValueError) as exc:
        raise _file_write_failed(exc) from exc
    return path.name


def _write_alias_file(container: ContainerDep, alias: ModelAliasConfig) -> str | None:
    path = _source_file(container, "models") or _source_file(container, "config")
    if path is None:
        return None
    try:
        upsert_list_entry(
            path, "aliases", "name", alias.name, _strip_nulls(alias.model_dump(mode="json"))
        )
    except (OSError, ValueError) as exc:
        raise _file_write_failed(exc) from exc
    return path.name


def _delete_file_entry(container: ContainerDep, section: str, key: str, name: str) -> str | None:
    path = _source_file(container, "models") or _source_file(container, "config")
    if path is None:
        return None
    try:
        delete_list_entry(path, section, key, name)
    except (OSError, ValueError) as exc:
        raise _file_write_failed(exc) from exc
    return path.name


def _write_limits_file(container: ContainerDep, provider_id: str, rules: list[dict]) -> str | None:
    path = _source_file(container, "providers")
    if path is None:
        return None
    try:
        sync_provider_rate_limits(path, provider_id, rules)
    except (OSError, ValueError, KeyError) as exc:
        raise _file_write_failed(exc) from exc
    return path.name


# --------------------------------------------------------------------------- #
# Catalogue views
# --------------------------------------------------------------------------- #
@router.get("/providers", summary="List providers")
async def list_providers(container: ContainerDep) -> dict[str, Any]:
    """Configured providers, their credentials and live availability."""
    availability = container.pool.provider_availability()
    console_limits = await container.config_repository.provider_rate_limit_overrides()
    providers: list[dict[str, Any]] = []
    for provider in container.config.providers.values():
        credentials = container.pool.for_provider(provider.id)
        providers.append(
            {
                "id": provider.id,
                "type": provider.type.value,
                "base_url": provider.base_url,
                "enabled": provider.enabled,
                "requires_credential": provider.requires_credential,
                "timeout": provider.timeout,
                "max_retries": provider.max_retries,
                "available": availability.get(provider.id, False),
                "rate_limits": list(provider.options.get("rate_limits") or []),
                "rate_limits_source": "console" if provider.id in console_limits else "yaml",
                "credentials": [c.snapshot() for c in credentials],
                "models": sorted(
                    {
                        deployment.model
                        for model in container.config.models.values()
                        for deployment in model.deployments
                        if deployment.provider_id == provider.id
                    }
                ),
            }
        )
    return {"object": "list", "data": providers}


@router.get("/providers/{provider_id}/models", summary="List a provider's upstream models")
async def provider_models(provider_id: str, container: ContainerDep) -> dict[str, Any]:
    """Probe a provider's native model list and match each id against the curated presets.

    Feeds the model marketplace: the console can show "which models this provider
    actually has" with pre-filled context/capability/price, so adding a model is a
    pick-and-save instead of filling a form from scratch. Providers without a
    credential (moonshot with no key) return an empty list with an explanatory note.
    """
    provider = container.config.providers.get(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"provider '{provider_id}' not found"}},
        )
    if not provider.enabled:
        return {
            "object": "list",
            "provider_id": provider_id,
            "data": [],
            "note": "该供应商已在配置里禁用（enabled: false）——启用后才能探测模型",
        }
    credential = next(iter(container.pool.for_provider(provider_id)), None)
    if credential is None and provider.requires_credential:
        return {
            "object": "list",
            "provider_id": provider_id,
            "data": [],
            "note": "该供应商还没有任何 Key——先去「凭据池 → ➕ 加 Key」",
        }
    if credential is not None and credential.secret_source == "missing":  # noqa: S105 - source label
        return {
            "object": "list",
            "provider_id": provider_id,
            "data": [],
            "note": (
                f"该供应商的 Key 没有实际值（{credential.secret_ref} 在 .env 里是空的）"
                "——去「凭据池 → ➕ 加 Key」把值填上"
            ),
        }
    adapter = container.router.adapter(provider_id)
    try:
        catalogue = await adapter.model_catalogue(credential)
    except Exception as exc:
        # A probe failure points at the upstream, not the gateway: surface it as
        # a note in the same shape so the console keeps rendering one panel.
        logger.info("model probe failed for %s: %s", provider_id, exc)
        return {
            "object": "list",
            "provider_id": provider_id,
            "data": [],
            "note": f"探测失败：{exc}（上游/网络问题，不是网关坏了）",
        }
    known_models = set(container.config.models)
    known_upstream = {
        deployment.model
        for model in container.config.models.values()
        for deployment in model.deployments
    }
    from app.models.discovery import model_facts
    from app.models.presets import preset_for

    data = []
    for upstream in sorted(catalogue):
        preset = preset_for(upstream)
        # Measured facts outrank the hand-written preset for the fields the
        # provider actually reported; anything it stayed silent about keeps the
        # preset value, and ``fallback`` records where each number came from so
        # the console can label it instead of presenting a guess as fact.
        facts = model_facts(catalogue[upstream])
        capabilities = dict(preset.capabilities)
        if facts.vision_input is not None:
            capabilities["vision"] = 10.0 if facts.vision_input else 0.0
        if facts.reasoning is not None:
            capabilities["reasoning"] = max(capabilities.get("reasoning", 0.0), 8.0)
        entry: dict[str, Any] = {
            "upstream_model": upstream,
            "suggested_model_id": _suggest_model_id(upstream),
            "already_added": preset.family in known_models
            or upstream in known_upstream,
            "preset": {
                "family": preset.family,
                "display_name": preset.display_name,
                "context_window": preset.context_window,
                "capabilities": capabilities,
                "input_price": preset.input_price,
                "output_price": preset.output_price,
                "description": preset.description,
            },
            "discovered": facts.as_dict(),
            "discovered_known": facts.known,
            # Higher-fidelity-vendor presets are only a guess for StepFun's own
            # models; anything else keeps the preset context window.
            "context_window_source": "provider" if facts.context_window else "preset",
        }
        if facts.known:
            entry["facts_note"] = facts.describe()
        data.append(entry)
    return {"object": "list", "provider_id": provider_id, "data": data}


def _suggest_model_id(upstream: str) -> str:
    """A gateway-facing id from an upstream name: ``z-ai/glm-5.3`` -> ``glm-5.3``."""
    return upstream.split("/")[-1].strip()


@router.get("/models", summary="List models with capabilities")
async def list_models(container: ContainerDep) -> dict[str, Any]:
    """Models plus their deployments, capabilities and aliases."""
    models = [
        container.model_service.describe(model_id)
        for model_id in container.config.public_model_ids()
    ]
    return {
        "object": "list",
        "data": [model for model in models if model],
        "aliases": container.model_service.alias_table(),
        "config_warnings": container.config.warnings,
    }


@router.get("/credentials", summary="List credentials (secrets masked)")
async def list_credentials(
    container: ContainerDep,
    provider_id: str | None = Query(default=None, description="Filter by provider"),
) -> dict[str, Any]:
    """Credential pool snapshot: status, counters, cooldowns, masked fingerprints.

    Also surfaces active conversation-affinity bindings (session keys are hashed,
    never leaked) so the console can show which key each live session is pinned to.
    """
    bindings = {
        provider: container.pool.affinity_bindings_for_provider(provider)
        for provider in container.config.providers
    }
    return {
        "object": "list",
        "data": container.pool.snapshot(provider_id),
        "stats": container.pool.stats(provider_id),
        "affinity": {
            "enabled": container.pool.affinity_enabled,
            "ttl_seconds": container.pool.affinity_ttl,
            "bindings": bindings,
        },
    }


@router.get("/aliases", summary="List model aliases")
async def list_aliases(container: ContainerDep) -> dict[str, Any]:
    return {
        "object": "list",
        "data": container.router.aliases.describe(),
        "resolved": {
            name: container.config.resolve_model_ids(name)
            for name in container.router.aliases.names()
        },
    }


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@router.get("/health", summary="Detailed health report")
async def health_report(container: ContainerDep) -> dict[str, Any]:
    """Pool state, provider availability and recent probe history."""
    report = container.health_service.status()
    report["recent_checks"] = await container.health_repository.recent(limit=20)
    report["error_rates"] = await container.health_repository.error_rate(minutes=15)
    report["database"] = {"ok": await container.database.health(), "url": container.database.url}
    report["config"] = container.config.describe()
    return report


class HealthCheckRequest(BaseModel):
    """Body for a manual health check."""

    providers: list[str] | None = Field(
        default=None, description="Provider ids to probe; omit for all enabled providers"
    )
    credentials: bool = Field(default=True, description="Probe each credential separately")


@router.post("/health/check", summary="Run a health check now")
async def run_health_check(
    container: ContainerDep, payload: HealthCheckRequest | None = Body(default=None)
) -> dict[str, Any]:
    """Manual probe (mode ``manual`` uses this endpoint)."""
    request = payload or HealthCheckRequest()
    return await container.health_service.check_all(
        kind="manual", provider_ids=request.providers
    )


class ProviderLimitsRequest(BaseModel):
    """Body for setting a provider's proactive quota rules from the console."""

    rules: list[dict[str, Any]] = Field(default_factory=list)


@router.put("/providers/{provider_id}/limits", summary="Set a provider's quota rules")
async def set_provider_limits(
    provider_id: str, payload: ProviderLimitsRequest, container: ContainerDep
) -> dict[str, Any]:
    """Replace a provider's sliding-window quotas at runtime; persists across restarts.

    Empty ``rules`` means "unlimited" and still shadows the YAML until it is reset
    with ``DELETE``.
    """
    provider = container.config.providers.get(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"provider '{provider_id}' not found"}},
        )
    cleaned: list[dict[str, Any]] = []
    for entry in payload.rules:
        rule = RateLimitRule.from_mapping(entry)
        if rule is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "message": f"invalid rule: {entry!r} (need window_seconds and "
                        "at least one of max_requests / max_tokens; scope in "
                        "credential|account|provider)",
                        "type": "invalid_rate_limit",
                    }
                },
            )
        cleaned.append(rule.as_mapping())
    synced = _write_limits_file(container, provider_id, cleaned)
    provider.options = {**provider.options, "rate_limits": cleaned}
    container.rate_limiter.register_provider(provider)
    if synced is not None:
        # The YAML now holds the value: drop any legacy console shadow so a later
        # hand-edit of providers.yaml stays authoritative.
        await container.config_repository.clear_provider_rate_limits(provider_id, cleaned)
    else:
        await container.config_repository.set_provider_rate_limits(provider_id, cleaned)
    logger.info("provider %s quota rules set to %s", provider_id, cleaned)
    return {
        "object": "rate_limits",
        "provider_id": provider_id,
        "rules": cleaned,
        "synced": synced,
    }


class AddCredentialRequest(BaseModel):
    """Body for adding a credential to a provider at runtime."""

    id: str
    env_var: str | None = None  # Name of the env var that holds the key (never the key)
    value: str | None = None  # The key itself; with write_env it lands in .env
    #: Write ``value`` into ``.env`` under ``env_var`` instead of inlining it in
    #: providers.yaml. The recommended path - the YAML only ever holds the name.
    write_env: bool = False
    priority: int = 100
    enabled: bool = True


def _env_file() -> Path:
    """The project ``.env`` (secrets live here, never in the YAML)."""
    return PROJECT_ROOT / ".env"


@router.post("/providers/{provider_id}/credentials", summary="Add a credential to a provider")
async def add_credential(
    provider_id: str, payload: AddCredentialRequest, container: ContainerDep
) -> dict[str, Any]:
    """Append a credential to a provider's pool at runtime; persists across restarts.

    With ``write_env=true`` the secret is written to ``.env`` under ``env_var`` and
    only that *name* goes into ``providers.yaml``; the key is also exported into
    this process so the credential works immediately, without a reload. Without it
    the legacy inline path applies (``value`` lands in the gitignored YAML),
    which is development-only.
    """
    provider = container.config.providers.get(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"provider '{provider_id}' not found"}},
        )
    if looks_like_secret(payload.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": (
                        "credential id looks like an API key - use a short name such as "
                        f"'{provider_id}-01' for the id, and put the key in the value field"
                    ),
                    "type": "secret_in_id",
                }
            },
        )
    if payload.env_var is None and payload.value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "either env_var or value must be set (env_var preferred - "
                    "store the key in .env and reference its name here)",
                    "type": "invalid_credential",
                }
            },
        )
    if payload.write_env and not payload.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "write_env needs the key itself in 'value'",
                    "type": "invalid_credential",
                }
            },
        )
    if payload.write_env and not payload.env_var:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "write_env needs 'env_var' - the name to store the key under in .env",
                    "type": "invalid_credential",
                }
            },
        )

    env_synced: str | None = None
    if payload.write_env and payload.value and payload.env_var:
        try:
            replaced = upsert_env_var(_env_file(), payload.env_var, payload.value)
        except OSError as exc:
            raise _file_write_failed(exc) from exc
        # Live-export so the credential works on the next request, not after a reload.
        os.environ[payload.env_var] = payload.value
        env_synced = f".env ({'updated' if replaced else 'appended'})"

    credential = {
        "id": payload.id,
        "env_var": payload.env_var,
        # Never let the secret reach the YAML when it was written to .env.
        "value": None if payload.write_env else payload.value,
        "priority": payload.priority,
        "enabled": payload.enabled,
    }
    synced = None
    path = _source_file(container, "providers")
    if path is not None:
        append_credential_to_provider(path, provider_id, credential)
        synced = path.name
    # Update in-memory config + credential pool
    from app.models.provider import CredentialConfig

    cred = CredentialConfig(
        id=payload.id,
        env_var=payload.env_var,
        value=credential["value"],
        enabled=payload.enabled,
        priority=payload.priority,
    )
    # replace if the id already exists
    provider.credentials = [c for c in provider.credentials if c.id != payload.id] + [cred]
    container.pool.register_provider(provider)
    return {
        "object": "credential",
        "provider_id": provider_id,
        "credential_id": payload.id,
        "synced": synced,
        "env_synced": env_synced,
    }


@router.delete("/providers/{provider_id}/credentials/{credential_id}", summary="Remove a credential")
async def delete_credential(
    provider_id: str, credential_id: str, container: ContainerDep
) -> dict[str, Any]:
    """Remove a credential from a provider's pool at runtime; persists across restarts."""
    provider = container.config.providers.get(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"provider '{provider_id}' not found"}},
        )
    if not any(c.id == credential_id for c in provider.credentials):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"credential '{credential_id}' not found"}},
        )
    synced = None
    path = _source_file(container, "providers")
    if path is not None:
        delete_credential_from_provider(path, provider_id, credential_id)
        synced = path.name
    provider.credentials = [c for c in provider.credentials if c.id != credential_id]
    container.pool.register_provider(provider)
    return {"deleted": credential_id, "provider_id": provider_id, "synced": synced}


@router.delete("/providers/{provider_id}/limits", summary="Reset quota rules to YAML")
async def reset_provider_limits(provider_id: str, container: ContainerDep) -> dict[str, Any]:
    """Drop the console override and re-read the provider's YAML-defined rules."""
    provider = container.config.providers.get(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"provider '{provider_id}' not found"}},
        )
    yaml_config = load_app_config(container.settings)
    yaml_provider = yaml_config.providers.get(provider_id)
    yaml_rules = list((yaml_provider.options if yaml_provider else {}).get("rate_limits") or [])
    provider.options = {**provider.options, "rate_limits": yaml_rules}
    container.rate_limiter.register_provider(provider)
    await container.config_repository.clear_provider_rate_limits(provider_id, yaml_rules)
    return {"object": "rate_limits", "provider_id": provider_id, "rules": yaml_rules, "source": "yaml"}


class ProviderUpsertRequest(BaseModel):
    """Body for creating/replacing a provider from the console."""

    id: str
    type: str = "openai_compatible"
    base_url: str
    enabled: bool = True
    timeout: float = 60.0
    connect_timeout: float = 10.0
    max_retries: int = 2
    api_version: str | None = None
    referer: str | None = None
    app_title: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


@router.post("/providers", summary="Create or replace a provider")
async def upsert_provider(payload: ProviderUpsertRequest, container: ContainerDep) -> dict[str, Any]:
    """Add or edit a provider (endpoint + protocol type); persists across restarts.

    Credentials are managed separately (``/providers/{id}/credentials``), so this
    never touches an existing provider's key list.
    """
    from app.models.provider import ProviderConfig, ProviderType

    try:
        provider = ProviderConfig(**payload.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "invalid_provider"}},
        ) from exc
    if provider.type is ProviderType.OLLAMA and not provider.base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": "ollama providers need a base_url", "type": "invalid_provider"}},
        )
    existing = container.config.providers.get(provider.id)
    if existing is not None:
        # Keep the hand-managed key list; only the endpoint/protocol fields change.
        provider.credentials = existing.credentials
        # Console edits only manage the top-level fields; provider-specific options
        # (e.g. sensenova's rate_limits) stay untouched unless this payload is meant
        # to replace them wholesale.
        if not payload.options:
            provider.options = dict(existing.options)
    synced = None
    path = _source_file(container, "providers")
    if path is not None:
        file_payload = _strip_nulls(provider.model_dump(mode="json"))
        file_payload.pop("credentials", None)  # never overwrite keys from here
        upsert_provider_file(path, provider.id, file_payload)
        synced = path.name
    container.config.providers[provider.id] = provider
    container.pool.register_provider(provider)
    container.rate_limiter.register_provider(provider)
    container.router.upsert_adapter(provider)
    await container.config_repository.sync_config(container.config)
    logger.info("provider %s upserted (%s) via console", provider.id, provider.type.value)
    return {
        "object": "provider",
        "provider_id": provider.id,
        "created": existing is None,
        "synced": synced,
    }


@router.delete("/providers/{provider_id}", summary="Delete a provider")
async def delete_provider(provider_id: str, container: ContainerDep) -> dict[str, Any]:
    """Remove a provider and its credentials; refuses while models still deploy on it."""
    if provider_id not in container.config.providers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"provider '{provider_id}' not found"}},
        )
    referenced = [
        model.id
        for model in container.config.models.values()
        if any(d.provider_id == provider_id for d in model.deployments)
    ]
    if referenced:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "message": (
                        f"provider '{provider_id}' still serves models: "
                        f"{', '.join(sorted(referenced))} - delete or move those deployments first"
                    ),
                    "type": "provider_in_use",
                }
            },
        )
    synced = None
    path = _source_file(container, "providers")
    if path is not None:
        delete_provider_file(path, provider_id)
        synced = path.name
    container.config.providers.pop(provider_id, None)
    pruned = container.pool.reconcile(container.config.providers)
    container.router.remove_adapter(provider_id)
    await container.config_repository.sync_config(container.config)
    logger.info("provider %s deleted via console (%d credential(s) dropped)", provider_id, pruned)
    return {"deleted": provider_id, "credentials_removed": pruned, "synced": synced}


# --------------------------------------------------------------------------- #
# Credential administration
# --------------------------------------------------------------------------- #
@router.post("/credentials/{credential_id}/enable", summary="Enable a credential")
async def enable_credential(credential_id: str, container: ContainerDep) -> dict[str, Any]:
    transition = container.pool.enable(credential_id)
    if transition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"credential '{credential_id}' not found"}},
        )
    await container.config_repository.set_credential_enabled(credential_id, True)
    return {
        "credential_id": credential_id,
        "status": transition.current.value,
        "previous_status": transition.previous.value,
        "reason": transition.reason,
    }


@router.post("/credentials/{credential_id}/disable", summary="Disable a credential")
async def disable_credential(
    credential_id: str,
    container: ContainerDep,
    reason: str = Query(default="disabled by operator"),
) -> dict[str, Any]:
    transition = container.pool.disable(credential_id, reason)
    if transition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"credential '{credential_id}' not found"}},
        )
    await container.config_repository.set_credential_enabled(credential_id, False, reason)
    return {
        "credential_id": credential_id,
        "status": transition.current.value,
        "previous_status": transition.previous.value,
        "reason": transition.reason,
    }


@router.post("/credentials/cooldowns/clear", summary="Clear cooldowns")
async def clear_cooldowns(
    container: ContainerDep, provider_id: str | None = Query(default=None)
) -> dict[str, Any]:
    """Force every cooling credential back to HEALTHY (emergency retry)."""
    cleared = container.pool.invalidate_cooldowns(provider_id)
    return {"cleared": cleared, "provider_id": provider_id}


# --------------------------------------------------------------------------- #
# Model administration (runtime edits persist in the DB mirror)
# --------------------------------------------------------------------------- #
class ModelUpsertRequest(BaseModel):
    """Body for creating/replacing a model at runtime (web console editor)."""

    id: str
    display_name: str | None = None
    owned_by: str | None = None
    description: str | None = None
    enabled: bool = True
    context_window: int = 128_000
    capabilities: dict[str, float] = Field(default_factory=dict)
    deployments: list[DeploymentConfig] = Field(default_factory=list)


@router.post("/models", summary="Create or replace a model")
async def upsert_model(payload: ModelUpsertRequest, container: ContainerDep) -> dict[str, Any]:
    """Hot-add/edit a model; takes effect immediately and survives restarts."""
    try:
        model = ModelConfig(**payload.model_dump())
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "invalid_model"}},
        ) from exc

    unknown_providers = [
        deployment.provider_id
        for deployment in model.deployments
        if deployment.provider_id not in container.config.providers
    ]
    if unknown_providers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": f"unknown providers: {', '.join(sorted(set(unknown_providers)))}",
                    "type": "invalid_model",
                    "known_providers": sorted(container.config.providers),
                }
            },
        )
    deployment_ids = [d.id for d in model.deployments]
    if len(deployment_ids) != len(set(deployment_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": "duplicate deployment ids", "type": "invalid_model"}},
        )

    # File first: if the YAML cannot be written we abort with no partial state,
    # so the console and the on-disk config never drift apart.
    synced = _write_model_file(container, model)
    container.config.models[model.id] = model
    await container.config_repository.upsert_model(model)
    return {
        "object": "model",
        "synced": synced,
        "data": container.model_service.describe(model.id),
    }


@router.delete("/models/{model_id}", summary="Delete a model")
async def delete_model(model_id: str, container: ContainerDep) -> dict[str, Any]:
    if model_id not in container.config.models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"model '{model_id}' not found"}},
        )
    referenced_by = [
        name
        for name, alias in container.config.aliases.items()
        if model_id in alias.targets
    ]
    if referenced_by:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "message": (
                        f"model '{model_id}' is still referenced by aliases: "
                        f"{', '.join(sorted(referenced_by))} - edit those first"
                    ),
                    "type": "model_in_use",
                }
            },
        )
    synced = _delete_file_entry(container, "models", "id", model_id)
    container.config.models.pop(model_id, None)
    await container.config_repository.delete_model(model_id)
    return {"deleted": model_id, "synced": synced}


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
@router.get("/router/preview", summary="Explain a routing decision")
async def router_preview(
    container: ContainerDep,
    model: str = Query(description="Model id or alias, e.g. zk-coding"),
    prompt: str = Query(default="", description="Prompt used for capability inference"),
    tools: int = Query(default=0, description="Number of tools the request declares"),
    json_mode: bool = Query(default=False, description="Simulate response_format=json_object"),
    max_tokens: int | None = Query(default=None),
) -> dict[str, Any]:
    """Show how the router would order candidates for a request."""
    messages = [ChatMessage(role="user", content=prompt or "hello")]
    tool_defs = (
        [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{index}",
                    "description": "placeholder tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for index in range(max(0, tools))
        ]
        or None
    )
    request = ChatCompletionRequest(
        model=model,
        messages=messages,
        tools=tool_defs,
        response_format={"type": "json_object"} if json_mode else None,
        max_tokens=max_tokens,
    )
    preview = container.router.preview(request)
    preview["pool"] = {
        provider_id: container.pool.describe_selection(provider_id)
        for provider_id in container.config.providers
    }
    return preview


class AliasUpsertRequest(BaseModel):
    """Body for creating/replacing an alias at runtime."""

    name: str
    targets: list[str] = Field(default_factory=list)
    strategy: AliasStrategy = AliasStrategy.CAPABILITY
    enabled: bool = True
    description: str | None = None
    weights: dict[str, float] = Field(default_factory=dict)
    requires: dict[str, float] = Field(default_factory=dict)


@router.post("/aliases", summary="Create or replace an alias")
async def upsert_alias(payload: AliasUpsertRequest, container: ContainerDep) -> dict[str, Any]:
    """Hot-swap an alias - clients keep sending the same model name."""
    try:
        alias = ModelAliasConfig(**payload.model_dump())
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "invalid_alias"}},
        ) from exc

    unknown = [
        target
        for target in alias.targets
        if target not in container.config.models
        and not container.router.aliases.is_alias(target)
    ]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": f"unknown targets: {', '.join(unknown)}",
                    "type": "invalid_alias",
                    "known_models": container.config.public_model_ids(),
                }
            },
        )

    synced = _write_alias_file(container, alias)
    container.router.aliases.upsert(alias)
    container.config.aliases[alias.name] = alias
    await container.config_repository.upsert_alias(
        alias.name,
        targets=list(alias.targets),
        strategy=alias.strategy.value,
        enabled=alias.enabled,
        weights=dict(alias.weights),
        requires=dict(alias.requires),
        description=alias.description,
    )
    return {
        "object": "alias",
        "synced": synced,
        "data": container.router.aliases.describe()[alias.name],
    }


@router.delete("/aliases/{name}", summary="Delete an alias")
async def delete_alias(name: str, container: ContainerDep) -> dict[str, Any]:
    removed = container.router.aliases.remove(name)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"alias '{name}' not found"}},
        )
    synced = _delete_file_entry(container, "aliases", "name", name)
    container.config.aliases.pop(name, None)
    await container.config_repository.delete_alias(name)
    return {"deleted": name, "synced": synced}


@router.post("/config/reload", summary="Reload YAML configuration")
async def reload_config(container: ContainerDep) -> dict[str, Any]:
    """Re-read the YAML files and rebuild aliases + adapters (no restart needed)."""
    try:
        config = load_app_config(container.settings)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "config_error"}},
        ) from exc

    # Re-apply quota rules that could not reach providers.yaml (file is otherwise
    # authoritative: models/aliases edits were written back to it on save).
    apply_db_overrides(
        config,
        await container.config_repository.provider_rate_limit_overrides(),
    )

    container.router.reload(config, alias_registry=AliasRegistry(config.aliases.values()))
    container.config = config
    container.model_service.config = config
    container.health_service.config = config
    container.usage_service.config = config
    container.rate_limiter.configure(config.providers)
    for provider in config.providers.values():
        container.pool.register_provider(provider)
    # The pool is in-memory state: dropping a credential/provider from YAML and
    # hitting reload must remove it here too, not just from the DB mirror.
    pruned = container.pool.reconcile(config.providers)
    if pruned:
        logger.info("config reload pruned %d stale credential(s) from the pool", pruned)
    return {
        "reloaded": True,
        "models": len(config.models),
        "aliases": sorted(config.aliases),
        "providers": sorted(config.providers),
        "warnings": config.warnings,
    }


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
@router.get("/stats", summary="Traffic, token and error statistics")
async def stats(
    container: ContainerDep,
    days: int = Query(default=7, ge=1, le=90, description="Aggregation window in days"),
    recent: int = Query(default=20, ge=0, le=200, description="Recent requests to include"),
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


@router.get("/requests", summary="List requests with filters and paging")
async def list_requests(
    container: ContainerDep,
    status: str | None = Query(default=None, description="success | error | cancelled | pending"),
    alias: str | None = Query(default=None, description="Exact alias name"),
    provider: str | None = Query(default=None, description="Exact provider id"),
    credential: str | None = Query(default=None, description="Exact credential id"),
    model: str | None = Query(default=None, description="Substring of requested/resolved model"),
    error_type: str | None = Query(default=None, description="Exact error type"),
    q: str | None = Query(default=None, description="Free text over id/model/credential/error"),
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


@router.get("/requests/{request_id}", summary="Request detail with attempts")
async def request_detail(request_id: str, container: ContainerDep) -> dict[str, Any]:
    attempts = await container.request_repository.attempts_for(request_id)
    row = await container.request_repository.get_request(request_id)
    if row is None and not attempts:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"request '{request_id}' not found"}},
        )
    return {"request_id": request_id, "request": row, "attempts": attempts}


@router.post("/usage/backfill-cost", summary="Backfill equivalent cost on zero-cost rows")
async def backfill_cost(container: ContainerDep) -> dict[str, Any]:
    """Reprice usage rows recorded before list prices existed (console button)."""
    return await container.usage_service.backfill_zero_cost()


# --------------------------------------------------------------------------- #
# ChatGPT / Codex desktop (~/.codex/config.toml + the zk-auto alias)
#
# The desktop app reads config.toml once at startup and never reloads it, so
# rewriting ``model = ...`` there only applies after an app restart. But the
# app already sends ``model = "zk-auto"`` and the router resolves aliases
# per-request. The console therefore hot-swaps zk-auto's target list - the
# next message in a running conversation already uses the new model.
# config.toml is never rewritten.
# --------------------------------------------------------------------------- #

_CODEX_CONFIG = Path.home() / ".codex" / "config.toml"

#: The alias the desktop app already sends.
CHATGPT_ALIAS = "zk-auto"


def _codex_config_path() -> Path:
    return _CODEX_CONFIG


def _read_codex_config() -> dict[str, str] | None:
    """Parse model / model_provider from config.toml (top-level keys only)."""
    path = _codex_config_path()
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip(chr(34)).strip(chr(39)).strip()
        if key in ("model", "model_provider"):
            result[key] = value
        if len(result) == 2:
            break
    return result if result else None


def _chatgpt_choices(container: ContainerDep) -> list[str]:
    """Selectable targets: enabled aliases (except our own) + enabled models."""
    aliases = sorted(
        name
        for name, a in container.config.aliases.items()
        if a.enabled and name != CHATGPT_ALIAS
    )
    models = sorted(m.id for m in container.config.models.values() if m.enabled)
    return aliases + models


def _chatgpt_state(container: ContainerDep) -> dict[str, Any]:
    """Current effective model = zk-auto's first target; plus config.toml state."""
    cfg = _read_codex_config() or {}
    config_model = cfg.get("model", "")
    alias = container.config.aliases.get(CHATGPT_ALIAS)
    effective = alias.targets[0] if alias and alias.targets else ""
    return {
        "ok": True,
        "path": str(_codex_config_path()),
        "alias": CHATGPT_ALIAS,
        "alias_exists": alias is not None,
        "effective_model": effective,
        "config_model": config_model,
        "config_provider": cfg.get("model_provider", ""),
        "pinned": config_model == CHATGPT_ALIAS,
        "choices": _chatgpt_choices(container),
    }


@router.get("/chatgpt", summary="ChatGPT/Codex desktop: current model")
async def get_chatgpt_config(container: ContainerDep) -> dict[str, Any]:
    cfg_path = _codex_config_path()
    if not cfg_path.exists():
        return {"ok": False, "detail": f"not found: {cfg_path}"}
    return _chatgpt_state(container)


@router.post("/chatgpt", summary="ChatGPT/Codex desktop: switch model (hot)")
async def set_chatgpt_model(
    container: ContainerDep, payload: dict[str, str] = Body(...)
) -> dict[str, Any]:
    """Promote *model* to the front of zk-auto's target chain - takes effect
    on the next request, no desktop restart needed. The original chain order is
    preserved behind the new first target so fallback still works."""
    model = payload.get("model", "").strip()
    if not model:
        raise HTTPException(
            status_code=400, detail={"error": {"message": "model is required"}}
        )
    known = _chatgpt_choices(container)
    if model not in known:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": f"unknown model or alias: {model}"}},
        )

    existing = container.config.aliases.get(CHATGPT_ALIAS)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"message": f"alias '{CHATGPT_ALIAS}' not found"}},
        )

    # Build the new target chain: chosen model first, then the rest unchanged.
    old_targets = [t for t in existing.targets if t != model]
    new_targets = [model, *old_targets]

    alias = ModelAliasConfig(
        name=CHATGPT_ALIAS,
        description=existing.description,
        strategy=existing.strategy,
        targets=new_targets,
        enabled=True,
        weights=dict(existing.weights),
        requires=dict(existing.requires),
        # The operator just pinned this model, so it must lead the attempt order
        # even under `strategy=capability` - otherwise the highest-scoring
        # model wins again and the hot-swap silently does nothing.
        pin_first=True,
    )
    synced = _write_alias_file(container, alias)
    container.router.aliases.upsert(alias)
    container.config.aliases[alias.name] = alias
    await container.config_repository.upsert_alias(
        alias.name,
        targets=list(alias.targets),
        strategy=alias.strategy.value,
        enabled=alias.enabled,
        weights=dict(alias.weights),
        requires=dict(alias.requires),
        description=alias.description,
    )

    return {
        "ok": True,
        "model": model,
        "effective": True,
        "restart_required": False,
        "targets": new_targets,
        "synced": synced,
        "path": str(_codex_config_path()),
    }
