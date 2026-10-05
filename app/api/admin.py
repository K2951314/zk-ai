"""Administration API (``/admin/*``).

Everything an operator needs: inspect providers/models/credentials, force health
checks, enable/disable keys, explain routing decisions and read statistics.

Protected by ``ZKAI_ADMIN_TOKEN`` when configured (see :func:`app.api.deps.require_admin`).
Credential responses never contain secret material - only a masked fingerprint.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, require_admin
from app.core.config import PROJECT_ROOT, load_app_config
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
from app.providers.base import ProviderContext
from app.retry.classifier import ErrorClassifier
from app.routing.limits import RateLimitRule
from app.services import burner_service, chatgpt_service

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
    # 陈旧配置是**可操作**的状态，不是服务器故障：给 409 + 明确指引，
    # 而不是把「请先 reload」埋在 500 的堆栈里。
    from app.core.config_writer import ConfigStaleError

    if isinstance(exc, ConfigStaleError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "message": str(exc),
                    "type": "config_stale",
                    "hint": "POST /admin/config/reload 之后再重试本次保存",
                }
            },
        )
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
    # 自己写的文件不会被自己判陈旧（atomic_write 内部 note_loaded），但
    # ConfigWatcher 用的是独立的 (mtime_ns, size) 签名——不 reseed 的话，下一
    # 次 poll 会在 ~2.4s 后看到文件变了并触发一次冗余 reload（结果与内存一致，
    # 纯浪费）。reseed 让 watcher 把刚写的文件认作新基线。
    container.refresh_config_watch()
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
    container.refresh_config_watch()  # 见 _write_model_file 的说明
    return path.name


def _delete_file_entry(container: ContainerDep, section: str, key: str, name: str) -> str | None:
    path = _source_file(container, "models") or _source_file(container, "config")
    if path is None:
        return None
    try:
        delete_list_entry(path, section, key, name)
    except (OSError, ValueError) as exc:
        raise _file_write_failed(exc) from exc
    container.refresh_config_watch()  # 见 _write_model_file 的说明
    return path.name


def _write_limits_file(container: ContainerDep, provider_id: str, rules: list[dict]) -> str | None:
    path = _source_file(container, "providers")
    if path is None:
        return None
    try:
        sync_provider_rate_limits(path, provider_id, rules)
    except (OSError, ValueError, KeyError) as exc:
        raise _file_write_failed(exc) from exc
    container.refresh_config_watch()  # 见 _write_model_file 的说明
    return path.name


# --------------------------------------------------------------------------- #
# Catalogue views
# --------------------------------------------------------------------------- #
@router.get("/providers", summary="列出供应商")
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


@router.post(
    "/providers/{provider_id}/models/verify",
    summary="逐个实测供应商模型是否真的可调用",
)
async def verify_provider_models(
    provider_id: str,
    container: ContainerDep,
    payload: VerifyModelsRequest,
) -> dict[str, Any]:
    """Probe each model with a **real minimal call** and report what actually works.

    为什么必须补这一环（2026-09-29 实测）：``/v1/models`` 列的是**供应商目录**，
    不是你账号能调的东西。三个真实反例，三个都通过了目录检查：

    * 商汤 ``deepseek-v4.1-flash``：目录里有，调用返回 403
      ``model is not available in the current token plan``
    * NVIDIA ``z-ai/glm-5.3``：目录里有，生成请求**挂死**（121s 仍无响应）
    * NVIDIA ``moonshotai/kimi-k3``：目录里有，调用返回 404
      ``Function id ... Not Found``

    市场原来只看目录，于是运营者会把死模型加进来，直到第一次真请求才暴露——
    而那时它已经在别名的故障转移链里，或者被设成了接口模型。

    探测用 ``max_tokens=1`` 的最小请求，且给一个**短超时**（默认 20s）：
    "挂死"本身就是结论，没必要陪它等 60 秒。串行执行（并发探测会被供应商
    当成滥用，也分不清限流是谁引起的）。
    """
    provider = container.config.providers.get(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"供应商 '{provider_id}' 不存在"}},
        )
    credential = next(iter(container.pool.for_provider(provider_id)), None)
    if credential is None and provider.requires_credential:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": f"供应商 '{provider_id}' 还没有可用的 Key，无法验证",
                    "type": "no_credential",
                }
            },
        )
    adapter = container.router.adapter(provider_id)
    timeout = max(3.0, float(payload.timeout_seconds or 20.0))

    results: list[dict[str, Any]] = []
    for upstream in payload.models[: payload.limit]:
        results.append(
            await _verify_one_model(container, adapter, provider, credential, upstream, timeout)
        )
    ok = sum(1 for r in results if r["ok"])
    return {
        "object": "list",
        "provider_id": provider_id,
        "checked": len(results),
        "usable": ok,
        "timeout_seconds": timeout,
        "results": results,
        "note": (
            "「目录里有」不等于「你账号能调」。ok=false 的模型加进配置后只会在"
            "请求时才失败，别把它放进别名链，更别设成接口模型。"
        ),
    }


async def _verify_one_model(
    container: ContainerDep,
    adapter: Any,
    provider: Any,
    credential: Any,
    upstream: str,
    probe_seconds: float,
) -> dict[str, Any]:
    """One probe. Never raises: a probe that blows up is itself the verdict."""
    # 用最小成本构造一次真实调用：1 个 token、无工具、无历史。
    model = ModelConfig(id=f"__probe__{upstream}", display_name=upstream, context_window=8192)
    deployment = DeploymentConfig(id=f"__probe__{upstream}", provider_id=provider.id, model=upstream)
    request = ChatCompletionRequest(
        model=model.id,
        messages=[ChatMessage(role="user", content="hi")],
        max_tokens=1,
    )
    ctx = ProviderContext(
        request_id=f"probe_{upstream}",
        deployment=deployment,
        model=model,
        credential=credential,
        timeout=probe_seconds,
        stream=False,
    )
    started = time.perf_counter()
    try:
        async with asyncio.timeout(probe_seconds):
            await adapter.chat(request, ctx)
    except TimeoutError:
        return {
            "upstream_model": upstream,
            "ok": False,
            "verdict": "hang",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": f"{probe_seconds:.0f} 秒内无任何响应——不是慢，是挂死。加进链里等于每次都白等。",
        }
    except Exception as exc:
        info = ErrorClassifier().classify(exc)
        return {
            "upstream_model": upstream,
            "ok": False,
            "verdict": _probe_verdict(info),
            "http_status": info.http_status,
            "error_type": info.error_type,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "message": (info.message or str(exc))[:300],
        }
    return {
        "upstream_model": upstream,
        "ok": True,
        "verdict": "ok",
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "message": "",
    }


def _probe_verdict(info: Any) -> str:
    """把 ErrorInfo 收敛成运营者看得懂的四种结论。"""
    msg = (info.message or "").lower()
    if info.http_status == 404 or "not found" in msg or "function id" in msg:
        return "not_found"
    if info.http_status in (401, 403) or "entitlement" in msg or "token plan" in msg:
        return "no_entitlement"
    if info.http_status == 429:
        return "rate_limited"
    if info.error_type == "timeout":
        return "timeout"
    return "error"


@router.get("/providers/{provider_id}/models", summary="列出供应商的上游模型")
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
            detail={"error": {"message": f"供应商 '{provider_id}' 不存在"}},
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
        # 第一把 Key 缺值不代表整池都缺（例如 sensenova-01 是留空的网关独享位，
        # 而 _02~_09 都有值）——目录探测只需要任意一把有效 Key。
        usable = next(
            (c for c in container.pool.for_provider(provider_id)
             if c.secret_source != "missing"),  # noqa: S105 - source label
            None,
        )
        if usable is None:
            return {
                "object": "list",
                "provider_id": provider_id,
                "data": [],
                "note": (
                    f"该供应商的 Key 没有实际值（{credential.secret_ref} 在 .env 里是空的）"
                    "——去「凭据池 → ➕ 加 Key」把值填上"
                ),
            }
        credential = usable
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


@router.get("/models", summary="列出模型（含能力评分）")
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


@router.get("/credentials", summary="列出凭据（密钥已脱敏）")
async def list_credentials(
    container: ContainerDep,
    provider_id: str | None = Query(default=None, description="按供应商筛选"),
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


@router.get("/aliases", summary="列出模型别名")
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
@router.get("/health", summary="详细健康报告")
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
        default=None, description="要探测的供应商 ID；留空 = 所有已启用的供应商"
    )
    credentials: bool = Field(default=True, description="是否逐把 Key 单独探测")


@router.post("/health/check", summary="立即执行健康检查")
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


@router.put("/providers/{provider_id}/limits", summary="设置供应商的配额规则")
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
            detail={"error": {"message": f"供应商 '{provider_id}' 不存在"}},
        )
    cleaned: list[dict[str, Any]] = []
    for entry in payload.rules:
        rule = RateLimitRule.from_mapping(entry)
        if rule is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "message": f"限额规则不合法：{entry!r}（需要 window_seconds，以及 "
                        "max_requests 或 max_tokens 至少一项；scope 取值 "
                        "credential|account|provider）",
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


@router.post("/providers/{provider_id}/credentials", summary="给供应商添加凭据")
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
            detail={"error": {"message": f"供应商 '{provider_id}' 不存在"}},
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
                    "message": "必须提供 env_var 或 value（推荐 env_var："
                    "把 Key 写进 .env，这里只填变量名）",
                    "type": "invalid_credential",
                }
            },
        )
    if payload.write_env and not payload.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "要把 Key 写进 .env，必须在 value 里填上 Key 本身",
                    "type": "invalid_credential",
                }
            },
        )
    if payload.write_env and not payload.env_var:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "要把 Key 写进 .env，必须在 env_var 里填上变量名（Key 在 .env 里的名字）",
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
        container.refresh_config_watch()  # 见 _write_model_file 的说明
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


@router.delete("/providers/{provider_id}/credentials/{credential_id}", summary="删除凭据")
async def delete_credential(
    provider_id: str, credential_id: str, container: ContainerDep
) -> dict[str, Any]:
    """Remove a credential from a provider's pool at runtime; persists across restarts."""
    provider = container.config.providers.get(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"供应商 '{provider_id}' 不存在"}},
        )
    if not any(c.id == credential_id for c in provider.credentials):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"凭据 '{credential_id}' 不存在"}},
        )
    synced = None
    path = _source_file(container, "providers")
    if path is not None:
        delete_credential_from_provider(path, provider_id, credential_id)
        synced = path.name
        container.refresh_config_watch()
    provider.credentials = [c for c in provider.credentials if c.id != credential_id]
    container.pool.register_provider(provider)
    return {"deleted": credential_id, "provider_id": provider_id, "synced": synced}


@router.delete("/providers/{provider_id}/limits", summary="恢复 YAML 里的配额规则")
async def reset_provider_limits(provider_id: str, container: ContainerDep) -> dict[str, Any]:
    """Drop the console override and re-read the provider's YAML-defined rules."""
    provider = container.config.providers.get(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"供应商 '{provider_id}' 不存在"}},
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


@router.post("/providers", summary="新建或替换供应商")
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
            detail={
                "error": {
                    "message": "Ollama 本地服务必须填写接入地址 base_url",
                    "type": "invalid_provider",
                }
            },
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
        container.refresh_config_watch()
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


@router.delete("/providers/{provider_id}", summary="删除供应商")
async def delete_provider(provider_id: str, container: ContainerDep) -> dict[str, Any]:
    """Remove a provider and its credentials; refuses while models still deploy on it."""
    if provider_id not in container.config.providers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"供应商 '{provider_id}' 不存在"}},
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
                        f"供应商 '{provider_id}' 上还有模型部署："
                        f"{"'、'".join(sorted(referenced))}——先删掉或改走这些部署，再删供应商"
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
        container.refresh_config_watch()
    container.config.providers.pop(provider_id, None)
    pruned = container.pool.reconcile(container.config.providers)
    container.router.remove_adapter(provider_id)
    await container.config_repository.sync_config(container.config)
    logger.info("provider %s deleted via console (%d credential(s) dropped)", provider_id, pruned)
    return {"deleted": provider_id, "credentials_removed": pruned, "synced": synced}


# --------------------------------------------------------------------------- #
# Credential administration
# --------------------------------------------------------------------------- #
@router.post("/credentials/{credential_id}/enable", summary="启用凭据")
async def enable_credential(credential_id: str, container: ContainerDep) -> dict[str, Any]:
    transition = container.pool.enable(credential_id)
    if transition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"凭据 '{credential_id}' 不存在"}},
        )
    await container.config_repository.set_credential_enabled(credential_id, True)
    return {
        "credential_id": credential_id,
        "status": transition.current.value,
        "previous_status": transition.previous.value,
        "reason": transition.reason,
    }


@router.post("/credentials/{credential_id}/disable", summary="禁用凭据")
async def disable_credential(
    credential_id: str,
    container: ContainerDep,
    reason: str = Query(default="操作员手动禁用"),
) -> dict[str, Any]:
    transition = container.pool.disable(credential_id, reason)
    if transition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"凭据 '{credential_id}' 不存在"}},
        )
    await container.config_repository.set_credential_enabled(credential_id, False, reason)
    return {
        "credential_id": credential_id,
        "status": transition.current.value,
        "previous_status": transition.previous.value,
        "reason": transition.reason,
    }


@router.post("/credentials/cooldowns/clear", summary="清空全部冷却")
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


@router.post("/models", summary="新建或替换模型")
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
                    "message": f"未知的供应商：{'、'.join(sorted(set(unknown_providers)))}",
                    "type": "invalid_model",
                    "known_providers": sorted(container.config.providers),
                }
            },
        )
    deployment_ids = [d.id for d in model.deployments]
    if len(deployment_ids) != len(set(deployment_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": "部署 ID 重复", "type": "invalid_model"}},
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


@router.delete("/models/{model_id}", summary="删除模型")
async def delete_model(model_id: str, container: ContainerDep) -> dict[str, Any]:
    if model_id not in container.config.models:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"模型 '{model_id}' 不存在"}},
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
                        f"模型 '{model_id}' 还被这些别名引用："
                        f"{"'、'".join(sorted(referenced_by))}——先改这些别名，再删模型"
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
@router.get("/router/preview", summary="解释一次路由决策（不真正调用）")
async def router_preview(
    container: ContainerDep,
    model: str = Query(description="模型 ID 或别名，如 zk-coding"),
    prompt: str = Query(default="", description="用于能力推断的提示词"),
    tools: int = Query(default=0, description="请求声明的工具数量"),
    json_mode: bool = Query(default=False, description="模拟 response_format=json_object"),
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


class VerifyModelsRequest(BaseModel):
    """Body for ``POST /admin/providers/{id}/models/verify``."""

    models: list[str] = Field(default_factory=list)
    #: 单模型探测上限。短是刻意的——「挂死」本身就是结论。
    timeout_seconds: float = 20.0
    #: 一次最多探几个，防止误传 81 个模型把供应商惹毛。
    limit: int = 12


class AliasUpsertRequest(BaseModel):
    """Body for creating/replacing an alias at runtime."""

    name: str
    targets: list[str] = Field(default_factory=list)
    strategy: AliasStrategy = AliasStrategy.CAPABILITY
    enabled: bool = True
    description: str | None = None
    weights: dict[str, float] = Field(default_factory=dict)
    requires: dict[str, float] = Field(default_factory=dict)
    #: 接口模型（永远排第一）。不传 = 不改动；传 null = 清除，交还能力路由。
    #: 走同一个 POST /admin/aliases 就能热改，不用重排 targets、不用重启——
    #: 这就是运营者「随时调整」的入口。
    front_model: str | None = None


@router.post("/aliases", summary="新建或替换别名")
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
                    "message": f"未知的目标模型/别名：{'、'.join(unknown)}",
                    "type": "invalid_alias",
                    "known_models": container.config.public_model_ids(),
                }
            },
        )
    # ``front_model`` 是运营者显式指定的接口模型：写错必须当场报错。
    # 路由层对配置文件里的拼写错误是静默回落（见 Router._promote_front_model），
    # 因为那可能是历史遗留；但一次显式的 API 调用静默无效更糟——运营者会以为
    # 换成功了，然后继续被同一个超时的模型卡住。
    if alias.front_model and alias.front_model not in alias.targets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": (
                        f"front_model '{alias.front_model}' 不在 targets 里。"
                        "接口模型必须同时是链上的一员，否则它没有可用的部署。"
                    ),
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


@router.delete("/aliases/{name}", summary="删除别名")
async def delete_alias(name: str, container: ContainerDep) -> dict[str, Any]:
    removed = container.router.aliases.remove(name)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"别名 '{name}' 不存在"}},
        )
    synced = _delete_file_entry(container, "aliases", "name", name)
    container.config.aliases.pop(name, None)
    await container.config_repository.delete_alias(name)
    return {"deleted": name, "synced": synced}


@router.post("/config/reload", summary="重载 YAML 配置")
async def reload_config(container: ContainerDep) -> dict[str, Any]:
    """Re-read the YAML files and rebuild aliases + adapters (no restart needed).

    Thin wrapper: the real work is :meth:`Container.reload_config`, which the
    config file watcher calls too. Keeping one implementation is the point -
    "it only works after I press reload" must not be a possible state.
    """
    try:
        return await container.reload_config()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "config_error"}},
        ) from exc


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
@router.get("/stats", summary="流量、Token 与错误统计")
async def stats(
    container: ContainerDep,
    days: int = Query(default=7, ge=1, le=90, description="统计窗口（天）"),
    recent: int = Query(default=20, ge=0, le=200, description="附带返回最近多少条请求"),
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


@router.get("/requests", summary="按条件筛选请求记录并分页")
async def list_requests(
    container: ContainerDep,
    status: str | None = Query(default=None, description="success | error | cancelled | pending"),
    alias: str | None = Query(default=None, description="精确的别名"),
    provider: str | None = Query(default=None, description="精确的供应商 ID"),
    credential: str | None = Query(default=None, description="精确的凭据 ID"),
    model: str | None = Query(default=None, description="请求/实际模型名的子串"),
    error_type: str | None = Query(default=None, description="精确的错误类型"),
    q: str | None = Query(default=None, description="关键字：覆盖 ID / 模型 / 凭据 / 错误"),
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


@router.get("/requests/{request_id}", summary="请求详情（含每次尝试）")
async def request_detail(request_id: str, container: ContainerDep) -> dict[str, Any]:
    attempts = await container.request_repository.attempts_for(request_id)
    row = await container.request_repository.get_request(request_id)
    if row is None and not attempts:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"请求记录 '{request_id}' 不存在"}},
        )
    return {"request_id": request_id, "request": row, "attempts": attempts}


@router.post("/usage/backfill-cost", summary="为没有成本的历史记录补算等价成本")
async def backfill_cost(container: ContainerDep) -> dict[str, Any]:
    """Reprice usage rows recorded before list prices existed (console button)."""
    return await container.usage_service.backfill_zero_cost()


# --------------------------------------------------------------------------- #
# ChatGPT / Codex desktop (~/.codex/config.toml + config/chatgpt.yaml)
#
# The desktop app reads config.toml once at startup and never reloads it, so
# rewriting ``model = ...`` there only applies after an app restart. But the
# app already sends ``model = "zk-auto"`` and the router resolves aliases
# per-request. The console therefore has two levels:
#
# * **hot swap** (``POST /admin/chatgpt``) - reorders zk-auto's target chain;
#   the next message in a running conversation already uses the new model;
# * **client config** (``PUT /admin/chatgpt/client`` / ``POST
#   /admin/chatgpt/apply``) - rewrites the *file* itself (base_url, wire_api,
#   env_key, reasoning effort, official-model switch) through the surgical
#   patcher in :mod:`app.services.chatgpt_service`. config.toml is owned by
#   the app, so only our keys are touched and a ``.bak-<stamp>`` is kept.
#
# The desired config lives in ``config/chatgpt.yaml`` (single source of truth,
# travels inside the migration package - importing on a new machine via
# 一键换机 Skill does NOT auto-configure the client; use this panel after import).
# --------------------------------------------------------------------------- #

#: The alias the desktop app already sends.
CHATGPT_ALIAS = "zk-auto"


class ChatGptClientPayload(BaseModel):
    """Console form → desired client config. Empty/blank = fall back to defaults."""

    mode: str = "zk-ai"
    model: str = ""
    model_provider: str = "zkai"
    provider_display: str = "ZK-AI"
    base_url: str = ""
    wire_api: str = "responses"
    env_key: str = "ZKAI_API_TOKEN"
    model_reasoning_effort: str = ""
    official_model: str = ""
    apply: bool = True


def _desired_config_path(container: ContainerDep) -> Path:
    return container.settings.resolved_config_dir / "chatgpt.yaml"


def _load_desired_or_400(container: ContainerDep) -> chatgpt_service.ChatGptConfig | None:
    """Load config/chatgpt.yaml; None when it was never saved."""
    try:
        return chatgpt_service.load_desired(_desired_config_path(container))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": {"message": str(exc)}}
        ) from exc


def _validate_or_400(desired: chatgpt_service.ChatGptConfig) -> None:
    errors = chatgpt_service.validate(desired)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "；".join(errors), "type": "invalid_chatgpt_config"}},
        )


def _base_url_warnings(
    desired: chatgpt_service.ChatGptConfig, container: ContainerDep
) -> list[str]:
    """base_url 与当前公网入口漂移时的提醒（不阻塞保存，见 service 里的注释）。"""
    try:
        return chatgpt_service.base_url_drift(desired, container.settings)
    except Exception:  # pragma: no cover - 配置异常不该让保存整体失败
        return []


def _chatgpt_choices(container: ContainerDep) -> list[str]:
    """Selectable targets: enabled aliases (except our own) + enabled models."""
    aliases = sorted(
        name
        for name, a in container.config.aliases.items()
        if a.enabled and name != CHATGPT_ALIAS
    )
    models = sorted(m.id for m in container.config.models.values() if m.enabled)
    return aliases + models


@router.get("/chatgpt", summary="ChatGPT / Codex 桌面版：模型与客户端配置状态")
async def get_chatgpt_config(container: ContainerDep) -> dict[str, Any]:
    """Everything the console panel needs: alias state, desired config,
    on-disk state (auth.json is read-only info), and key-level drift."""
    cfg_path = chatgpt_service.config_toml_path()
    desired = _load_desired_or_400(container)
    disk = chatgpt_service.read_disk_state(cfg_path)
    drift: list[dict[str, Any]] = []
    if desired is not None and not disk.parse_error:
        drift = [
            c.as_dict()
            for c in chatgpt_service.plan_changes(
                chatgpt_service.read_config_toml(cfg_path), desired
            )
        ]
    alias = container.config.aliases.get(CHATGPT_ALIAS)
    env_key_name = disk.env_key or (desired.env_key if desired else "") or "ZKAI_API_TOKEN"
    env_key_value = chatgpt_service.read_user_env_var(env_key_name)
    gateway_token = container.settings.api_token
    return {
        "ok": True,
        "path": str(cfg_path),
        "alias": CHATGPT_ALIAS,
        "alias_exists": alias is not None,
        "effective_model": alias.targets[0] if alias and alias.targets else "",
        "targets": list(alias.targets) if alias else [],
        "config_exists": disk.exists,
        "config_model": disk.model,
        "config_provider": disk.model_provider,
        "config_parse_error": disk.parse_error,
        "pinned": disk.model == CHATGPT_ALIAS,
        "choices": _chatgpt_choices(container),
        "desired": asdict(desired) if desired else None,
        "desired_path": str(_desired_config_path(container)),
        "disk": asdict(disk),
        "auth": chatgpt_service.read_auth_info(cfg_path.parent),
        "drift": drift,
        # 桌面版能否真正读到 Key：它从 explorer 启动，只认用户级环境变量——
        # 缺它就报 "Missing environment variable: <NAME>"（2026-09-22 换机事故）
        "env_key_name": env_key_name,
        "env_key_visible": env_key_value is not None,
        "env_key_matches_gateway": (
            env_key_value == gateway_token if (gateway_token and env_key_value is not None) else None
        ),
        "gateway": {
            "port": container.settings.port,
            "host": container.settings.host,
            "api_token_set": bool(gateway_token),
        },
        # 部署形态：远程部署时写本机 config.toml 没有意义（那是服务器的文件
        # 系统，不是打开控制台那台电脑的），面板据此只渲染公网调用那段。
        "deploy": chatgpt_service.deploy_context(container.settings),
        # 现役模型快照，供面板拼「模型清单」：调用方要知道除了 zk-auto 还能点什么。
        "catalogue": _chatgpt_catalogue(container),
    }


def _chatgpt_catalogue(container: ContainerDep) -> list[dict[str, Any]]:
    """别名 + 具体模型，带上下文窗口，按名字排序（面板「模型清单」用）。"""
    out: list[dict[str, Any]] = []
    for name, alias in container.config.aliases.items():
        if not alias.enabled:
            continue
        first = alias.targets[0] if alias.targets else ""
        model = container.config.models.get(first)
        out.append({
            "kind": "alias",
            "name": name,
            "resolves_to": first,
            "context_window": model.context_window if model else None,
            "description": alias.description or "",
        })
    for model_id, model in container.config.models.items():
        if not model.enabled:
            continue
        out.append({
            "kind": "model",
            "name": model_id,
            "resolves_to": model_id,
            "context_window": model.context_window,
            "description": "",
        })
    out.sort(key=lambda item: (item["kind"] != "alias", item["name"]))
    return out


@router.post("/chatgpt", summary="ChatGPT / Codex 桌面版：热切换模型")
async def set_chatgpt_model(
    container: ContainerDep, payload: dict[str, str] = Body(...)
) -> dict[str, Any]:
    """Promote *model* to the front of zk-auto's target chain - takes effect
    on the next request, no desktop restart needed. The original chain order is
    preserved behind the new first target so fallback still works."""
    model = payload.get("model", "").strip()
    if not model:
        raise HTTPException(
            status_code=400, detail={"error": {"message": "必须填写模型名"}}
        )
    known = _chatgpt_choices(container)
    if model not in known:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": f"未知的模型或别名：{model}"}},
        )

    existing = container.config.aliases.get(CHATGPT_ALIAS)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"message": f"别名 '{CHATGPT_ALIAS}' 不存在"}},
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
        "path": str(chatgpt_service.config_toml_path()),
    }


@router.put("/chatgpt/client", summary="保存期望的客户端配置（可选同时写入本机）")
async def put_chatgpt_client(
    container: ContainerDep, payload: ChatGptClientPayload
) -> dict[str, Any]:
    """Save ``config/chatgpt.yaml``; with ``apply=true`` (default) also rewrite
    ``~/.codex/config.toml`` right away (backup first, app sections untouched)."""
    desired = chatgpt_service.ChatGptConfig(
        mode=payload.mode.strip() or "zk-ai",
        model=payload.model.strip(),
        model_provider=payload.model_provider.strip() or "zkai",
        provider_display=payload.provider_display.strip() or "ZK-AI",
        # blank base_url means "this machine's gateway" - fill in the live port
        base_url=payload.base_url.strip()
        or f"http://127.0.0.1:{container.settings.port}/v1",
        wire_api=payload.wire_api.strip() or "responses",
        env_key=payload.env_key.strip() or "ZKAI_API_TOKEN",
        model_reasoning_effort=payload.model_reasoning_effort.strip(),
        official_model=payload.official_model.strip(),
    )
    _validate_or_400(desired)

    warnings: list[str] = []
    if desired.mode == "zk-ai" and desired.model:
        # 换机事故复盘：config.toml 的 model 写成一个网关没有的名字（如 'zk'），
        # 桌面版每条消息都是 404。这里的名单就是路由器的准入集（全部模型+别名），
        # 不在里面的值写进去也不可能work——直接拦，附上可选清单。
        known = container.config.known_names()
        if desired.model not in known:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": f"模型 '{desired.model}' 不在网关配置里——桌面版发这个名字会 404。"
                        f"可用的别名/模型：{', '.join(known)}"
                    }
                },
            )
    port_note = _base_url_port_note(desired.base_url, container)
    if port_note:
        warnings.append(port_note)
    warnings.extend(_base_url_warnings(desired, container))

    desired_path = _desired_config_path(container)
    try:
        chatgpt_service.save_desired(desired_path, desired)
    except OSError as exc:
        raise _file_write_failed(exc) from exc

    result: dict[str, Any] = {
        "ok": True,
        "saved": str(desired_path),
        "config_path": str(chatgpt_service.config_toml_path()),
        "warnings": warnings,
        "applied": False,
    }
    if payload.apply:
        result.update(_apply_desired(chatgpt_service.config_toml_path(), desired))
    return result


@router.post("/chatgpt/apply", summary="按已保存的期望配置重写 ~/.codex/config.toml")
async def apply_chatgpt_client(container: ContainerDep) -> dict[str, Any]:
    """Re-materialise the client config from ``config/chatgpt.yaml`` - e.g. the
    desktop app regenerated its file, or this is a fresh machine."""
    desired = _load_desired_or_400(container)
    if desired is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "message": "还没有保存过客户端配置——先在控制台「🤖 ChatGPT」面板保存一份"
                }
            },
        )
    _validate_or_400(desired)
    return {"ok": True, **_apply_desired(chatgpt_service.config_toml_path(), desired)}


@router.post("/chatgpt/sync-env", summary="把网关令牌同步进用户级环境变量")
async def sync_chatgpt_env(container: ContainerDep) -> dict[str, Any]:
    """One-click recovery from "Missing environment variable" / stale token.

    The desktop app authenticates with the user-level (HKCU\\Environment)
    variable named by ``env_key`` - it never reads ``.env``. Rotating
    ``ZKAI_API_TOKEN`` in .env (or landing on a fresh machine) leaves the app
    with a missing/old value; this writes the gateway's current token there
    and verifies by reading it back.
    """
    disk = chatgpt_service.read_disk_state(chatgpt_service.config_toml_path())
    try:
        desired = _load_desired_or_400(container)
    except HTTPException:
        desired = None  # yaml 坏不该拦住同步——变量名以磁盘 config.toml 为准
    name = disk.env_key or (desired.env_key if desired else "") or "ZKAI_API_TOKEN"
    token = container.settings.api_token
    if not token:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "网关未设置 ZKAI_API_TOKEN（.env 里没有）——此时 /v1/* 开放，"
                    "桌面版不需要它，无需同步"
                }
            },
        )
    try:
        written, _old, note = chatgpt_service.write_user_env_var(name, token)
    except OSError as exc:  # pragma: no cover - registry write failures
        raise _file_write_failed(exc) from exc
    if not written:
        raise HTTPException(status_code=400, detail={"error": {"message": note or "写入失败"}})
    verified = chatgpt_service.read_user_env_var(name) == token
    return {
        "ok": True,
        "name": name,
        "verified": verified,
        "message": f"已同步 {name} 到用户级环境变量；重启 ChatGPT 桌面版（含托盘退出）后生效",
    }


def _base_url_port_note(base_url: str, container: ContainerDep) -> str | None:
    """Warn when the client would talk to a port the gateway is not listening on."""
    match = re.search(r":(\d{1,5})(?:/|$)", base_url)
    if match and int(match.group(1)) != container.settings.port:
        return (
            f"base_url 端口 {match.group(1)} 与网关当前端口 {container.settings.port} 不一致"
            "——换机后如改过端口，在这里改回來"
        )
    return None


def _apply_desired(
    config_file: Path, desired: chatgpt_service.ChatGptConfig
) -> dict[str, Any]:
    try:
        applied = chatgpt_service.apply_config(config_file, desired)
    except ValueError as exc:
        # 坏 TOML / 手术验证不过：chatgpt_service 保证原文件未动，原样转述
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}}) from exc
    except OSError as exc:
        raise _file_write_failed(exc) from exc
    return {
        "applied": True,
        "no_op": applied.no_op,
        "apply_result": applied.as_dict(),
    }


# --------------------------------------------------------------------------- #
# 积分消耗器（scripts/burn_sensenova.py）——配置 + 运行状态
#
# 消耗器是独立进程、不经网关，控制台原本完全看不到它。这里把它的配置
# （config/burner.yaml）和账本（data/burn_state.json）接进控制台：窗口余量、
# 每账号 5h/周边界、剩余可烧条数都能看，参数改完写回 YAML，重启消耗器即生效。
# --------------------------------------------------------------------------- #
def _burner_paths() -> tuple[Path, Path]:
    """(账本, burner.yaml)。

    账本走 `Settings.burner_state_path`：服务器上 burner 由 systemd 用
    --state-file 指到 `/var/lib/zkai/burner/`，比网关 data_dir 深一层。
    直接拼 data_dir 会让控制台去读写一个不存在的账本——运营者以为校准
    生效了，真正的 burner 什么都没看到。
    """
    settings = load_app_config().settings
    return (settings.burner_state_path,
            settings.resolved_config_dir / "burner.yaml")


@router.get("/burner", summary="积分消耗器配置与运行状态")
async def burner_snapshot() -> dict[str, Any]:
    state_file, config_file = _burner_paths()
    return burner_service.snapshot(state_file, config_file)


@router.put("/burner/config", summary="保存积分消耗器配置")
async def burner_save_config(
    payload: dict[str, Any] = Body(...),
    restart: Annotated[bool, Query(description="保存后请求重启消耗器（改配置才会生效）")] = True,
) -> dict[str, Any]:
    """写入 config/burner.yaml（保留注释），返回新的快照。

    写失败整体回滚：文件用临时文件 + replace，异常时原文件不动，避免界面显示
    成功而磁盘还是旧值。

    ``restart=true``（默认）时顺带留一个重启请求，托盘心跳看到就重启 burner——
    它只在自己启动时读配置，不重启的话这次保存对运行中的实例无效。重启由托盘
    异步完成，这里只承诺「已请求」，所以返回里带 ``restart_pending`` 让界面能提示。
    """
    state_file, config_file = _burner_paths()
    try:
        patch = burner_service.validate_patch(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "burner_config_invalid"}},
        ) from exc
    try:
        changed = burner_service.write_config(config_file, patch)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": {
                "message": f"消耗器配置写入失败，本次改动已取消：{exc}",
                "type": "burner_config_write_failed"}},
        ) from exc
    logger.info("burner config updated: %s", ", ".join(sorted(changed)))
    if restart:
        with contextlib.suppress(OSError):
            burner_service.request_restart(state_file.parent)
        logger.info("burner restart requested (tray will pick it up within ~3s)")
    return burner_service.snapshot(state_file, config_file)

# --------------------------------------------------------------------------- #
# 消耗器：账号核对与费率校准
#
# 运营者从商汤后台看回真实用量后从这里写回。三个端点：
#   GET  /admin/burner/reconcile   当前账本 + 配置的结构化快照（表单初值）
#   POST /admin/burner/reconcile   只读预演，返回逐账号 diff（保存前确认弹窗）
#   PUT  /admin/burner/reconcile   真正写账本 + burner.yaml，并重启
#   POST /admin/burner/calibrate   按实扣积分反推费率（采纳后走 reconcile 落盘）
# --------------------------------------------------------------------------- #
@router.get("/burner/reconcile", summary="账号核对的当前快照（账本 + 配置）")
async def burner_reconcile_state() -> dict[str, Any]:
    state_file, config_file = _burner_paths()
    config = burner_service.read_config(config_file)
    status = burner_service.read_status(state_file, config)
    now = time.time()
    cost = burner_service.estimate_request_cost(config)
    global_week_anchor = burner_service._parse_global_week_anchor(
        str(config.get("week_anchor") or "Mon 00:00"), now
    )
    accounts = [
        burner_service.account_view(a, now, global_week_anchor, status, cost)
        for a in status.accounts
    ]
    accounts.sort(key=lambda a: a["name"])
    return {
        "ok": True,
        "accounts": [
            {
                "name": a["name"],
                "credits_total": a["credits_total"],
                "burned_5h": a["burned_5h"],
                "burned_week": a["burned_week"],
                "cap_5h": a["cap_5h"],
                "cap_week": a["cap_week"],
                "anchor": burner_service._fmt_hhmm(
                    burner_service.account_anchor_ts(
                        config, a["name"], now
                    )
                ),
                "week_anchor": burner_service._fmt_weekday_hhmm(
                    burner_service.account_week_anchor_ts(
                        config, a["name"], now
                    )
                ),
                "is_burning": not a["parked"],
                "parked": a["parked"],
            }
            for a in accounts
        ],
        "config": {
            "only": burner_service.only_to_set(str(config.get("only") or "")),
            "anchors": burner_service.anchors_to_accounts(str(config.get("anchors") or "")),
            "week_anchors": burner_service.week_anchors_to_accounts(
                str(config.get("week_anchors") or "")
            ),
            "rate_in": status.rate_in,
            "rate_out": status.rate_out,
            "window_credits": status.window_credits,
            "weekly_credits": status.weekly_credits,
            "safety_margin": status.safety_margin,
        },
        "ledger_saved_at": status.saved_at,
        "restart_keys": list(burner_service.RECONCILE_RESTART_KEYS),
        "warnings": burner_service._warnings(config, status, accounts),
    }


@router.post("/burner/reconcile", summary="账号核对预演（只读，不写盘）")
async def burner_reconcile_preview(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    state_file, config_file = _burner_paths()
    try:
        diff = burner_service.reconcile_diff(
            state_file, burner_service.read_config(config_file), payload
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "burner_reconcile_invalid"}},
        ) from exc
    return {"ok": True, **diff}


@router.put("/burner/reconcile", summary="账号核对落盘（账本 + 配置 + 重启）")
async def burner_reconcile_apply(
    payload: dict[str, Any] = Body(...),
    restart: Annotated[bool, Query(description="写入后重启消耗器")] = True,
) -> dict[str, Any]:
    state_file, config_file = _burner_paths()
    try:
        result = burner_service.apply_reconcile(state_file, config_file, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "burner_reconcile_invalid"}},
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": {
                "message": f"写入失败：{exc}",
                "type": "burner_reconcile_write_failed"}},
        ) from exc
    logger.info(
        "burner reconciled: ledger=%s config=%s",
        ",".join(result["ledger_written"]) or "-",
        ",".join(result["config_written"]) or "-",
    )
    restart_result: dict[str, Any] = {"mode": "none", "ok": False, "message": "未请求重启"}
    if restart:
        restart_result = burner_service.request_restart_ex(state_file.parent)
    snapshot = burner_service.snapshot(state_file, config_file)
    return {
        "ok": True,
        "reconcile": result,
        "restart": restart_result,
        "snapshot": snapshot,
    }


@router.post("/burner/calibrate", summary="按实扣积分反推费率")
async def burner_calibrate(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    state_file, config_file = _burner_paths()
    try:
        actual = float(payload.get("actual_credits") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": "实扣积分要填数字"}},
        ) from exc
    try:
        result = burner_service.suggest_calibration(
            state_file,
            burner_service.read_config(config_file),
            actual,
            str(payload.get("account") or "").strip(),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "burner_calibrate_invalid"}},
        ) from exc
    # 采纳时把建议费率并进 reconcile 的 payload——两条路径共用同一套写入与重启，
    # 否则会出现「校准算了但没写」或「写了但没重启」两条分叉。
    if payload.get("adopt"):
        try:
            burner_service.apply_reconcile(
                state_file,
                config_file,
                {"rate_in": result["suggested"]["rate_in"],
                 "rate_out": result["suggested"]["rate_out"]},
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": {"message": str(exc), "type": "burner_calibrate_invalid"}},
            ) from exc
        result["adopted"] = True
        result["restart"] = burner_service.request_restart_ex(state_file.parent)
    else:
        result["adopted"] = False
    return {"ok": True, "calibration": result}
