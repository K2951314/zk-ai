"""YAML loading, Settings<->config.yaml bridging, and the config cache.

Loads the three YAML files (config / providers / models), parses them into
:class:`AppConfig`, applies DB overrides, and caches the result process-wide.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.core.config.appconfig import AppConfig
from app.core.config.env_health import load_dotenv_file, shadowed_env_names
from app.core.config.settings import (
    PROJECT_ROOT,
    Settings,
    deep_merge,
    interpolate_env,
    logger,
)
from app.core.errors import ConfigError
from app.models.provider import (
    AliasStrategy,
    ModelAliasConfig,
    ModelConfig,
    ProviderConfig,
)
from app.retry.policy import RetryPolicy

# --------------------------------------------------------------------------- #
# Settings <-> config.yaml bridging
# --------------------------------------------------------------------------- #
#: ``config.yaml`` path -> Settings field. Environment variables always win.
_SETTINGS_SECTIONS: dict[str, tuple[str, str]] = {
    "app.name": ("app", "app_name"),
    "app.environment": ("app", "environment"),
    "app.host": ("app", "host"),
    "app.port": ("app", "port"),
    "logging.level": ("logging", "log_level"),
    "logging.json": ("logging", "log_json"),
    "database.url": ("database", "database_url"),
    "database.echo": ("database", "db_echo"),
    "admin.enabled": ("admin", "admin_enabled"),
    "admin.token": ("admin", "admin_token"),
    "request.timeout": ("request", "request_timeout"),
    "request.default_max_tokens": ("request", "default_max_tokens"),
    # 2026-09-28：补上桥接。AGENTS.md 记着 ZKAI_MAX_REQUEST_BODY /
    # ZKAI_MAX_INPUT_TOKENS 没有桥接的坑——改 config.yaml 静默无效，用户只能
    # 改 .env。新的 config.yaml 键一律同时登记在这里，别再犯第二次。
    "request.trim_history_tokens": ("request", "trim_history_tokens"),
    "health_check.mode": ("health_check", "health_check_mode"),
    "health_check.interval_seconds": ("health_check", "health_check_interval"),
    "health_check.on_startup": ("health_check", "health_check_on_startup"),
    "health_check.timeout": ("health_check", "health_check_timeout"),
    "security.allow_inline_secrets": ("security", "allow_inline_secrets"),
    # 部署形态：公网入口 + 形态标签。缺失 = 本机部署，回落到 host:port。
    "deploy.public_base_url": ("deploy", "public_base_url"),
    "deploy.exposure": ("deploy", "exposure"),
}


def _env_precedence_set(field: str) -> bool:
    """True when an explicit ``ZKAI_<FIELD>`` environment variable exists."""
    return os.environ.get(f"ZKAI_{field.upper()}") not in (None, "")


def apply_settings_from_yaml(settings: Settings, data: dict[str, Any]) -> Settings:
    """Overlay ``config.yaml`` sections onto *settings* (env vars take priority)."""
    updates: dict[str, Any] = {}
    for path, (section, field_name) in _SETTINGS_SECTIONS.items():
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        key = path.split(".", 1)[1]
        if key not in block:
            continue
        if _env_precedence_set(field_name):
            continue
        updates[field_name] = block[key]
    if not updates:
        return settings
    validated = Settings(**{**settings.model_dump(), **updates})
    return validated


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - fs errors
        raise ConfigError(f"cannot read config file '{path}': {exc}") from exc
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file '{path}' must contain a mapping at the top level")
    interpolated = interpolate_env(data)
    if not isinstance(interpolated, dict):  # pragma: no cover - defensive
        raise ConfigError(f"config file '{path}' produced a non-mapping document")
    return interpolated


def _pick(config_dir: Path, stem: str) -> Path | None:
    """Return ``<stem>.yaml`` if present, else ``<stem>.example.yaml``."""
    for candidate in (config_dir / f"{stem}.yaml", config_dir / f"{stem}.yml"):
        if candidate.exists():
            return candidate
    for candidate in (config_dir / f"{stem}.example.yaml", config_dir / f"{stem}.example.yml"):
        if candidate.exists():
            return candidate
    return None


def _parse_providers(data: dict[str, Any]) -> dict[str, ProviderConfig]:
    entries = data.get("providers") or []
    if not isinstance(entries, list):
        raise ConfigError("providers.yaml: 'providers' must be a list")
    providers: dict[str, ProviderConfig] = {}
    for entry in entries:
        try:
            provider = ProviderConfig(**entry)
        except ValidationError as exc:
            raise ConfigError(f"invalid provider definition: {exc}") from exc
        if provider.id in providers:
            raise ConfigError(f"duplicate provider id '{provider.id}'")
        providers[provider.id] = provider
    return providers


def _parse_models(data: dict[str, Any]) -> tuple[dict[str, ModelConfig], dict[str, ModelAliasConfig]]:
    entries = data.get("models") or []
    if not isinstance(entries, list):
        raise ConfigError("models.yaml: 'models' must be a list")
    from app.models.provider import DeploymentConfig

    models: dict[str, ModelConfig] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("models.yaml: every model entry must be a mapping")
        payload = dict(entry)
        model_id = str(payload.get("id") or "")
        raw_deployments = payload.pop("deployments", []) or []
        # Deployments are validated explicitly so that ``id``/``model`` can be
        # defaulted from the parent model (nicer YAML, fewer surprises).
        deployments: list[DeploymentConfig] = []
        for index, deployment in enumerate(raw_deployments):
            if not isinstance(deployment, dict):
                raise ConfigError(f"model '{model_id}': deployments must be mappings")
            item = dict(deployment)
            item.setdefault("id", f"{model_id}-{index + 1}")
            item.setdefault("model", item.get("model") or model_id)
            try:
                deployments.append(DeploymentConfig(**item))
            except ValidationError as exc:
                raise ConfigError(f"invalid deployment for model '{model_id}': {exc}") from exc

        try:
            model = ModelConfig(**payload)
        except ValidationError as exc:
            raise ConfigError(f"invalid model definition '{model_id}': {exc}") from exc
        model.deployments = deployments
        if model.id in models:
            raise ConfigError(f"duplicate model id '{model.id}'")
        models[model.id] = model

    aliases: dict[str, ModelAliasConfig] = {}
    for entry in data.get("aliases") or []:
        try:
            alias = ModelAliasConfig(**entry)
        except ValidationError as exc:
            raise ConfigError(f"invalid alias definition '{entry.get('name')}': {exc}") from exc
        aliases[alias.name] = alias
    return models, aliases


def _default_aliases(models: dict[str, ModelConfig]) -> dict[str, ModelAliasConfig]:
    """Fallback aliases when models.yaml defines none, so ``zk-*`` always resolves."""
    present = set(models.keys())
    presets: dict[str, tuple[list[str], AliasStrategy, str]] = {
        "zk-auto": ([], AliasStrategy.CAPABILITY, "balanced default: best all-round model"),
        "zk-coding": ([], AliasStrategy.CAPABILITY, "optimised for code generation"),
        "zk-reasoning": ([], AliasStrategy.CAPABILITY, "optimised for deep reasoning"),
        "zk-fast": ([], AliasStrategy.SPEED, "lowest latency first"),
        "zk-cheap": ([], AliasStrategy.COST, "lowest cost first"),
    }
    aliases: dict[str, ModelAliasConfig] = {}
    for name, (_, strategy, description) in presets.items():
        if name in present:
            continue
        ordered = sorted(models.values(), key=lambda m: m.id)
        aliases[name] = ModelAliasConfig(
            name=name,
            targets=[m.id for m in ordered],
            strategy=strategy,
            description=description,
        )
    return aliases


def load_app_config(settings: Settings | None = None) -> AppConfig:
    """Load and validate the full configuration."""
    load_dotenv_file()
    settings = settings or Settings()
    config_dir = settings.resolved_config_dir

    config_path = _pick(config_dir, "config")
    providers_path = _pick(config_dir, "providers")
    models_path = _pick(config_dir, "models")

    files: dict[str, str] = {}
    config_data: dict[str, Any] = {}
    if config_path:
        config_data = _read_yaml(config_path)
        files["config"] = config_path.name
        # `config.yaml` may carry application settings (host/port/log/db/admin...).
        settings = apply_settings_from_yaml(settings, config_data)

    provider_data: dict[str, Any] = {}
    if providers_path:
        provider_data = _read_yaml(providers_path)
        files["providers"] = providers_path.name
    # config.yaml may embed providers/models for single-file setups.
    provider_data = deep_merge(config_data.get("providers_data") or {}, provider_data)
    if "providers" in config_data:
        provider_data = deep_merge({"providers": config_data["providers"]}, provider_data)

    model_data: dict[str, Any] = {}
    if models_path:
        model_data = _read_yaml(models_path)
        files["models"] = models_path.name
    if "models" in config_data:
        model_data = deep_merge({"models": config_data["models"]}, model_data)
    if "aliases" in config_data:
        model_data = deep_merge({"aliases": config_data["aliases"]}, model_data)

    providers = _parse_providers(provider_data)
    models, aliases = _parse_models(model_data)
    if not aliases:
        aliases = _default_aliases(models)

    app_config = AppConfig(
        settings=settings,
        providers=providers,
        models=models,
        aliases=aliases,
        retry=RetryPolicy.from_mapping(config_data.get("retry")),
        raw=config_data,
        source_files=files,
    )

    for problem in app_config.validate():
        logger.warning("config problem: %s", problem)
        app_config.warnings.append(problem)
    # Only complain about shadows that matter: a .env name whose value is ignored
    # because the process already has a *different* one, and that name is actually
    # referenced as a provider credential. (A stale ZKAI_* in the user profile is
    # not our business; a stale SENSENOVA_API_KEY silently duplicates a key.)
    used_refs: set[str] = set()
    for provider in providers.values():
        for credential in provider.credentials:
            ref = credential.env_reference() or ""
            match = re.match(r"^\$\{(\w+)", ref)
            if match:
                used_refs.add(match.group(1))
    for name in shadowed_env_names(PROJECT_ROOT / ".env"):
        if name in used_refs:
            app_config.warnings.append(
                f"credential env var {name} is shadowed by a stale process "
                "environment variable with a different value - the .env entry is ignored"
            )
    # 记下这三个文件此刻的 mtime：config_writer 靠它判断「有没有人在外面改过」。
    # 不记的话，控制台保存会把内存里的旧值写回文件、静默回滚外部编辑（实测踩过）。
    from app.core import config_writer as _cw

    for _path in (config_path, providers_path, models_path):
        if _path:
            _cw.note_loaded(_path)
    return app_config


_cache: AppConfig | None = None


def apply_db_overrides(
    config: AppConfig,
    provider_limit_rows: dict[str, list[dict[str, Any]]] | None = None,
) -> AppConfig:
    """Re-apply the provider quota rules that could not be written to YAML.

    Models/aliases edited through the console are **written back to the YAML files**
    (see :mod:`app.core.config_writer`), so the file is the single source of truth and
    nothing is replayed from the DB mirror - a hand-deleted file entry therefore stays
    deleted across reloads and restarts. ``provider_limit_rows`` is the sole exception:
    it only carries rules from template-only/read-only setups where the
    ``providers.yaml`` write was impossible (the console flags them with
    ``rate_limits_source=console``).
    """
    for provider_id, rules in (provider_limit_rows or {}).items():
        provider = config.providers.get(provider_id)
        if provider is None:
            continue
        provider.options = {**provider.options, "rate_limits": list(rules)}
    return config


def get_app_config(*, refresh: bool = False) -> AppConfig:
    """Process-wide cached configuration (call ``refresh=True`` to reload)."""
    global _cache
    if _cache is None or refresh:
        _cache = load_app_config()
    return _cache


def reset_config_cache() -> None:
    """Drop the cached configuration (used by tests and the reload endpoint)."""
    global _cache
    _cache = None
