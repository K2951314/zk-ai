"""Configuration loading: ``.env`` -> :class:`Settings`, YAML -> :class:`AppConfig`.

Resolution order for the three YAML files (first existing wins)::

    config/config.yaml   ||  config/config.example.yaml
    config/providers.yaml||  config/providers.example.yaml
    config/models.yaml   ||  config/models.example.yaml

Values may reference environment variables with ``${VAR}`` or ``${VAR:-default}``
anywhere in the YAML, which keeps real API keys out of the repository.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values, load_dotenv
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ConfigError
from app.core.logging import get_logger
from app.models.provider import (
    AliasStrategy,
    ModelAliasConfig,
    ModelConfig,
    ProviderConfig,
)
from app.retry.policy import RetryPolicy

logger = get_logger("config")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
#: A name that can legitimately *name* an environment variable.
_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BOOL_TRUE = {"true", "yes", "on", "1"}
_BOOL_FALSE = {"false", "no", "off", "0"}


def _coerce(text: str) -> Any:
    """Best-effort scalar coercion for interpolated values."""
    lowered = text.strip().lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def interpolate_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` inside parsed YAML.

    Two behaviours are deliberate and load-bearing:

    * a value that is *exactly* a reference is expanded and then coerced, so
      ``port: ${ZKAI_PORT:-8317}`` lands as ``int`` and ``enabled: ${X:-true}``
      as ``bool`` - otherwise every number in the YAML would need quoting;
    * a reference with no environment variable and **no default expands to
      ``None``**, never to an empty string. ``None`` makes the field *absent*,
      which fails closed: a missing ``${ADMIN_TOKEN}`` disables the admin API
      rather than silently enabling it with a blank token.

    Note this runs on the *whole* document, so an ``env:`` key under a
    credential is expanded too. That key holds an environment variable **name**,
    not the secret - see :meth:`AppConfig.validate` for the guard that catches
    the reverse reading.
    """
    if isinstance(value, dict):
        return {key: interpolate_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate_env(item) for item in value]
    if not isinstance(value, str):
        return value

    full = _ENV_REF.fullmatch(value)
    if full:
        var_name, default = full.group(1), full.group(2)
        raw = os.environ.get(var_name)
        if raw is not None and raw != "":
            return _coerce(raw)
        return _coerce(default) if default is not None else None

    def _replace(match: re.Match[str]) -> str:
        var_name, default = match.group(1), match.group(2)
        raw = os.environ.get(var_name)
        if raw is not None and raw != "":
            return raw
        return default or ""

    return _ENV_REF.sub(_replace, value)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base* (override wins)."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class Settings(BaseSettings):
    """Process level settings (``ZKAI_*`` environment variables / ``.env``)."""

    model_config = SettingsConfigDict(
        env_prefix="ZKAI_",
        # Anchor to the project root: a relative ".env" breaks when the process
        # starts from any other directory (everything silently DISABLED).
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "ZK-AI"
    version: str = "0.1.0"
    environment: str = "development"

    host: str = "0.0.0.0"
    port: int = 8317
    root_path: str = ""

    log_level: str = "INFO"
    log_json: bool = False

    config_dir: Path = Path("config")
    data_dir: Path = Path("data")
    database_url: str | None = None
    db_echo: bool = False

    admin_enabled: bool = True
    admin_token: str | None = None
    #: Optional token protecting the inference surface (``/v1/*``). Unset = open
    #: (fine on loopback). With ``ZKAI_HOST=0.0.0.0`` this is what stops anyone
    #: on the LAN from burning your quota.
    api_token: str | None = None

    request_timeout: float = 120.0
    stream_idle_timeout: float = 120.0
    max_request_body_mb: float = 10.0
    #: 估算 input token 的硬上限（0 = 关闭）。超限直接返回可读错误，不发上游。
    #: 为什么要有它：``max_request_body_mb`` 是**字节**闸且只看 content-length
    #: 首部，10MB ≈ 290 万 est tokens，是现役最大上游窗口（1M）的 2.9 倍——放过去
    #: 只会收到上游 400/429；而 chunked 上传不带这个首部，字节闸整个失效。
    #: 这道闸按 token 算、在 ``RequestService.chat`` 里生效（三条对外路径的收敛点），
    #: 且报错文案说清「哪一段太大、怎么减」，比 413 有信息量。
    max_input_tokens: int = 0
    #: 单次客户端请求的墙钟预算（秒），0 = 关闭。
    #: 为什么不是只用次数上限：``max_total_attempts`` 数的是**次数**，20 次 ×
    #: 60s（nvidia read timeout）= 最坏 20 分钟。2026-09-28 实测一次 zk-auto
    #: 首条消息磨了十几分钟、客户端一个字节都没收到——每次尝试都「还没超」，
    #: 所以次数闸认为还有预算。默认 300s = 现役最大上游超时（商汤 300s）再留
    #: 余量，一次成功的慢生成不会被误杀，只有「反复失败累计」会被截断。
    max_request_seconds: float = 300.0
    #: 历史裁剪预算（估算 tokens），0 = 关闭。
    #: 背景：zk-auto / Codex 客户端每轮重发整段对话，turn 30 要为 turn 1..29
    #: 再付一遍，而实测 84% 的工具轮是 ack/diff/status 这类再也不会被引用的机械轮。
    #: 开启后 ``app/services/history.py`` 只删「旧」轮次：system 前言、最近若干轮、
    #: 以及未闭合的 tool_call 配对一律保留，被删部分用一行占位说明，绝不静默。
    #: **默认关闭**：删上下文可能改变答案（turn 4 读过的文件 turn 40 还要用），
    #: 要不要这个换血是产品决策，不该藏在默认值里。想省就先设成一个保守的大数
    #: （例如 120000），再看日志里的「历史裁剪」WARNING 与路由 reason 核对效果。
    trim_history_tokens: int = 0
    #: 是否把客户端传来的图片真正转发给上游（默认 true）。
    #: 背景：Codex 会把截图以 ``input_image`` 塞进 /v1/responses 的 input，
    #: 而网关以前**静默丢掉**它——上游收到的是空 tool 消息，等于撒谎「这个工具
    #: 没返回内容」。打开后图会转成 chat 侧的 ``image_url`` part 发上去，
    #: K3 / StepFun 这类有视觉能力的模型才真的看得见。
    #: 代价：图片 token 是真实花费（25 张 1440x950 量级的截图 ≈ 3.5 万 tokens），
    #: 且估算器已按像素计入 ``ZKAI_MAX_INPUT_TOKENS``。设为 false 则回落为
    #: 「留一行占位文本」，省流量但模型看不到图。
    forward_images: bool = True

    default_max_tokens: int = 1024

    health_check_mode: str = "startup"  # manual | startup | scheduled | off
    health_check_interval: float = 900.0
    health_check_on_startup: bool = True
    health_check_timeout: float = 20.0

    allow_inline_secrets: bool = True
    alias_reload_token: str | None = None

    #: Strip ``reasoning_content``/``reasoning`` from responses whenever a real
    #: answer exists. Reasoning models (Kimi K3 等) often think in English even
    #: when answering in Chinese - the thinking bubbles then dominate what the
    #: user sees. Truncated answers still surface the thinking (never a blank
    #: reply): the strip only applies when content is present.
    strip_reasoning: bool = False

    #: Fallback model id for the Anthropic ``/v1/messages`` surface. Clients
    #: such as Claude Code send ``claude-*`` model ids regardless of provider
    #: mapping; those resolve to this alias instead of 404-ing. Bare ``zk-*``
    #: ids pass through unchanged.
    anthropic_default_model: str = "zk-auto"

    # ---- ZK-Agent (batch coding-task runner, /ui/agent) ------------------- #
    #: Master switch for the ``/admin/agent/*`` surface and the ``/ui/agent`` page.
    agent_enabled: bool = True
    #: Root the agent may touch: every path the model passes is resolved and must
    #: stay inside this tree; ``run_command`` also executes with this cwd.
    agent_workspace: Path = Path(".")
    #: Alias (or model id) the agent loop routes through. ``zk-auto`` keeps the
    #: traffic off the flash-lite dedicated points pool by design.
    agent_default_model: str = "zk-auto"
    #: Hard cap of tool-loop steps per task (protects the points pools from a
    #: runaway loop even if the model keeps asking for tools).
    agent_max_steps: int = 40
    #: Sessions allowed to run their loop simultaneously (extra ones queue).
    agent_max_concurrent: int = 3
    #: Per-command timeout and output cap for ``run_command``.
    agent_command_timeout: float = 60.0
    #: Estimated input tokens above which the transcript gets summarised down.
    agent_context_token_limit: int = 120_000
    # ---- ZK-Agent 子任务派发（spawn_subagent）--------------------------- #
    #: 派发深度上限。根会话 depth=1，子会话=2；子会话的工具箱里根本没有
    #: spawn_subagent，所以这是硬边界而非软约定。
    agent_max_depth: int = 2
    #: 单个父会话同时在跑的子任务数上限（超出时把「并发已满」回给模型，不抛异常）。
    agent_max_live_children: int = 2
    #: 全局同时在跑的子任务数上限。子任务走**独立的**信号量，不占
    #: ``agent_max_concurrent``——否则父持槽等子、子又等着同一个槽，会死锁。
    #: 真实并发上界 = agent_max_concurrent + 本项。
    agent_max_children_total: int = 4
    #: 角色 -> alias 覆盖，JSON 字符串（坏 JSON 静默按默认表走）。
    #: 例：{"vision": "zk-vision", "coder": "zk-auto"}
    agent_role_aliases: str = ""

    @property
    def resolved_config_dir(self) -> Path:
        """Absolute config directory (relative paths resolve against the project root)."""
        path = Path(self.config_dir)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @property
    def resolved_data_dir(self) -> Path:
        path = Path(self.data_dir)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @property
    def resolved_workspace(self) -> Path:
        """Agent workspace anchored to the project root (like ``data_dir``)."""
        path = Path(self.agent_workspace)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    def parsed_role_aliases(self) -> dict[str, str]:
        """``agent_role_aliases`` 的 JSON 解析；坏 JSON / 非对象都回落空 dict。

        解析失败不抛异常：一个写错的环境变量不该让整个 agent 面起不来，
        此时按 :data:`app.services.agent.roles.DEFAULT_ROLE_ALIASES` 走。
        """
        raw = (self.agent_role_aliases or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("ZKAI_AGENT_ROLE_ALIASES 不是合法 JSON，已按默认角色表走")
            return {}
        if not isinstance(data, dict):
            logger.warning("ZKAI_AGENT_ROLE_ALIASES 不是 JSON 对象，已按默认角色表走")
            return {}
        return {str(k): str(v) for k, v in data.items() if str(k).strip() and str(v).strip()}

    @property
    def resolved_database_url(self) -> str:
        """SQLAlchemy async URL, defaulting to SQLite inside the data directory."""
        if self.database_url:
            url = self.database_url
            if url.startswith("sqlite:///"):
                url = url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
            if url.startswith("sqlite+aiosqlite:///"):
                # A relative path in the URL is resolved against the CWD by
                # SQLAlchemy — anchor it to the project root so the DB does not
                # silently move when the process starts elsewhere.
                raw = url[len("sqlite+aiosqlite:///"):]
                p = Path(raw)
                if not p.is_absolute():
                    p = (PROJECT_ROOT / p).resolve()
                return f"sqlite+aiosqlite:///{p.as_posix()}"
            return url
        db_path = self.resolved_data_dir / "zkai.db"
        return f"sqlite+aiosqlite:///{db_path.as_posix()}"


@dataclass
class AppConfig:
    """Fully merged runtime configuration."""

    settings: Settings
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    models: dict[str, ModelConfig] = field(default_factory=dict)
    aliases: dict[str, ModelAliasConfig] = field(default_factory=dict)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    raw: dict[str, Any] = field(default_factory=dict)
    source_files: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        return self.providers.get(provider_id)

    def get_model(self, model_id: str) -> ModelConfig | None:
        return self.models.get(model_id)

    def get_alias(self, name: str) -> ModelAliasConfig | None:
        return self.aliases.get(name)

    def is_alias(self, name: str) -> bool:
        return name in self.aliases

    def resolve_model_ids(self, name: str) -> list[str]:
        """Expand an alias (or a plain model id) into concrete model ids."""
        alias = self.aliases.get(name)
        if alias is None:
            return [name] if name in self.models else []
        targets: list[str] = []
        for target in alias.targets:
            nested = self.aliases.get(target)
            if nested is not None:
                targets.extend(self.resolve_model_ids(target))
            elif target in self.models:
                targets.append(target)
        # de-duplicate, keep order
        seen: dict[str, None] = {}
        for target in targets:
            seen.setdefault(target, None)
        return list(seen)

    def deployments_for_model(self, model_id: str) -> list[Any]:
        model = self.models.get(model_id)
        return list(model.deployments) if model else []

    def enabled_models(self) -> list[ModelConfig]:
        return [m for m in self.models.values() if m.enabled]

    def public_model_ids(self) -> list[str]:
        return sorted(self.models.keys())

    def enabled_aliases(self) -> list[ModelAliasConfig]:
        return [a for a in self.aliases.values() if a.enabled]

    def credentials_for_provider(self, provider_id: str) -> list[Any]:
        provider = self.providers.get(provider_id)
        return list(provider.credentials) if provider else []

    def describe(self) -> dict[str, Any]:
        """Summary used by ``/admin/health`` and startup logging."""
        return {
            "environment": self.settings.environment,
            "providers": {
                pid: {
                    "type": provider.type.value,
                    "enabled": provider.enabled,
                    "credentials": len(provider.credentials),
                }
                for pid, provider in self.providers.items()
            },
            "models": len(self.models),
            "aliases": sorted(self.aliases.keys()),
            "source_files": self.source_files,
            "warnings": self.warnings,
        }

    def validate(self) -> list[str]:
        """Cross-reference checks; returns a list of human readable problems."""
        problems: list[str] = []
        for provider in self.providers.values():
            problems.extend(self._validate_credentials(provider))
        for model in self.models.values():
            if not model.deployments:
                problems.append(f"model '{model.id}' has no deployments")
            for deployment in model.deployments:
                if deployment.provider_id not in self.providers:
                    problems.append(
                        f"model '{model.id}' deployment '{deployment.id}' references "
                        f"unknown provider '{deployment.provider_id}'"
                    )
        for alias in self.aliases.values():
            if not alias.targets:
                problems.append(f"alias '{alias.name}' has no targets")
            for target in alias.targets:
                if target not in self.models and target not in self.aliases:
                    problems.append(f"alias '{alias.name}' target '{target}' is unknown")
        return problems

    @staticmethod
    def _validate_credentials(provider: ProviderConfig) -> list[str]:
        """Catch the two ways a credential ends up with no usable secret.

        ``env``/``env_var`` hold the *name* of an environment variable, so a
        document such as ``env: ${OPENAI_KEY}`` is expanded by
        :func:`interpolate_env` into the secret **value** - which then names a
        variable that does not exist. The failure is nasty because it is not
        reported as a missing key: the credential stays enabled holding the
        literal string as its secret (every request 401s), and if the expanded
        value happens to be identifier-safe the pool logs it verbatim.
        """
        problems: list[str] = []
        for credential in provider.credentials:
            name = credential.env or credential.env_var
            if name is None:
                if credential.enabled and credential.value is None and provider.requires_credential:
                    problems.append(
                        f"credential '{credential.id}' has no secret source: "
                        "set env/env_var to an environment variable name, or value for development"
                    )
                continue
            if not _ENV_VAR_NAME.match(name):
                problems.append(
                    f"credential '{credential.id}' env '{name}' is not a valid environment "
                    "variable name - put the variable NAME there, e.g. env: MY_KEY, or "
                    "env: ${MY_KEY_ENV:-MY_KEY} to pick the name through indirection"
                )
        return problems

    def known_names(self) -> list[str]:
        return sorted({*self.models.keys(), *self.aliases.keys()})


# --------------------------------------------------------------------------- #
# YAML loading
# --------------------------------------------------------------------------- #
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


def load_dotenv_file(path: Path | str | None = None) -> bool:
    """Load a ``.env`` file into the process environment.

    ``pydantic-settings`` reads ``.env`` into the :class:`Settings` object, but
    provider *credentials* are resolved straight from ``os.environ`` by
    :func:`app.core.security.resolve_env_reference` - so without this call a key
    placed in ``.env`` would silently never be found.

    Real environment variables always win (``override=False``), and a missing
    file is not an error. Returns ``True`` when a file was actually loaded.

    Because "env wins" is a silent precedence rule, a stale variable exported at
    the OS level (e.g. a Windows user variable set long ago) shadows the .env
    value forever - two credentials then resolve to the same key and the operator
    has no idea. That happened in practice, so every shadowed name that also has
    a *different* value in .env is logged as a warning at startup and surfaced in
    ``AppConfig.warnings``.
    """
    if path is not None:
        # Explicit path: respect it exactly (tests pass fixtures here).
        target = Path(path)
        if not target.is_file():
            return False
    else:
        target = PROJECT_ROOT / ".env"
        if not target.is_file():
            # Unusual layout (e.g. Docker mounts): fall back to the CWD.
            target = Path(".env")
            if not target.is_file():
                return False
    malformed = malformed_env_names(target)
    if malformed:
        # Reported first and separately: unlike a genuine shadow, the fix is to
        # repair the file, and the symptom shows up far away (a bind failure
        # logged as a DNS error), so the operator needs the pointer.
        logger.error(
            "%d variable(s) in %s carry stray whitespace/control characters in "
            "their value (e.g. a line ending in \\r\\r): %s - processes reading "
            "them straight from the environment get a value like '0.0.0.0\\r', "
            "which fails deep in the stack (socket bind reports it as "
            "'getaddrinfo failed'). Repair the file: rewrite those lines with "
            "plain CRLF endings",
            len(malformed), target, ", ".join(sorted(malformed)[:20]),
        )
    shadowed = shadowed_env_names(target)
    if shadowed:
        logger.warning(
            "%d variable(s) in %s are shadowed by pre-existing process "
            "environment variables (env wins; the .env values are ignored): %s "
            "- if these are stale, unset them in the OS environment",
            len(shadowed), target, ", ".join(sorted(shadowed)[:20]),
        )
    loaded = load_dotenv(target, override=False)
    if loaded:
        logger.info("loaded environment variables from %s", target)
    return bool(loaded)


def shadowed_env_names(path: Path | str = ".env") -> list[str]:
    """``.env`` names whose file value is being ignored (process env already has
    a *different* value - with ``override=False`` the process value silently wins).

    A value that differs from the file only by *surrounding whitespace or control
    characters* is not a real conflict - it is a corrupted ``.env``. Those lines
    end up here because ``dotenv_values()`` strips them from the file value while
    ``os.environ`` keeps them, and the fallout is severe: a line written as
    ``ZKAI_HOST=0.0.0.0\\r\\r`` puts ``"0.0.0.0\\r"`` into the process (and, via
    the tray, into uvicorn's argv), where ``socket.bind()`` resolves the host
    through ``getaddrinfo`` and dies with ``[Errno 11001] getaddrinfo failed`` -
    a message that reads as a DNS outage. So the two cases are reported
    separately: :func:`malformed_env_names` names the fixable ones.
    """
    target = Path(path)
    if not target.is_file():
        return []
    try:
        file_values = dotenv_values(target)
    except Exception:  # pragma: no cover - malformed .env
        return []
    return [
        name
        for name, value in file_values.items()
        if value and name in os.environ and os.environ[name] != value
        and name not in malformed_env_names(target)
    ]


def malformed_env_names(path: Path | str = ".env") -> list[str]:
    """``.env`` names whose *process* value differs only by stripped characters.

    These are not shadowing conflicts to resolve by unsetting an OS variable -
    the file's own bytes carry the junk, so the fix is to repair the file.
    """
    target = Path(path)
    if not target.is_file():
        return []
    try:
        file_values = dotenv_values(target)
    except Exception:  # pragma: no cover - malformed .env
        return []
    names: list[str] = []
    for name, value in file_values.items():
        current = os.environ.get(name)
        if current and value and current != value and current.strip() == value:
            names.append(name)
    return names


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
