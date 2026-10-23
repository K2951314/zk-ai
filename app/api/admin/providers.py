"""Provider, credential, quota and health endpoints.

Covers ``/admin/providers/*``, ``/admin/credentials/*`` and ``/admin/health/*``.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import Body, HTTPException, Query, status

from app.api.admin import _common
from app.api.admin._common import (
    AddCredentialRequest,
    HealthCheckRequest,
    ProviderLimitsRequest,
    ProviderUpsertRequest,
    VerifyModelsRequest,
    _file_write_failed,
    _source_file,
    _strip_nulls,
    _write_limits_file,
    logger,
    router,
)
from app.api.deps import ContainerDep
from app.core.config import load_app_config
from app.core.config_writer import (
    append_credential_to_provider,
    delete_credential_from_provider,
    delete_env_var,
    looks_like_secret,
    update_credential_in_provider,
    upsert_env_var,
)
from app.core.config_writer import delete_provider as delete_provider_file
from app.core.config_writer import upsert_provider as upsert_provider_file
from app.models.provider import (
    DeploymentConfig,
    ModelConfig,
)
from app.models.request import ChatCompletionRequest, ChatMessage
from app.providers.base import ProviderContext
from app.retry.classifier import ErrorClassifier
from app.routing.limits import RateLimitRule


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
    from app.models.presets import highest_effort, preset_for
    from app.models.pricing import lookup, reference_prices

    # 上游的模型列表只说 id：描述、上下文窗口这些事实要去模型社区的资料页取。
    # 取不到就不补（官方没说的不猜），网络问题也不能让整个市场失败。
    official: dict[str, dict[str, Any]] = {}
    if "modelscope" in (provider.base_url or "").lower():
        try:
            from app.models.catalogue_info import enrich_modelscope

            official = await enrich_modelscope(sorted(catalogue))
        except Exception as exc:
            logger.info("catalogue enrich failed for %s: %s", provider_id, exc)

    # 参考价：上游目录不报价，去公开渠道取挂牌价。
    # 口径必须标清：这是参考价，不是供应商给的结算价。
    prices: dict[str, dict[str, Any]] = {}
    try:
        prices = await reference_prices()
    except Exception as exc:
        logger.info("reference price fetch failed for %s: %s", provider_id, exc)

    data = []
    for upstream in sorted(catalogue):
        # 供应商专属预设优先：同一模型（如 kimi-k3）在商汤/月之暗面的档位表不一样
        preset = preset_for(upstream, provider_id)
        # Measured facts outrank the hand-written preset for the fields the
        # provider actually reported; anything it stayed silent about keeps the
        # preset value, and ``fallback`` records where each number came from so
        # the console can label it instead of presenting a guess as fact.
        facts = model_facts(catalogue[upstream])
        info = official.get(upstream) or {}
        capabilities = dict(preset.capabilities)
        if facts.vision_input is not None:
            capabilities["vision"] = 10.0 if facts.vision_input else 0.0
        if facts.reasoning is not None:
            capabilities["reasoning"] = max(capabilities.get("reasoning", 0.0), 8.0)
        # 取数优先级：供应商实测 > 社区官方资料 > 预置表猜测。
        if info.get("vision") and capabilities.get("vision", 0.0) <= 0.0:
            capabilities["vision"] = 8.0
        if info.get("reasoning"):
            capabilities["reasoning"] = max(capabilities.get("reasoning", 0.0), 8.0)
        if info.get("tool_use"):
            capabilities["tool_use"] = max(capabilities.get("tool_use", 0.0), 7.5)
        quoted = lookup(prices, upstream) or {}
        input_price = preset.input_price or float(quoted.get("input_cost_per_mtok") or 0.0)
        output_price = preset.output_price or float(quoted.get("output_cost_per_mtok") or 0.0)
        if preset.input_price:
            price_source = "preset"
        elif quoted:
            price_source = "reference"
        else:
            price_source = None

        entry: dict[str, Any] = {
            "upstream_model": upstream,
            "suggested_model_id": _suggest_model_id(upstream),
            "already_added": preset.family in known_models
            or upstream in known_upstream,
            "preset": {
                "family": preset.family,
                "display_name": info.get("display_name") or preset.display_name,
                "context_window": info.get("context_window") or preset.context_window,
                "capabilities": capabilities,
                "input_price": input_price,
                "output_price": output_price,
                "price_source": price_source,
                "description": info.get("description") or preset.description,
                "reasoning_efforts": list(preset.reasoning_efforts),
                "reasoning_effort_default": preset.reasoning_effort_default,
                "max_output_tokens": preset.max_output_tokens,
                "max_output_tokens_max": preset.max_output_tokens_max,
                "spec_source": preset.spec_source,
            },
            "official": info,
            "discovered": facts.as_dict(),
            "discovered_known": facts.known,
            # 推理力度档位：探测（reasoning_effort_support_list）有就用探测的；
            # 探测没有就回落官方文档预设——商汤 /models 根本不回档位字段，
            # 只靠探测永远「档位未知」，而官方文档明明写着 deepseek-flash 是
            # none/low/high/max。没预设的仍留空，不猜。
            "reasoning_efforts": (
                list(facts.reasoning_effort)
                or list(preset.reasoning_efforts)
            ),
            "reasoning_efforts_source": (
                "discovery" if facts.reasoning_effort
                else ("official" if preset.reasoning_efforts else None)
            ),
            "reasoning_effort_default": preset.reasoning_effort_default,
            # 运营者默认上最高档（none 除外，那是关思考）
            "reasoning_effort_recommended": highest_effort(
                list(facts.reasoning_effort) or list(preset.reasoning_efforts)
            ),
            # 官方规格来源：official = 官方文档核实；estimated = 预估，表单要标出来
            "spec_source": preset.spec_source,
            "max_output_tokens": preset.max_output_tokens,
            "max_output_tokens_max": preset.max_output_tokens_max,
            # Higher-fidelity-vendor presets are only a guess for StepFun's own
            # models; anything else keeps the preset context window.
            "context_window_source": (
                "provider" if facts.context_window
                else "official" if info.get("context_window")
                else "preset"
            ),
        }
        if facts.known:
            entry["facts_note"] = facts.describe()
        if preset.spec_source == "official":
            entry["official_facts"] = preset.official_facts()
        data.append(entry)
    return {"object": "list", "provider_id": provider_id, "data": data}


def _suggest_model_id(upstream: str) -> str:
    """A gateway-facing id from an upstream name: ``z-ai/glm-5.3`` -> ``glm-5.3``."""
    return upstream.split("/")[-1].strip()


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


@router.post("/health/check", summary="立即执行健康检查")
async def run_health_check(
    container: ContainerDep, payload: HealthCheckRequest | None = Body(default=None)
) -> dict[str, Any]:
    """Manual probe (mode ``manual`` uses this endpoint)."""
    request = payload or HealthCheckRequest()
    return await container.health_service.check_all(
        kind="manual", provider_ids=request.providers
    )


# --------------------------------------------------------------------------- #
# Provider quota rules
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Credential administration
# --------------------------------------------------------------------------- #
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
            replaced = upsert_env_var(_common._env_file(), payload.env_var, payload.value)
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


@router.put("/providers/{provider_id}/credentials/{credential_id}", summary="更新凭据（换 Key / 调参数）")
async def update_credential(
    provider_id: str,
    credential_id: str,
    payload: _common.UpdateCredentialRequest,
    container: ContainerDep,
) -> dict[str, Any]:
    """Rotate a credential in place; persists across restarts.

    The piece whose absence forced delete-then-readd: ``value`` lands in ``.env``
    under the credential's env var (existing name, or ``env_var`` when renaming),
    so an expired or leaked key is replaced without losing the entry's identity,
    priority or fail counters. Renaming unifies the legacy ``env`` on ``env_var``.
    """
    provider = container.config.providers.get(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"供应商 '{provider_id}' 不存在"}},
        )
    current = next((c for c in provider.credentials if c.id == credential_id), None)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"message": f"凭据 '{credential_id}' 不存在"}},
        )
    old_name = current.env_var or (current.env.strip("${} ") if current.env else None)
    new_name = payload.env_var or old_name
    env_synced: str | None = None
    if payload.value is not None:
        if not new_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "message": "该凭据还没有环境变量名，先在 env_var 里指定一个再换 Key",
                        "type": "invalid_credential",
                    }
                },
            )
        try:
            replaced = upsert_env_var(_common._env_file(), new_name, payload.value)
        except OSError as exc:
            raise _file_write_failed(exc) from exc
        # Live-export so the new key works on the next request, not after a reload.
        os.environ[new_name] = payload.value
        env_synced = f".env ({'updated' if replaced else 'appended'})"
    fields: dict[str, Any] = {}
    if payload.env_var is not None:
        fields["env"] = None  # 统一走 env_var，清掉历史遗留的 env 写法
        fields["env_var"] = payload.env_var
    if payload.priority is not None:
        fields["priority"] = payload.priority
    if payload.enabled is not None:
        fields["enabled"] = payload.enabled
    synced = None
    path = _source_file(container, "providers")
    if path is not None and fields:
        try:
            update_credential_in_provider(path, provider_id, credential_id, fields)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": {"message": str(exc)}},
            ) from exc
        synced = path.name
        container.refresh_config_watch()
    # 改名后旧变量若再无凭据引用，顺手从 .env 清掉，杜绝残留复活。
    if payload.env_var is not None and old_name and old_name != payload.env_var:
        still_used = any(
            (c.env_var or (c.env.strip("${} ") if c.env else None)) == old_name
            for p in container.config.providers.values()
            for c in p.credentials
            if c.id != credential_id
        )
        if not still_used:
            delete_env_var(_common._env_file(), old_name)
            os.environ.pop(old_name, None)
    for c in provider.credentials:
        if c.id != credential_id:
            continue
        if payload.env_var is not None:
            c.env = None
            c.env_var = payload.env_var
        if payload.priority is not None:
            c.priority = payload.priority
        if payload.enabled is not None:
            c.enabled = payload.enabled
    container.pool.register_provider(provider)
    return {
        "object": "credential",
        "provider_id": provider_id,
        "credential_id": credential_id,
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
    doomed = next((c for c in provider.credentials if c.id == credential_id), None)
    env_name = (
        doomed.env_var or (doomed.env.strip("${} ") if doomed.env else None)
        if doomed is not None
        else None
    )
    synced = None
    path = _source_file(container, "providers")
    if path is not None:
        delete_credential_from_provider(path, provider_id, credential_id)
        synced = path.name
        container.refresh_config_watch()
    provider.credentials = [c for c in provider.credentials if c.id != credential_id]
    container.pool.register_provider(provider)
    # 密钥本体也要走：变量若再无任何凭据引用，就从 .env 删掉，
    # 否则界面上删了、文件里还留着，重加同名凭据时旧 Key 又复活。
    env_removed = False
    if env_name:
        still_used = any(
            (c.env_var or (c.env.strip("${} ") if c.env else None)) == env_name
            for p in container.config.providers.values()
            for c in p.credentials
        )
        if not still_used:
            env_removed = delete_env_var(_common._env_file(), env_name)
            os.environ.pop(env_name, None)
    return {
        "deleted": credential_id,
        "provider_id": provider_id,
        "synced": synced,
        "env_removed": env_removed,
    }


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
# Provider CRUD
# --------------------------------------------------------------------------- #
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
