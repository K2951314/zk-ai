"""Settings: process-level configuration from ``ZKAI_*`` env vars and ``.env``.

Also hosts the shared YAML-interpolation helpers (``interpolate_env``,
``deep_merge``) and the project-root anchor, since ``Settings`` itself depends
on them and every other config sub-module needs ``PROJECT_ROOT``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.logging import get_logger

logger = get_logger("config")

PROJECT_ROOT = Path(__file__).resolve().parents[3]

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
    #: 公网入口（客户端真正用来调用的那个地址）。留空 = 本机部署，此时
    #: 控制台显示的 base_url 回落 http://<host>:<port>。
    #:
    #: **为什么不能由 host:port 推导**：跳反代部署时代理路径带前缀
    #: （Caddy 的 /zkai/v1），而网关只听得到自己的 loopback 地址，前缀对它完全不可见。
    #: 写错不会报错，只会让运营者把 http://<ip>:8318/v1 这种打不开的地址抄给客户端
    #: —— 所以必须显式配置。
    public_base_url: str = ""
    #: 部署形态标签，仅用于控制台展示与文案分流（local / server）。
    #: 留空时由 is_remote_deploy 自动判定。
    exposure: str = ""

    log_level: str = "INFO"
    log_json: bool = False

    config_dir: Path = Path("config")
    data_dir: Path = Path("data")
    #: 消耗器账本与明细日志的位置。默认与网关数据目录同层（本机部署、
    #: 托盘启动时 burn_sensenova.py 就写在那里）。
    #:
    #: **服务器上必须显式配**：zkai-burner.service 的 ExecStart 用
    #: --state-file/--log-file 把两者指到 /var/lib/zkai/burner/，比网关的
    #: ZKAI_DATA_DIR 深一层。不配的话控制台的「账号核对」会去读写一个不存在
    #: 的文件——运营者以为校准生效了，真正的 burner 却什么都没看到，比没有
    #: 这个功能更糟。
    burner_dir: Path | None = None
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

    #: 监听 config/*.yaml，改动后自动 reload（不用再手动 reload、也不用重启）。
    #: 为什么值得默认打开：网关只在启动时读一次配置，内存与磁盘会长期背离；
    #: 更糟的是控制台表单用**内存值**渲染，一保存就把运营者刚才的文件编辑
    #: 覆盖回去（2026-09-28/29 一天内吃掉两次改动，细节见 AGENTS.md）。
    #: 自动 reload 去掉的是根因——内存和磁盘不再有可能不一致；配上
    #: `config_writer` 的陈旧写闸，控制台会拒绝覆盖、而编辑自己就能生效。
    #: 代价：多几个 stat 调用；改坏了 YAML 不会让网关倒下（保留上一份配置并报错，
    #: 改好后下一次改动自动接上）。设 false 回到「必须手动 reload」。
    watch_config: bool = True
    #: 轮询间隔（秒）。纯标准库 stat 轮询，不引 watchdog/inotify：
    #: 为一个 2 秒的 stat 加依赖不划算，而且轮询在网络盘/容器里更可靠。
    watch_config_interval: float = 2.0

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
    def resolved_burner_dir(self) -> Path:
        """消耗器的账本 + 日志目录（ZKAI_BURNER_DIR 覆盖，测试用 tmp_path 隔离）。"""
        if self.burner_dir is not None:
            path = Path(self.burner_dir)
            return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
        return self.resolved_data_dir

    @property
    def burner_state_path(self) -> Path:
        return self.resolved_burner_dir / "burn_state.json"

    @property
    def burner_log_path(self) -> Path:
        return self.resolved_burner_dir / "burn_sensenova.log"

    # ------------------------------------------------------------------ #
    # 部署形态（公网入口 / 本机 vs 服务器）
    # ------------------------------------------------------------------ #
    @property
    def public_base_url_effective(self) -> str:
        """客户端该用的公网入口；未配置时回落 http://<host>:<port>。

        三处消费者共用同一个值（/health 的 deploy 块、控制台接入面板、
        chatgpt_service 的 base_url 校验），所以必须是单一事实来源。
        """
        raw = (self.public_base_url or "").strip().rstrip("/")
        if raw:
            return raw
        return f"http://{self.host}:{self.port}"

    @property
    def client_base_url(self) -> str:
        """OpenAI 兼容的 /v1 地址（连接口带不带 /v1 都在这里统一）。"""
        base = self.public_base_url_effective
        return base if base.endswith("/v1") else base + "/v1"

    @property
    def is_remote_deploy(self) -> bool:
        """是否「部署在服务器上」：听了非回环地址，或显式配了公网入口。

        判据刻意宽松：配了公网入口即算远程（反代后面 bind 回环也常见），
        这样控制台才会走「公网调用」那条路，而不是让运营者抄一个
        只在服务器本机能用的 127.0.0.1。
        """
        if (self.exposure or "").strip().lower() in {"server", "remote", "public"}:
            return True
        if (self.exposure or "").strip().lower() == "local":
            return False
        if (self.public_base_url or "").strip():
            return True
        return self.host.strip() not in {"127.0.0.1", "localhost", "::1", ""}

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
