"""配置读写：config/burner.yaml 的解析、校验、行级写入。

拆自 burner_service.py。消耗器的配置走文本级行替换（保留注释与排版），
校验防止把字符串数字烧到一半才炸、防止把非 Flash-lite 模型写进去烧错池。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

#: 配置键 → 类型。与 scripts/burn_sensenova.py 的 _CONFIG_KEYS 保持一致；
#: 控制台改配置也走这里校验，避免把「安全系数」写成字符串烧到一半才炸。
CONFIG_KEYS: dict[str, type] = {
    "model": str,
    "base_url": str,
    "max_seconds": float,
    "once": bool,
    "concurrency": int,
    "per_account_max": int,
    "per_account_start": int,
    # 饥饿救济（2026-09-28 加，治 AIMD 饿死账号）。不加进白名单的话，
    # 写在 burner.yaml 里会被静默忽略——运营者以为调了，其实没生效。
    "starve_after": float,
    "starve_grace": float,
    # 限流停靠（2026-09-30 加，治「挤了也没用」的空转）。不加进白名单的话，
    # 控制台写进 burner.yaml 也会被静默忽略——和上面两条同样的坑。
    "rate_park_after": int,
    "rate_park_seconds": float,
    "max_tokens": int,
    "filler_chars": int,
    "window_credits": float,
    "weekly_credits": float,
    "safety_margin": float,
    "week_anchor": str,
    "week_anchors": str,
    "pool_total_credits": float,
    "rate_in": float,
    "rate_out": float,
    "quota_park_hours": float,
    "account_groups": str,
    "anchors": str,
    "cooldown_base": float,
    "cooldown_max": float,
    "connect_timeout": float,
    "read_timeout": float,
    "summary_interval": float,
    "only": str,
}

#: 控制台表单字段（顺序即展示顺序）。其余键仍有默认值但不进表单。
FORM_FIELDS: tuple[str, ...] = (
    "model",
    "concurrency",
    "per_account_start",
    "per_account_max",
    # 饥饿救济放在并发后面（它本来就是并发的补充调节），默认值即推荐值。
    "starve_after",
    "starve_grace",
    "max_tokens",
    "filler_chars",
    "rate_in",
    "rate_out",
    "safety_margin",
    "window_credits",
    "weekly_credits",
    "pool_total_credits",
    "quota_park_hours",
    "only",
    "anchors",
    "week_anchors",
    "week_anchor",
    "account_groups",
    "cooldown_base",
    "cooldown_max",
    "summary_interval",
    "max_seconds",
)

#: 只有 Flash-lite 家族扣「专属池」——烧错模型等于直接吃 kimi-k3 的通用池积分。
FLASH_LITE_REQUIRED = "flash-lite"


def is_flash_lite(model: str) -> bool:
    return FLASH_LITE_REQUIRED in model.lower()


def read_config(path: Path) -> dict[str, Any]:
    """读 config/burner.yaml → {dest: value}。文件不存在 → {}。"""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件顶层必须是「键: 值」")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        dest = str(key).replace("-", "_")
        if dest in CONFIG_KEYS:
            out[dest] = value
    return out


def _coerce(dest: str, value: Any) -> Any:
    """按声明的类型收敛前端传来的值（字符串数字 → 数字）。"""
    want = CONFIG_KEYS[dest]
    if want is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if isinstance(value, bool):
        raise ValueError(f"{dest} 不能是布尔值")
    if want is int:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{dest} 必须是整数")
        return int(value)
    if want is float:
        return float(value)
    return str(value)


def validate_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """校验控制台提交的配置片段 → 可写入的 {dest: value}。

    两处硬校验：未知键拒绝（防止把别的东西写进消耗器配置）、
    ``model`` 必须是 Flash-lite（防止烧错池、白吃 K3 口粮）。
    """
    out: dict[str, Any] = {}
    for key, value in patch.items():
        dest = str(key).replace("-", "_")
        if dest not in CONFIG_KEYS:
            raise ValueError(f"{key} 不是消耗器的已知参数")
        out[dest] = _coerce(dest, value)
    model = out.get("model")
    if model is not None and not is_flash_lite(model):
        raise ValueError(
            f"model 只能是 Flash-lite 家族（当前 {model!r}）：只有它扣专属池、"
            "能 1:1 折算回充成 kimi-k3 可用积分；其他模型直接扣通用池（K3 的口粮）"
        )
    margin = out.get("safety_margin")
    if margin is not None and not 0 < float(margin) <= 1:
        raise ValueError("safety_margin 必须在 (0, 1] 之间")
    return out


def _fmt(value: Any) -> str:
    """按 YAML 标量规则渲染。字符串一律加引号——锚点串里有 '=' 和 ';'，
    裸写有被某些解析器当成映射的风险。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_config(path: Path, patch: dict[str, Any]) -> list[str]:
    """把 patch 写进 config/burner.yaml（保留注释与排版），返回被改的键名。

    用文本级行替换而不是 yaml.dump：文件里每个键上方都有「这个值怎么算出来的」
    注释，重排会把注释和键挤散。键已存在就换值，不存在就追加到文件尾。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    changed: list[str] = []
    for dest, value in patch.items():
        pattern = re.compile(rf"^(\s*){re.escape(dest).replace('_', '[_-]')}(\s*):")
        hit = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
        rendered = f"{dest}: {_fmt(value)}\n"
        if hit is not None:
            lines[hit] = rendered
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(rendered)
        changed.append(dest)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(path)
    return changed


__all__ = [
    "CONFIG_KEYS",
    "FLASH_LITE_REQUIRED",
    "FORM_FIELDS",
    "is_flash_lite",
    "read_config",
    "validate_patch",
    "write_config",
]
