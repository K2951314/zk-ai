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
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from scripts.burn_sensenova import DEFAULT_SAFETY_MARGIN

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
    quota_hits: int = 0
    starve_boost: int = 0
    last_ok: float = 0.0
    #: 连续撞 429 的次数 / 限流停靠截止时刻（2026-09-30 的 rate-park）。
    #: 运营者必须能分清「积分烧完了」（parked）和「挤不进 tpm/rpm 桶」
    #: （rate_park）——两者处置相反，被混成一个数字会当成号坏了。
    rate_streak: int = 0
    rate_park_until: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class BurnerStatus:
    saved_at: str = ""
    accounts: list[AccountStatus] = field(default_factory=list)
    rate_in: float = 0.0
    rate_out: float = 0.0
    # 没有本地兜底值：直接 import burn_sensenova 的唯一常量。这里曾写死 0.45，
    # 于是控制台按 27 万画熔断线、消耗器按 54 万跑，同一块屏幕两个数。
    safety_margin: float = DEFAULT_SAFETY_MARGIN
    window_credits: float = 60000.0
    weekly_credits: float = 600000.0
    pool_total_credits: float = 0.0


def read_status(state_file: Path, config: dict[str, Any]) -> BurnerStatus:
    """读 data/burn_state.json。账本由消耗器每分钟落盘，读到半行的概率不为 0：
    解析失败一律当成「暂无数据」，控制台显示空表，不报 500。"""
    status = BurnerStatus(
        rate_in=float(config.get("rate_in") or 0.0),
        rate_out=float(config.get("rate_out") or 0.0),
        safety_margin=float(config.get("safety_margin") or DEFAULT_SAFETY_MARGIN),
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
            # 其中权益用尽的次数；旧账本没有这个键，回落 0（不做猜测）
            quota_hits=int(k.get("quota_hits") or 0),
            starve_boost=int(blob.get("starve_boost") or 0),
            rate_streak=int(blob.get("rate_streak") or 0),
            rate_park_until=float(blob.get("rate_park_until") or 0.0),
            last_ok=float(blob.get("last_ok") or 0.0),
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
        # 429 的两种含义分开给前端：限流（tpm exhausted，等窗口滑过）与
        # 额度用尽（entitlement exhausted，停靠到周刷新）。前者是争抢、
        # 后者是真用完，处置和预期都相反——混成一个数字会让人以为号坏了。
        "quota_hits": acct.quota_hits,
        "freq_hits": max(0, acct.rate_limited - acct.quota_hits),
        # 饥饿救济是否正在借出额度（账本里的 starve_boost > 0）。
        # 运营者看到「已救济」就知道这不是号坏了，是网关在帮它抢配额。
        # 限流停靠（2026-09-30）：账号连续撞 limit 后主动停手一段时间，
        # 到期自动重试。前端据此显示「限流停靠」，别和「积分耗尽」混淆。
        "rate_parked": bool(acct.rate_park_until and now < acct.rate_park_until),
        "rate_park_until": acct.rate_park_until,
        "rate_streak": acct.rate_streak,
        "starved": bool(getattr(acct, "starve_boost", 0)),
        "last_ok": getattr(acct, "last_ok", 0.0),
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

# --------------------------------------------------------------------------- #
# 账号核对与校准：把运营者从商汤后台看回来的真实消耗写回账本
#
# 为什么需要它：API 给不出池级信号（AGENTS.md 固化的结论），账本的 burned_5h /
# burned_week 全是按费率估算的。运营者有空时登陆商汤控制台看到真实用量，回来
# 需要一个入口把「真实」写回去——否则窗口估算漂移之后，要么提前停靠（少烧），
# 要么把专属池烧穿（静默溢出扣 K3 的通用池，不可逆）。
#
# 两条硬约束：
# 1. **窗口口径必须连续**：写回 burned 时不重建 events，而是注入一个校准事件
#    把账本估算拉到运营者给的数上，events 的时间戳序列仍单调——否则重启后
#    burner 按「最近 5h 的 events」重算，新旧口径在边界上跳变。
# 2. **账本不许反过来覆盖显式配置**：费率优先级（命令行 > burner.yaml >
#    账本）已由 burn_sensenova 的 _rate_fields 守着；这里写入只碰 burner.yaml
#    里没有的键，yaml 有的以 yaml 为准。
# --------------------------------------------------------------------------- #

#: 运营者能改、且改完必须重启才会生效的 burner.yaml 键。
RECONCILE_RESTART_KEYS = ("anchors", "week_anchors", "only", "account_groups")

#: 单个账号可核对的数字字段（单位 = 积分）。
_ACCOUNT_NUMERIC = ("credits_total", "burned_5h", "burned_week")

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _fmt_hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else ""


def _fmt_weekday_hhmm(ts: float) -> str:
    """epoch -> 周锚点表单的显示值。默认给 MM-DD HH:MM（2026-10-07 改）：
    运营者看到的是商汤控制台「下次重置时间」那一列，照抄即可；
    星期几得自己推，是「看不懂怎么填」的直接原因。"""
    if not ts:
        return ""
    lt = time.localtime(ts)
    return f"{lt.tm_mon:02d}-{lt.tm_mday:02d} {lt.tm_hour:02d}:{lt.tm_min:02d}"


def _parse_hhmm(spec: str) -> float:
    """'HH:MM' -> 今天该时刻的 epoch；非法抛 ValueError。"""
    text = (spec or "").strip()
    if not text:
        return 0.0
    try:
        hh, mm = (int(x) for x in text.split(":"))
    except ValueError as exc:
        raise ValueError(f"时间 {spec!r} 要是 HH:MM") from exc
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"时间 {spec!r} 超出范围")
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))


def _parse_weekday_hhmm(spec: str) -> float:
    """'Wed 18:10' -> 最近一个该时刻的 epoch（与 burn_sensenova 同规则）。"""
    from scripts.burn_sensenova import parse_week_anchor

    text = (spec or "").strip()
    if not text:
        return 0.0
    try:
        return parse_week_anchor(text)
    except ValueError as exc:
        raise ValueError(
            f"周锚点 {spec!r} 要是 '10-09 18:10'（月-日 时刻）或 'Wed 18:10'（星期几 时刻）"
        ) from exc


def _key_index(name: str) -> int | None:
    """'SENSENOVA_API_KEY_02' -> 2；不带后缀的 -> 1；不认识 -> None。"""
    if name == "SENSENOVA_API_KEY":
        return 1
    if name.startswith("SENSENOVA_API_KEY_"):
        suffix = name.rsplit("_", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
    return None


def _account_key_name(index: int) -> str:
    """账号序号 -> burner.yaml 的 only / anchors 里写的 Key 名。"""
    return "SENSENOVA_API_KEY" if index == 1 else f"SENSENOVA_API_KEY_{index:02d}"


def account_anchor_ts(config: dict[str, Any], name: str, now: float) -> float:
    """这个账号真正生效的 5h 锚点（按账号 anchors 优先，落回全局 anchor ）。
    为什么不直接给布尔：表单要显示管理员编辑的那个锚点值。"""
    per_account = anchors_to_accounts(str(config.get("anchors") or ""))
    spec = (per_account.get(name) or "").strip()
    if not spec:
        spec = str(config.get("anchor") or "").strip()
    if not spec:
        return 0.0
    try:
        return _parse_hhmm(spec)
    except ValueError:
        return 0.0


def account_week_anchor_ts(config: dict[str, Any], name: str, now: float) -> float:
    """这个账号真正生效的 周锚点（week_anchors 优先，落回全局 week_anchor ）。
    为什么不直接给布尔：表单要显示管理员编辑的那个锚点值。"""
    per_account = week_anchors_to_accounts(str(config.get("week_anchors") or ""))
    spec = (per_account.get(name) or "").strip()
    if not spec:
        spec = str(config.get("week_anchor") or "").strip()
    if not spec:
        return 0.0
    try:
        return _parse_weekday_hhmm(spec)
    except ValueError:
        return 0.0


def anchors_to_accounts(spec: str) -> dict[str, str]:
    """'2=15:10;3=15:10' -> {Key 名: 'HH:MM'}；坏项跳过。"""
    out: dict[str, str] = {}
    for item in filter(None, (s.strip() for s in (spec or "").split(";"))):
        num_s, sep, hm = item.partition("=")
        if not sep or not num_s.strip().isdigit():
            continue
        out[_account_key_name(int(num_s.strip()))] = hm.strip()
    return out


def week_anchors_to_accounts(spec: str) -> dict[str, str]:
    """'2=Wed 18:10' -> {Key 名: 'Wed 18:10'}。"""
    out: dict[str, str] = {}
    for item in filter(None, (s.strip() for s in (spec or "").split(";"))):
        num_s, sep, when = item.partition("=")
        if not sep or not num_s.strip().isdigit():
            continue
        out[_account_key_name(int(num_s.strip()))] = when.strip()
    return out


def accounts_to_anchors(mapping: dict[str, str]) -> str:
    """反过来序列化成 burner.yaml 的字符串，按账号序号排序保证可 diff。"""
    parts: list[tuple[int, str]] = []
    for name, hm in mapping.items():
        idx = _key_index(name)
        if idx is None or not (hm or "").strip():
            continue
        parts.append((idx, f"{idx}={hm.strip()}"))
    return ";".join(text for _, text in sorted(parts))


def only_to_set(spec: str) -> list[str]:
    """'K_02,K_03' -> ['SENSENOVA_API_KEY_02', ...]（容忍空格/分号）。"""
    return [raw for raw in re.split(r"[,;\s]+", (spec or "").strip()) if raw]


def set_to_only(names: list[str]) -> str:
    return ",".join(str(n) for n in names)


# --------------------------------------------------------------------------- #
# 核对：账本现状 vs 运营者填的真实值（只读）
# --------------------------------------------------------------------------- #
def reconcile_diff(
    state_file: Path, config: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """逐账号比对，返回 {accounts, config_changes, restart_keys, current}。

    只读不写盘——前端拿它做「保存前确认」弹窗的内容。
    """
    status = read_status(state_file, config)
    now = time.time()
    cost = estimate_request_cost(config)
    global_week_anchor = _parse_global_week_anchor(
        str(config.get("week_anchor") or "Mon 00:00"), now
    )
    by_name = {a.name: a for a in status.accounts}
    rows: list[dict[str, Any]] = []
    for item in payload.get("accounts") or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        acct = by_name.get(name)
        if acct is None:
            raise ValueError(
                f"账本里没有账号 {name!r}；可用：" + ", ".join(sorted(by_name))
            )
        view = account_view(acct, now, global_week_anchor, status, cost)
        row: dict[str, Any] = {"name": name}
        for field_name in _ACCOUNT_NUMERIC:
            current = float(view.get(field_name) or 0.0)
            raw = item.get(field_name)
            if raw is None or str(raw).strip() == "":
                row[field_name] = {
                    "current": round(current, 1), "filled": None, "delta": None
                }
                continue
            try:
                filled = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{name} 的 {field_name} 不是数字：{raw!r}"
                ) from exc
            delta = filled - current
            row[field_name] = {
                "current": round(current, 1),
                "filled": round(filled, 1),
                "delta": round(delta, 1),
                # 绝对差 > 2000 积分且相对差 > 5% 才提示，避免四舍五入噪音刷屏
                "notable": abs(delta) > 2000
                and (current <= 0 or abs(delta) / current > 0.05),
            }
        row["anchor"] = {
            "current": _fmt_hhmm(acct.anchor_ts),
            "filled": str(item.get("anchor") or "").strip(),
        }
        row["week_anchor"] = {
            "current": _fmt_weekday_hhmm(acct.week_anchor_ts),
            "filled": str(item.get("week_anchor") or "").strip(),
        }
        row["parked"] = bool(acct.parked_until and now < acct.parked_until)
        row["burning"] = bool(item.get("burning", True))
        rows.append(row)

    config_changes: dict[str, Any] = {}
    only_names = payload.get("only")
    if only_names is not None:
        config_changes["only"] = set_to_only([str(n) for n in only_names])
    for field_name, key in (
        ("anchors", "anchors"),
        ("week_anchors", "week_anchors"),
    ):
        mapping = payload.get(field_name)
        if mapping:
            config_changes[key] = accounts_to_anchors(
                {str(k): str(v) for k, v in mapping.items()}
            )
    for field_name in ("rate_in", "rate_out"):
        if payload.get(field_name) not in (None, ""):
            try:
                config_changes[field_name] = float(payload[field_name])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{field_name} 不是数字：{payload[field_name]!r}"
                ) from exc

    restart_keys = sorted(k for k in config_changes if k in RECONCILE_RESTART_KEYS)
    return {
        "accounts": rows,
        "config_changes": config_changes,
        "restart_keys": restart_keys,
        "current": {
            "only": str(config.get("only") or ""),
            "anchors": str(config.get("anchors") or ""),
            "week_anchors": str(config.get("week_anchors") or ""),
            "rate_in": status.rate_in,
            "rate_out": status.rate_out,
        },
    }


def _atomic_write_json(path: Path, data: Any) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".reconcile.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    tmp.replace(path)


def apply_reconcile(
    state_file: Path, config_file: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    """把核对结果写进 burn_state.json + burner.yaml，返回改了什么的摘要。

    校验在动任何一个字节之前全部跑完（reconcile_diff 会抛 ValueError），
    所以「保存失败但写了一半」不会发生。
    """
    config = read_config(config_file)
    diff = reconcile_diff(state_file, config, payload)
    accounts_payload = {
        str(item.get("name") or "").strip(): item
        for item in payload.get("accounts") or []
        if str(item.get("name") or "").strip()
    }
    if not accounts_payload and not diff["config_changes"]:
        raise ValueError("没有可写入的内容：账号与配置都是空的")

    ledger_written: list[str] = []
    if accounts_payload:
        import json

        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        accounts = data.setdefault("accounts", {})
        keys = data.setdefault("keys", {})
        now = time.time()
        for name, item in accounts_payload.items():
            blob = accounts.setdefault(name, {})
            events = [[float(ts), float(c)] for ts, c in blob.get("events") or []]
            window_start = now - WIN_5H
            burned_5h_now = sum(c for ts, c in events if ts > window_start)
            for field_name, current in (
                ("burned_5h", burned_5h_now),
                ("burned_week", sum(c for ts, c in events if ts > now - WIN_WEEK)),
            ):
                raw = item.get(field_name)
                if raw is None or str(raw).strip() == "":
                    continue
                filled = float(raw)
                gap = filled - current
                if abs(gap) < 0.5:
                    continue
                # 注入一个落在当前窗口内的校准事件，把估算值拉到运营者给的数上。
                # 负数 = 账本多记了，正数 = 少记了；burner 的 burned_since 只做
                # sum，两者都成立，且时间戳序列仍单调。
                events.append([now, round(gap, 4)])
                ledger_written.append(f"{name}.{field_name}")
            raw_total = item.get("credits_total")
            if raw_total not in (None, ""):
                blob["credits_total"] = round(float(raw_total), 2)
                ledger_written.append(f"{name}.credits_total")
            anchor = str(item.get("anchor") or "").strip()
            if anchor:
                blob["anchor_ts"] = _parse_hhmm(anchor)
                ledger_written.append(f"{name}.anchor_ts")
            week_anchor = str(item.get("week_anchor") or "").strip()
            if week_anchor:
                blob["week_anchor_ts"] = _parse_weekday_hhmm(week_anchor)
                ledger_written.append(f"{name}.week_anchor_ts")
            if item.get("burning") is False:
                # 「暂停这个账号」= 停靠一个窗口周期（burner 的 parked_until 语义）
                blob["parked_until"] = now + WIN_5H
                ledger_written.append(f"{name}.parked_until")
            blob["events"] = events[-20000:]
            accounts[name] = blob
            keys.setdefault(name, {})
        data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data["reconciled_at"] = now
        _atomic_write_json(state_file, data)

    config_written: list[str] = []
    if diff["config_changes"]:
        config_written = write_config(config_file, diff["config_changes"])

    return {
        "ledger_written": ledger_written,
        "config_written": config_written,
        "restart_keys": diff["restart_keys"],
        "accounts": diff["accounts"],
    }


def suggest_calibration(
    state_file: Path, config: dict[str, Any], actual_credits: float, account: str = ""
) -> dict[str, Any]:
    """按账本 token 量反推费率（与 burn_sensenova.suggest_rates 同一口径）。"""
    if actual_credits <= 0:
        raise ValueError("实扣积分要大于 0")
    status = read_status(state_file, config)
    if account:
        members = [a for a in status.accounts if a.name == account]
        if not members:
            raise ValueError(
                f"账本里没有账号 {account!r}；可用：" + ", ".join(a.name for a in status.accounts)
            )
        tin = sum(a.tokens_in for a in members)
        tout = sum(a.tokens_out for a in members)
    else:
        tin = sum(a.tokens_in for a in status.accounts)
        tout = sum(a.tokens_out for a in status.accounts)
    denom = tout + tin / 3
    if denom <= 0:
        raise ValueError("账本里这段时段没有任何 token，无法反推费率")
    r_out = actual_credits * 1e6 / denom
    return {
        "suggested": {"rate_in": round(r_out / 3, 2), "rate_out": round(r_out, 2)},
        "current": {"rate_in": status.rate_in, "rate_out": status.rate_out},
        "actual_credits": round(actual_credits, 2),
        "account": account or "",
        "ledger_tokens": {"tokens_in": tin, "tokens_out": tout},
        "ledger_saved_at": status.saved_at,
        "caveat": (
            "反推用的是账本累计 token；实扣积分必须覆盖同一时段，"
            "否则两者口径不一致，算出来的费率会偏高或偏低。"
        ),
    }

# --------------------------------------------------------------------------- #
# 重启：托盘文件 vs systemd，按部署形态二选一
#
# 同一个「保存配置并重启」动作，本机与服务器走完全不同的通道：
#   本机（Windows 托盘）：burner 由托盘 spawn，网关碰不到它。写一个
#     data/burner_restart.request，托盘心跳（3s）看到就重启再删文件。
#   服务器（systemd）：zkai-burner 是 systemd unit，托盘不存在，restart
#     request 文件写一万年也没人看。此时直接 systemctl restart。
#
# 判据刻意保守：只有看到 systemctl 能找到 zkai-burner 这个 unit 时才走
# systemd，否则一律退回托盘文件——误判成 systemd 会在本机执行一个必然失败
# 的命令，而托盘那条路是本机唯一正确的路。
# --------------------------------------------------------------------------- #
#: 服务器上 burner 的 systemd unit 名（deploy-server/zkai-burner.service）。
_SYSTEMD_UNIT = "zkai-burner"


def _systemctl() -> str | None:
    """systemctl 的绝对路径；找不到（Windows）返回 None。

    用绝对路径而不是裸 "systemctl"：既满足 S607，也避免 PATH 被污染时
    误调到一个同名的假命令。
    """
    return shutil.which("systemctl")


def _systemd_unit_active() -> bool:
    """unit 存在且我们能查到它（不一定 active——load 不到就是没这个 unit）。"""
    systemctl = _systemctl()
    if systemctl is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 固定命令+常量 unit 名，无外部输入
            [systemctl, "status", _SYSTEMD_UNIT, "--no-pager", "-n", "0"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # status 对 active/inactive/failed 都返回非 0/0 各异，但「没有这个 unit」会明确
    # 打到 stderr 的 "could not be found"；用返回码 3/4 之外的判据不可靠，
    # 所以直接看 stdout 里有没有 Loaded: 行。
    return "Loaded:" in proc.stdout


def detect_restart_mode(data_dir: Path) -> str:
    """'systemd' | 'tray' | 'manual'。"""
    if _systemd_unit_active():
        return "systemd"
    if os.name == "nt":
        return "tray"
    return "manual"


def request_restart_ex(data_dir: Path) -> dict[str, Any]:
    """按部署形态请求重启，返回 {mode, message, command, ok}。

    只有真正把重启发出去（或把信号文件写好）才 ok=True；manual 模式下
    ok=False，界面据此把手动命令显示给运营者。
    """
    mode = detect_restart_mode(data_dir)
    if mode == "systemd":
        systemctl = _systemctl() or "systemctl"
        try:
            proc = subprocess.run(  # noqa: S603 固定命令+常量 unit 名，无外部输入
                [systemctl, "restart", _SYSTEMD_UNIT],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "mode": "manual",
                "ok": False,
                "message": f"systemctl 重启失败：{exc}",
                "command": f"sudo systemctl restart {_SYSTEMD_UNIT}",
            }
        if proc.returncode == 0:
            return {
                "mode": "systemd",
                "ok": True,
                "message": f"已通过 systemd 重启 {_SYSTEMD_UNIT}",
                "command": f"systemctl restart {_SYSTEMD_UNIT}",
            }
        return {
            "mode": "manual",
            "ok": False,
            "message": f"systemctl 退出码 {proc.returncode}：{proc.stderr.strip()[:200]}",
            "command": f"sudo systemctl restart {_SYSTEMD_UNIT}",
        }
    if mode == "tray":
        path = request_restart(data_dir)
        return {
            "mode": "tray",
            "ok": True,
            "message": "已留下重启请求，托盘约 3 秒内重启消耗器",
            "command": "",
            "request_file": str(path),
        }
    return {
        "mode": "manual",
        "ok": False,
        "message": "检测不到托盘也检测不到 systemd unit，请手动重启消耗器",
        "command": "python scripts/burn_sensenova.py",
    }
