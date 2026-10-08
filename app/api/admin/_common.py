"""Shared helpers for the admin API surface.

YAML write-through helpers live here so every sub-module writes config files the
same way: stale-config detection, atomic writes and watcher re-seeding are
handled once instead of being re-implemented per endpoint group.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, require_admin
from app.core.config import PROJECT_ROOT
from app.core.config_writer import (
    delete_list_entry,
    sync_provider_rate_limits,
    upsert_list_entry,
)
from app.core.logging import get_logger
from app.models.provider import AliasStrategy

logger = get_logger("api.admin")

#: The single :class:`APIRouter` every sub-module registers routes on. Keeping
#: one instance (not one per file) means the ``/admin`` prefix, the ``admin``
#: tag and the ``require_admin`` dependency are declared exactly once.
router = APIRouter(
    prefix="/zkadmin", tags=["admin"], dependencies=[Depends(require_admin)]
)


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


def _write_model_file(container: ContainerDep, model: Any) -> str | None:
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


def _write_alias_file(container: ContainerDep, alias: Any) -> str | None:
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


def _delete_file_entry(
    container: ContainerDep, section: str, key: str, name: str
) -> str | None:
    path = _source_file(container, "models") or _source_file(container, "config")
    if path is None:
        return None
    try:
        delete_list_entry(path, section, key, name)
    except (OSError, ValueError) as exc:
        raise _file_write_failed(exc) from exc
    container.refresh_config_watch()  # 见 _write_model_file 的说明
    return path.name


def _write_limits_file(
    container: ContainerDep, provider_id: str, rules: list[dict]
) -> str | None:
    path = _source_file(container, "providers")
    if path is None:
        return None
    try:
        sync_provider_rate_limits(path, provider_id, rules)
    except (OSError, ValueError, KeyError) as exc:
        raise _file_write_failed(exc) from exc
    container.refresh_config_watch()  # 见 _write_model_file 的说明
    return path.name


def _env_file() -> Path:
    """The project ``.env`` (secrets live here, never in the YAML)."""
    return PROJECT_ROOT / ".env"


# --------------------------------------------------------------------------- #
# Request bodies shared across sub-modules
# --------------------------------------------------------------------------- #
class HealthCheckRequest(BaseModel):
    """Body for a manual health check."""

    providers: list[str] | None = Field(
        default=None, description="要探测的供应商 ID；留空 = 所有已启用的供应商"
    )
    credentials: bool = Field(default=True, description="是否逐把 Key 单独探测")


class ProviderLimitsRequest(BaseModel):
    """Body for setting a provider's proactive quota rules from the console."""

    rules: list[dict[str, Any]] = Field(default_factory=list)


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


class VerifyModelsRequest(BaseModel):
    """Body for ``POST /admin/providers/{id}/models/verify``."""

    models: list[str] = Field(default_factory=list)
    #: 单模型探测上限。短是刻意的——「挂死」本身就是结论。
    timeout_seconds: float = 20.0
    #: 一次最多探几个，防止误传 81 个模型把供应商惹毛。
    limit: int = 12


class ModelUpsertRequest(BaseModel):
    """Body for creating/replacing a model at runtime (web console editor)."""

    id: str
    display_name: str | None = None
    owned_by: str | None = None
    description: str | None = None
    enabled: bool = True
    context_window: int = 128_000
    capabilities: dict[str, float] = Field(default_factory=dict)
    deployments: list[Any] = Field(default_factory=list)


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
