"""ChatGPT / Codex desktop integration endpoints (``/admin/chatgpt/*``).

The desktop app reads config.toml once at startup and never reloads it, so
rewriting ``model = ...`` there only applies after an app restart. But the
app already sends ``model = "zk-auto"`` and the router resolves aliases
per-request. The console therefore has two levels:

* **hot swap** (``POST /admin/chatgpt``) - reorders zk-auto's target chain;
  the next message in a running conversation already uses the new model;
* **client config** (``PUT /admin/chatgpt/client`` / ``POST
  /admin/chatgpt/apply``) - rewrites the *file* itself (base_url, wire_api,
  env_key, reasoning effort, official-model switch) through the surgical
  patcher in :mod:`app.services.chatgpt_service`. config.toml is owned by
  the app, so only our keys are touched and a ``.bak-<stamp>`` is kept.

The desired config lives in ``config/chatgpt.yaml`` (single source of truth,
travels inside the migration package - importing on a new machine via
一键换机 Skill does NOT auto-configure the client; use this panel after import).
"""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Body, HTTPException
from pydantic import BaseModel

from app.api.admin._common import _file_write_failed, _write_alias_file, router
from app.api.deps import ContainerDep
from app.models.provider import ModelAliasConfig
from app.services import chatgpt_service

#: The alias the desktop app already sends.
CHATGPT_ALIAS = "zk-auto"


class ChatGptClientPayload(BaseModel):
    """Console form -> desired client config. Empty/blank = fall back to defaults."""

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
