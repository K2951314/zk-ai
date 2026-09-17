"""Administration API (``/admin/*``).

Everything an operator needs: inspect providers/models/credentials, force health
checks, enable/disable keys, explain routing decisions and read statistics.

Protected by ``ZKAI_ADMIN_TOKEN`` when configured (see :func:`app.api.deps.require_admin`).
Credential responses never contain secret material - only a masked fingerprint.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, require_admin
from app.core.config import apply_db_overrides, load_app_config
from app.core.logging import get_logger
from app.models.provider import (
    AliasStrategy,
    DeploymentConfig,
    ModelAliasConfig,
    ModelConfig,
)
from app.models.request import ChatCompletionRequest, ChatMessage
from app.routing.aliases import AliasRegistry

logger = get_logger("api.admin")

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# --------------------------------------------------------------------------- #
# Catalogue views
# --------------------------------------------------------------------------- #
@router.get("/providers", summary="List providers")
async def list_providers(container: ContainerDep) -> dict[str, Any]:
    """Configured providers, their credentials and live availability."""
    availability = container.pool.provider_availability()
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

    container.config.models[model.id] = model
    await container.config_repository.upsert_model(model)
    return {"object": "model", "data": container.model_service.describe(model.id)}


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
    container.config.models.pop(model_id, None)
    await container.config_repository.delete_model(model_id)
    return {"deleted": model_id}


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
    return {"object": "alias", "data": container.router.aliases.describe()[alias.name]}


@router.delete("/aliases/{name}", summary="Delete an alias")
async def delete_alias(name: str, container: ContainerDep) -> dict[str, Any]:
    removed = container.router.aliases.remove(name)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"alias '{name}' not found"}},
        )
    container.config.aliases.pop(name, None)
    await container.config_repository.delete_alias(name)
    return {"deleted": name}


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

    # Re-apply console edits persisted in the DB so a reload does not drop them.
    apply_db_overrides(
        config,
        await container.config_repository.model_overrides(),
        await container.config_repository.alias_overrides(),
    )

    container.router.reload(config, alias_registry=AliasRegistry(config.aliases.values()))
    container.config = config
    container.model_service.config = config
    container.health_service.config = config
    container.usage_service.config = config
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
