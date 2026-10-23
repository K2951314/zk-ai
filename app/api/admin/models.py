"""Model, alias, routing and config-reload endpoints.

Covers ``/admin/models/*``, ``/admin/aliases/*``, ``/admin/router/*`` and
``/admin/config/reload``.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query, status

from app.api.admin._common import (
    AliasUpsertRequest,
    ModelUpsertRequest,
    _delete_file_entry,
    _write_alias_file,
    _write_model_file,
    logger,
    router,
)
from app.api.deps import ContainerDep
from app.models.provider import (
    ModelAliasConfig,
    ModelConfig,
)
from app.models.request import ChatCompletionRequest, ChatMessage


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
# Model administration (runtime edits persist in the DB mirror)
# --------------------------------------------------------------------------- #
@router.post("/models", summary="新建或替换模型")
async def upsert_model(payload: ModelUpsertRequest, container: ContainerDep) -> dict[str, Any]:
    """Hot-add/edit a model; takes effect immediately and survives restarts."""
    from app.models.provider import DeploymentConfig as _Dep

    # deployments 是 list[Any]（前端会带 extra 字段），逐个剥掉未知键再喂
    # ModelConfig（extra=forbid）——否则市场带回来的 reasoning_efforts 等
    # 证据字段会把整个保存炸成 500。
    dep_fields = set(_Dep.model_fields)
    raw = payload.model_dump()
    raw.pop("merge", None)  # 合并标志，不进 ModelConfig
    raw["deployments"] = [
        {k: v for k, v in dep.items() if k in dep_fields}
        for dep in (raw.get("deployments") or [])
    ]

    # 合并模式：同 ID 已存在时把新部署追加进去，不整体覆盖。
    # 市场「＋添加」同一模型的第二个供应商就走这条路。
    existing = container.config.models.get(payload.id)
    if payload.merge and existing is not None:
        seen = {d.id for d in existing.deployments}
        for dep_raw in raw["deployments"]:
            if dep_raw.get("id") not in seen:
                existing.deployments.append(_Dep(**dep_raw))
            else:
                # 同 ID 的部署更新字段（如 priority 被手动改了）
                for i, d in enumerate(existing.deployments):
                    if d.id == dep_raw["id"]:
                        existing.deployments[i] = _Dep(**dep_raw)
                        break
        _write_model_file(container, existing)
        await container.config_repository.upsert_model(existing)
        return {
            "object": "model",
            "merged": True,
            "data": container.model_service.describe(existing.id),
        }

    try:
        model = ModelConfig(**raw)
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

    # 先改内存立即生效（下一次请求就能看到），再落盘。
    # 落盘失败则回滚内存，保持文件与内存一致——编辑场景要恢复旧值，
    # 不能一 pop 了之（那会让旧模型从内存里消失，与文件里的条目不一致）。
    previous = container.config.models.get(model.id)
    container.config.models[model.id] = model
    try:
        synced = _write_model_file(container, model)
        await container.config_repository.upsert_model(model)
    except Exception:
        if previous is None:
            container.config.models.pop(model.id, None)
        else:
            container.config.models[model.id] = previous
        raise
    return {
        "object": "model",
        "synced": synced,
        "data": container.model_service.describe(model.id),
    }


@router.delete("/models/{model_id:path}", summary="删除模型")
async def delete_model(model_id: str, container: ContainerDep) -> dict[str, Any]:
    """Delete a model by id. ``:path`` lets ids containing ``/`` match
    (e.g. ``deepseek-ai/DeepSeek-V4.1-Flash`` from ModelScope)."""
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


@router.post("/gateway/restart", summary="一键重启网关")
async def restart_gateway(container: ContainerDep) -> dict[str, Any]:
    """按部署形态重启网关：systemd（服务器）/ 托盘（本机）/ 手动兜底。

    控制台一键搞定，不用再 SSH 到服务器敲 systemctl。本机网关不能重启
    自己，写请求文件由托盘心跳接手（约 3 秒）。
    """
    from app.services import gateway_restart

    data_dir = container.settings.resolved_data_dir
    result = gateway_restart.restart_gateway(data_dir)
    logger.info("gateway restart requested via console: %s", result)
    return {"object": "gateway_restart", **result}
