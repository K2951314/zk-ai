"""商汤积分消耗器（scripts/burn_sensenova.py）的控制台读写层。

消耗器是独立进程、不经网关，所以控制台原本看不到它。本模块把两件事接上：

1. **读运行状态**：从 ``data/burn_state.json``（消耗器自己的账本）算出每账号的
   5h/周窗口已烧、熔断线、下一个边界、剩余可烧条数、AIMD 并发目标——也就是
   「什么时候该停、什么时候该启动」的那张表。
2. **读写配置**：``config/burner.yaml``（gitignore，模板 burner.example.yaml 可提交）。
   写入走文本级行替换，保留文件里的注释与排版——操作员靠这些注释记住锚点是怎么
   算出来的，格式化重排会把注释和键挤散。

账本可能正被消耗器写，故读取容忍半行：解析失败时返回空状态而不是报错
（控制台显示「暂无数据」比 500 有用）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

WIN_5H = 5 * 3600
WIN_WEEK = 7 * 86400

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


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 重启请求（消耗器只在自己启动时读配置，改完必须重启才生效）
#
# burner 由托盘 spawn，网关进程碰不到它。所以这里只负责「留信」：写一个
# data/burner_restart.request，托盘心跳（3s 一次）看到就重启 burner 再删信。
# 信箱本身不代表重启已发生——调用方只能承诺「已请求」，真正的重启由托盘完成。
# --------------------------------------------------------------------------- #
def request_restart(data_dir: Path) -> Path:
    path = data_dir / "burner_restart.request"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
    return path


def restart_pending(data_dir: Path) -> bool:
    return (data_dir / "burner_restart.request").exists()


# --------------------------------------------------------------------------- #
# 运行状态（读消耗器账本）
# --------------------------------------------------------------------------- #
@dataclass
class AccountStatus:
    name: str
    events: list[tuple[float, float]] = field(default_factory=list)
    credits_total: float = 0.0
    target: float = 0.0
    anchor_ts: float = 0.0
    week_anchor_ts: float = 0.0
    parked_until: float = 0.0
    ok: int = 0
    fail: int = 0
    rate_limited: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class BurnerStatus:
    saved_at: str = ""
    accounts: list[AccountStatus] = field(default_factory=list)
    rate_in: float = 0.0
    rate_out: float = 0.0
    safety_margin: float = 0.45
    window_credits: float = 60000.0
    weekly_credits: float = 600000.0
    pool_total_credits: float = 0.0


def read_status(state_file: Path, config: dict[str, Any]) -> BurnerStatus:
    """读 data/burn_state.json。账本由消耗器每分钟落盘，读到半行的概率不为 0：
    解析失败一律当成「暂无数据」，控制台显示空表，不报 500。"""
    status = BurnerStatus(
        rate_in=float(config.get("rate_in") or 0.0),
        rate_out=float(config.get("rate_out") or 0.0),
        safety_margin=float(config.get("safety_margin") or 0.45),
        window_credits=float(config.get("window_credits") or 60000.0),
        weekly_credits=float(config.get("weekly_credits") or 600000.0),
        pool_total_credits=float(config.get("pool_total_credits") or 0.0),
    )
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return status
    if not isinstance(data, dict):
        return status
    status.saved_at = str(data.get("saved_at") or "")
    # 费率以账本为准：控制台改了配置但消耗器没重启时，账本才是它实际用的值
    if data.get("rate_in"):
        status.rate_in = float(data["rate_in"])
    if data.get("rate_out"):
        status.rate_out = float(data["rate_out"])
    if data.get("safety_margin"):
        status.safety_margin = float(data["safety_margin"])
    keys = data.get("keys") or {}
    for name, blob in (data.get("accounts") or {}).items():
        if not isinstance(blob, dict):
            continue
        k = keys.get(name) or {}
        events = [(float(ts), float(c)) for ts, c in blob.get("events") or []]
        status.accounts.append(AccountStatus(
            name=name,
            events=events,
            credits_total=float(blob.get("credits_total") or 0.0),
            target=float(blob.get("target") or 0.0),
            anchor_ts=float(blob.get("anchor_ts") or 0.0),
            week_anchor_ts=float(blob.get("week_anchor_ts") or 0.0),
            parked_until=float(blob.get("parked_until") or 0.0),
            ok=int(k.get("ok") or 0),
            fail=int(k.get("fail") or 0),
            rate_limited=int(k.get("rate_limited") or 0),
            tokens_in=int(k.get("tokens_in") or 0),
            tokens_out=int(k.get("tokens_out") or 0),
        ))
    return status


def _week_start(anchor_ts: float, now: float) -> float:
    if not anchor_ts:
        return 0.0
    return anchor_ts + int((now - anchor_ts) // WIN_WEEK) * WIN_WEEK


def account_view(acct: AccountStatus, now: float, global_week_anchor: float,
                 status: BurnerStatus, request_cost: float) -> dict[str, Any]:
    """单账号的窗口账：已烧 / 熔断线 / 余量 / 下一边界 / 剩余可烧条数。

    与消耗器的 ``AccountState.available`` 同一套算法（固定窗口按账号锚点优先，
    回落全局），这样控制台显示的数就是消耗器做决策的数。
    """
    cap5h = status.window_credits * status.safety_margin
    capweek = status.weekly_credits * status.safety_margin
    ws5 = (acct.anchor_ts + int((now - acct.anchor_ts) // WIN_5H) * WIN_5H) if acct.anchor_ts else 0.0
    burned_5h = sum(c for ts, c in acct.events if ts >= ws5) if ws5 else \
        sum(c for ts, c in acct.events if ts > now - WIN_5H)
    wsw = _week_start(acct.week_anchor_ts or global_week_anchor, now)
    burned_week = sum(c for ts, c in acct.events if ts >= wsw) if wsw else \
        sum(c for ts, c in acct.events if ts > now - WIN_WEEK)
    left5 = max(0.0, cap5h - burned_5h)
    leftw = max(0.0, capweek - burned_week)
    next5 = (ws5 + WIN_5H) if ws5 else 0.0
    nextw = (wsw + WIN_WEEK) if wsw else 0.0
    capped = bool(status.pool_total_credits) and acct.credits_total >= status.pool_total_credits
    return {
        "name": acct.name,
        "credits_total": round(acct.credits_total, 1),
        "burned_5h": round(burned_5h, 1),
        "burned_week": round(burned_week, 1),
        "left_5h": round(left5, 1),
        "left_week": round(leftw, 1),
        "cap_5h": round(cap5h, 1),
        "cap_week": round(capweek, 1),
        "next_5h_boundary": next5,
        "next_week_boundary": nextw,
        "anchored_5h": bool(acct.anchor_ts),
        "anchored_week": bool(acct.week_anchor_ts),
        "parked_until": acct.parked_until,
        "parked": bool(acct.parked_until and now < acct.parked_until),
        "absolute_capped": capped,
        "target": acct.target,
        "ok": acct.ok,
        "fail": acct.fail,
        "rate_limited": acct.rate_limited,
        "tokens_in": acct.tokens_in,
        "tokens_out": acct.tokens_out,
        "requests_left_5h": int(left5 / request_cost) if request_cost > 0 else 0,
    }


def estimate_request_cost(config: dict[str, Any]) -> float:
    """单条请求预估积分（与消耗器 request_cost 同口径：填充 + max_tokens）。"""
    rate_in = float(config.get("rate_in") or 0.0)
    rate_out = float(config.get("rate_out") or 0.0)
    filler = float(config.get("filler_chars") or 0.0)
    max_tokens = float(config.get("max_tokens") or 0.0)
    prompt_tokens = filler / 3.5
    return rate_in * prompt_tokens / 1e6 + rate_out * max_tokens / 1e6


def snapshot(state_file: Path, config_file: Path) -> dict[str, Any]:
    """控制台「消耗器」页需要的一切：配置 + 每账号窗口账 + 派生提示。"""
    config = read_config(config_file)
    status = read_status(state_file, config)
    now = time.time()
    cost = estimate_request_cost(config)
    global_week_anchor = _parse_global_week_anchor(str(config.get("week_anchor") or "Mon 00:00"), now)
    accounts = [account_view(a, now, global_week_anchor, status, cost) for a in status.accounts]
    accounts.sort(key=lambda a: a["name"])
    return {
        "config": {k: config.get(k) for k in FORM_FIELDS},
        "form_fields": list(FORM_FIELDS),
        # 只给类型名（int/float/str/bool）：把 Python 的 type 对象直接塞进响应
        # 会让 pydantic 序列化失败，整个 /admin/burner 500。
        "config_keys": {k: t.__name__ for k, t in CONFIG_KEYS.items()},
        "ledger": {
            "saved_at": status.saved_at,
            "rate_in": status.rate_in,
            "rate_out": status.rate_out,
            "safety_margin": status.safety_margin,
            "window_credits": status.window_credits,
            "weekly_credits": status.weekly_credits,
            "pool_total_credits": status.pool_total_credits,
        },
        "request_cost": round(cost, 2),
        "accounts": accounts,
        "flash_lite_ok": is_flash_lite(str(config.get("model") or "")),
        "restart_pending": restart_pending(state_file.parent),
        "warnings": _warnings(config, status, accounts),
    }


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_global_week_anchor(spec: str, now: float) -> float:
    m = re.fullmatch(r"([A-Za-z]{3})\s*(\d{1,2}):(\d{2})", spec.strip())
    if not m or m.group(1).lower() not in _WEEKDAYS:
        return 0.0
    wd = _WEEKDAYS.index(m.group(1).lower())
    lt = time.localtime(now)
    days_back = (lt.tm_wday - wd) % 7
    try:
        ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - days_back,
                          int(m.group(2)), int(m.group(3)), 0, 0, 0, -1))
    except (OverflowError, ValueError):
        return 0.0
    return ts - WIN_WEEK if ts > now else ts


def _warnings(config: dict[str, Any], status: BurnerStatus,
              accounts: list[dict[str, Any]]) -> list[str]:
    """控制台首页「需要处理」要显示的那些事。"""
    out: list[str] = []
    if not is_flash_lite(str(config.get("model") or "")):
        out.append("model 不是 Flash-lite：会直接扣 kimi-k3 的通用池积分（纯亏）")
    margin = float(config.get("safety_margin") or 0)
    if margin > 0.6:
        out.append(f"安全系数 {margin} 偏高：真实费率若在实测区间上沿，专属池仍可能被烧穿")
    if not accounts:
        out.append("账本里还没有任何账号：消耗器可能从未运行，或正在首次启动")
    unanchored = [a["name"] for a in accounts if not a["anchored_5h"]]
    if unanchored:
        out.append("这些账号没配 5h 锚点，按滚动窗口记账（保守、会少烧）："
                   + ", ".join(unanchored))
    capped = [a["name"] for a in accounts if a["absolute_capped"]]
    if capped:
        out.append("这些账号已达绝对上限、永久停靠：" + ", ".join(capped))
    return out
