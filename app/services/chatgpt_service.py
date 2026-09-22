"""ChatGPT / Codex 桌面版客户端配置：读取、计划、写入、换机自动配置。

桌面版的配置在 ``~/.codex/config.toml``——这个文件归 ChatGPT app 自己所有
（mcp_servers / plugins / marketplaces / projects 信任级 / desktop 偏好都在里面），
ZK-AI 只需要其中四个顶层键加一张 provider 表::

    model                  = "zk-auto"
    model_provider         = "zkai"
    model_reasoning_effort = "max"

    [model_providers.zkai]
    name       = "ZK-AI"
    base_url   = "http://127.0.0.1:8317/v1"
    wire_api   = "responses"
    env_key    = "ZKAI_API_TOKEN"

所以写入必须是**文本级外科手术**：读用 stdlib tomllib，写只改这些行，文件里
其他每一个字节原样保留。``auth.json``（ChatGPT 登录态）**永远不写**——桌面版
从 ``env_key`` 指向的环境变量取 Key，换机流程会负责把该变量 provision 好
（见 ``scripts/migrate.py``）。

期望配置存在 ``config/chatgpt.yaml``（本地现役、gitignore、随迁移包加密同行），
新机器导入后即可自动生成客户端配置，"换机后上来就能用"。
**密钥不落 yaml**：令牌值永远取 ``ZKAI_API_TOKEN``（.env / 用户环境变量）。

本模块只依赖 stdlib + pyyaml，``scripts/migrate.py`` 也复用它（无 app 栈依赖）。
"""

from __future__ import annotations

import os
import re
import shutil
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #

#: 默认 Codex/桌面版配置目录；``ZKAI_CODEX_HOME`` 可覆盖（测试 / 多开用户）。
DEFAULT_PORT = 8317


def codex_home() -> Path:
    """``~/.codex``；环境变量 ``ZKAI_CODEX_HOME`` 优先。"""
    raw = (os.environ.get("ZKAI_CODEX_HOME") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def config_toml_path() -> Path:
    return codex_home() / "config.toml"


def auth_json_path() -> Path:
    return codex_home() / "auth.json"


# --------------------------------------------------------------------------- #
# 期望配置（config/chatgpt.yaml）
# --------------------------------------------------------------------------- #

#: 接入方式：zk-ai = 走本机网关；official = 切回 ChatGPT 官方模型。
MODES = ("zk-ai", "official")
WIRE_APIS = ("responses", "chat")
EFFORTS = ("", "minimal", "low", "medium", "high", "max")

_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(?::\d{1,5})?(?:/[^\s]*)?$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


@dataclass
class ChatGptConfig:
    """桌面版期望配置（config/chatgpt.yaml 的内存形态）。

    ``official_model`` 只在 ``mode="official"`` 时使用；``official`` 模式只改
    顶层 ``model`` 行、删掉 ``model_provider`` 行，provider 表保留不删——
    随时可以切回来，re-apply 也会把表补全。
    """

    mode: str = "zk-ai"
    model: str = "zk-auto"
    model_provider: str = "zkai"
    provider_display: str = "ZK-AI"
    base_url: str = f"http://127.0.0.1:{DEFAULT_PORT}/v1"
    wire_api: str = "responses"
    env_key: str = "ZKAI_API_TOKEN"
    model_reasoning_effort: str = "max"
    official_model: str = "gpt-5.6-terra"

    def effective_model(self) -> str:
        return self.official_model if self.mode == "official" else self.model


_YAML_HEADER = """\
# ---------------------------------------------------------------------------
# ChatGPT / Codex 桌面版期望配置（由 /ui 控制台「🤖 ChatGPT」面板维护）
#
# mode: zk-ai     走 ZK-AI 网关（本机 base_url，Key 用 env_key 指向的环境变量）
#       official  切回 ChatGPT 官方（删 model_provider 行，model 改官方模型名）
# 密钥不在这里：令牌值永远取 .env 的 ZKAI_API_TOKEN。
# 本文件随一键换机迁移包同行，导入后自动写入新机 ~/.codex/config.toml。
# ---------------------------------------------------------------------------

"""

_FIELD_ORDER = (
    "mode",
    "model",
    "model_provider",
    "provider_display",
    "base_url",
    "wire_api",
    "env_key",
    "model_reasoning_effort",
    "official_model",
)


def validate(cfg: ChatGptConfig) -> list[str]:
    """返回人类可读的错误列表（空 = 可以保存）。"""
    errors: list[str] = []
    if cfg.mode not in MODES:
        errors.append(f"mode 只能是 {MODES} 之一")
        return errors
    if cfg.mode == "official":
        if not cfg.official_model.strip():
            errors.append("official 模式下 official_model 不能为空（要写回 config.toml 的官方模型名）")
        return errors
    if not cfg.model.strip():
        errors.append("model 不能为空（别名 zk-auto 或具体模型名）")
    if not _PROVIDER_NAME_RE.match(cfg.model_provider or ""):
        errors.append("model_provider 只能是字母/数字/下划线/连字符（config.toml 表名）")
    if not cfg.base_url or not _URL_RE.match(cfg.base_url):
        errors.append("base_url 要以 http(s):// 开头，例如 http://127.0.0.1:8317/v1")
    if cfg.wire_api not in WIRE_APIS:
        errors.append(f"wire_api 只能是 {WIRE_APIS} 之一")
    if not _ENV_NAME_RE.match(cfg.env_key or ""):
        errors.append("env_key 要是合法的环境变量名，例如 ZKAI_API_TOKEN")
    if cfg.model_reasoning_effort not in EFFORTS:
        errors.append(f"思考强度只能是 {EFFORTS[1:]} 之一，或留空不写这一行")
    if not cfg.provider_display.strip():
        errors.append("provider_display（config.toml 里的显示名）不能为空")
    return errors


def render_desired(cfg: ChatGptConfig) -> str:
    """序列化为带注释头的 YAML 文本（固定字段序）。"""
    values = asdict(cfg)
    lines: list[str] = []
    for key in _FIELD_ORDER:
        value = values[key]
        lines.append(f"{key}: {value}\n")
    return _YAML_HEADER + "".join(lines)


def save_desired(path: Path, cfg: ChatGptConfig) -> None:
    """写 ``config/chatgpt.yaml``：原子写 + 单滚动 ``.bak``（与 config_writer 一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render_desired(cfg), encoding="utf-8")
    if path.exists():
        bak = path.with_name(path.name + ".bak")
        try:
            if bak.exists():
                bak.unlink()
            os.replace(path, bak)
        except OSError:
            pass  # 滚动备份尽力而为，不阻塞写入
    os.replace(tmp, path)


def load_desired(path: Path) -> ChatGptConfig | None:
    """读 ``config/chatgpt.yaml``；不存在返回 None（= 尚未保存过期望配置）。"""
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} 不是合法 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是映射")
    defaults = ChatGptConfig()
    kwargs = {
        key: str(data[key])
        for key in _FIELD_ORDER
        if key in data and data[key] is not None
    }
    cfg = ChatGptConfig(**{**asdict(defaults), **kwargs})
    return cfg


# --------------------------------------------------------------------------- #
# 磁盘现状（只读）
# --------------------------------------------------------------------------- #

@dataclass
class DiskState:
    """``~/.codex/config.toml`` 的现状（tomllib 解析）。"""

    exists: bool = False
    model: str = ""
    model_provider: str = ""
    base_url: str = ""
    wire_api: str = ""
    env_key: str = ""
    model_reasoning_effort: str = ""
    parse_error: str = ""


def read_config_toml(path: Path) -> dict[str, Any]:
    """解析 config.toml；文件不存在返回 {}。"""
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def read_disk_state(path: Path) -> DiskState:
    """当前**生效**的值：顶层 model/model_provider + 该 provider 表里的键。"""
    if not path.exists():
        return DiskState(exists=False)
    try:
        data = read_config_toml(path)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return DiskState(exists=True, parse_error=str(exc))
    provider_name = str(data.get("model_provider", "") or "")
    table = (data.get("model_providers") or {}).get(provider_name) or {}
    if not isinstance(table, dict):
        table = {}
    return DiskState(
        exists=True,
        model=str(data.get("model", "") or ""),
        model_provider=provider_name,
        base_url=str(table.get("base_url", "") or ""),
        wire_api=str(table.get("wire_api", "") or ""),
        env_key=str(table.get("env_key", "") or ""),
        model_reasoning_effort=str(data.get("model_reasoning_effort", "") or ""),
    )


def read_auth_info(home: Path | None = None) -> dict[str, Any]:
    """auth.json 只读信息（存在与否、有没有 key）——永远不写它。"""
    path = (home or codex_home()) / "auth.json"
    if not path.exists():
        return {"exists": False, "has_openai_key": False}
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"exists": True, "has_openai_key": False}
    has_key = isinstance(data, dict) and bool(str(data.get("OPENAI_API_KEY", "") or ""))
    return {"exists": True, "has_openai_key": has_key}


# --------------------------------------------------------------------------- #
# 变更计划
# --------------------------------------------------------------------------- #

@dataclass
class Change:
    """一个键层面的改动。``new=None`` 表示删除该行（official 模式删 model_provider）。"""

    scope: str  # "top" | "provider"
    key: str
    old: str | None
    new: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_changes(disk_data: dict[str, Any], desired: ChatGptConfig) -> list[Change]:
    """磁盘现状（tomllib 字典）→ 期望配置 的键级差异清单。"""
    changes: list[Change] = []

    def want(scope: str, key: str, new: str | None, old: str | None) -> None:
        if (new or "") == (old or ""):
            return
        changes.append(Change(scope=scope, key=key, old=old, new=new))

    want("top", "model", desired.effective_model(), str(disk_data.get("model", "") or "") or None)
    if desired.mode == "official":
        current = str(disk_data.get("model_provider", "") or "")
        want("top", "model_provider", None, current or None)
    else:
        table = (disk_data.get("model_providers") or {}).get(desired.model_provider) or {}
        if not isinstance(table, dict):
            table = {}
        want("top", "model_provider", desired.model_provider,
             str(disk_data.get("model_provider", "") or "") or None)
        want("provider", "name", desired.provider_display, str(table.get("name", "") or "") or None)
        want("provider", "base_url", desired.base_url, str(table.get("base_url", "") or "") or None)
        want("provider", "wire_api", desired.wire_api, str(table.get("wire_api", "") or "") or None)
        want("provider", "env_key", desired.env_key, str(table.get("env_key", "") or "") or None)
    want("top", "model_reasoning_effort", desired.model_reasoning_effort or None,
         str(disk_data.get("model_reasoning_effort", "") or "") or None)
    return changes


# --------------------------------------------------------------------------- #
# 写入（文本级外科手术）
# --------------------------------------------------------------------------- #

def _toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _kv_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(key)}\s*=")


def _section_start(lines: list[str]) -> int:
    """第一个 ``[table]`` 头的行号；没有则 len(lines)。"""
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            return index
    return len(lines)


def _set_kv(lines: list[str], start: int, end: int, key: str, value: str) -> None:
    """在 lines[start:end] 里设 ``key = "value"``：原地替换，否则插到本段最后
    一个非空行之后。只认本段内的键——顶层段在第一个 ``[`` 之前，不会误伤
    ``[table]`` 里同名的键。"""
    pattern = _kv_pattern(key)
    upper = min(end, len(lines))
    for index in range(start, upper):
        if pattern.match(lines[index]):
            lines[index] = f"{key} = {_toml_quote(value)}"
            return
    insert_at = start
    for index in range(start, upper):
        if lines[index].strip():
            insert_at = index + 1
    lines.insert(insert_at, f"{key} = {_toml_quote(value)}")


def _del_kv(lines: list[str], start: int, end: int, key: str) -> None:
    pattern = _kv_pattern(key)
    upper = min(end, len(lines))
    for index in range(start, upper):
        if pattern.match(lines[index]):
            del lines[index]
            return


def _find_provider_table(lines: list[str], provider: str) -> int | None:
    """``[model_providers.<provider>]`` 头行号（容忍带引号写法）。"""
    pattern = re.compile(
        rf'^\[\s*model_providers\s*\.\s*(?:{re.escape(provider)}|"{re.escape(provider)}")\s*\]\s*$'
    )
    for index, line in enumerate(lines):
        if pattern.match(line.strip()):
            return index
    return None


def _next_section(lines: list[str], start: int) -> int:
    for index in range(start + 1, len(lines)):
        if lines[index].lstrip().startswith("["):
            return index
    return len(lines)


def patch_text(text: str, changes: list[Change], provider: str) -> str:
    """把变更清单应用到 config.toml 文本上，返回新文本。

    保留原换行符风格（CRLF/LF）与全部其他字节；顶层键只可能在第一个
    ``[section]`` 之前增删改，provider 表只可能在其表体内增改——桌面版自己
    的段落一个字都不会动。
    """
    if not changes:
        return text
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    boundary = _section_start(lines)

    top_changes = [c for c in changes if c.scope == "top"]
    prov_changes = [c for c in changes if c.scope == "provider"]

    for change in top_changes:
        if change.new is None:
            _del_kv(lines, 0, boundary, change.key)
        else:
            _set_kv(lines, 0, boundary, change.key, change.new)

    if prov_changes:
        header = _find_provider_table(lines, provider)
        if header is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[model_providers.{provider}]")
            for change in prov_changes:
                if change.new is not None:
                    lines.append(f"{change.key} = {_toml_quote(change.new)}")
        else:
            body_end = _next_section(lines, header)
            for change in prov_changes:
                if change.new is not None:
                    _set_kv(lines, header + 1, body_end, change.key, change.new)

    out = eol.join(lines)
    if out and not out.endswith(eol):
        out += eol
    return out


@dataclass
class ApplyResult:
    path: Path
    changes: list[Change]
    backup: Path | None
    no_op: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "changes": [c.as_dict() for c in self.changes],
            "backup": str(self.backup) if self.backup else None,
            "no_op": self.no_op,
        }


def apply_config(config_file: Path, desired: ChatGptConfig) -> ApplyResult:
    """把期望配置写进 ``config.toml``（无变更则不动文件、不建备份）。"""
    data = read_config_toml(config_file)
    changes = plan_changes(data, desired)
    if not changes:
        return ApplyResult(path=config_file, changes=[], backup=None, no_op=True)
    # newline="" matters: read_text() would translate CRLF to LF and the app's
    # own line-ending style would be silently rewritten.
    text = ""
    if config_file.exists():
        with config_file.open("r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    new_text = patch_text(text, changes, desired.model_provider)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if config_file.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = config_file.with_name(f"{config_file.name}.bak-{stamp}")
        shutil.copy2(config_file, backup)  # 先拷贝后替换：写失败原文件还在
    tmp = config_file.with_name(config_file.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8", newline="")
    os.replace(tmp, config_file)
    return ApplyResult(path=config_file, changes=changes, backup=backup)


# --------------------------------------------------------------------------- #
# Windows 用户级环境变量（换机最后一环）
# --------------------------------------------------------------------------- #

def write_user_env_var(name: str, value: str) -> tuple[bool, str | None, str]:
    """把变量写进用户级环境（HKCU\\Environment）并广播 WM_SETTINGCHANGE。

    Returns ``(written, old_value, note)``：explorer 之后启动的进程都能读到，
    这正是从 explorer 双击打开的 ChatGPT 桌面版需要的。非 Windows 或空值
    时跳过（note 说明原因）。**不会**静默覆盖——旧值由调用方备份/提示还原。
    """
    if os.name != "nt":
        return False, None, "非 Windows 平台，跳过用户级环境变量写入"
    if not value:
        return False, None, f"{name} 为空，跳过"
    import winreg

    old: str | None = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            try:
                old = str(winreg.QueryValueEx(key, name)[0])
            except FileNotFoundError:
                old = None
    except OSError:
        old = None
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    _broadcast_setting_change()
    return True, old, ""


def _broadcast_setting_change() -> None:
    """让已运行的 explorer 立刻重读环境变量（新进程才生效的关键一步）。"""
    import contextlib
    import ctypes

    WM_SETTINGCHANGE = 0x1A
    SMTO_ABORTIFHUNG = 0x0002
    # 广播失败不影响注册表值本身；重登或重开 explorer 后同样生效
    with contextlib.suppress(OSError):
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000, None
        )


def env_restore_hint(name: str, old: str | None) -> str:
    """还原用户级环境变量的命令（打印给操作者，不自动执行）。"""
    if old is None:
        return f'reg delete "HKCU\\Environment" /v {name} /f'
    escaped = old.replace('"', "'")
    return f'setx {name} "{escaped}"'


__all__ = [
    "ApplyResult",
    "Change",
    "ChatGptConfig",
    "DiskState",
    "apply_config",
    "auth_json_path",
    "codex_home",
    "config_toml_path",
    "env_restore_hint",
    "load_desired",
    "patch_text",
    "plan_changes",
    "read_auth_info",
    "read_config_toml",
    "read_disk_state",
    "render_desired",
    "save_desired",
    "validate",
    "write_user_env_var",
]
