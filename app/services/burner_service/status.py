"""运行状态：从消耗器账本（data/burn_state.json）算窗口账与派生提示。

拆自 burner_service.py。账本可能正被消耗器写，故读取容忍半行：解析失败
时返回空状态而不是报错（控制台显示「暂无数据」比 500 有用）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.burner_service.config_io import (
    CONFIG_KEYS,
    FORM_FIELDS,
    is_flash_lite,
    read_config,
)
from app.services.burner_service.restart import restart_pending
from scripts.burn_sensenova import DEFAULT_SAFETY_MARGIN

WIN_5H = 5 * 3600
WIN_WEEK = 7 * 86400

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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
    # safety_margin 是配置项（burner.yaml 显式给定），不是运行时校准值——
    # 账本里的旧值不应覆盖配置文件。消耗器侧 _load_rates_from_state 在
    # yaml 显式给费率时也不会从账本恢复 safety_margin，这里保持同一口径。
    # 只在配置没给（回退默认 0.95）且账本有显式值时才用账本——实际上
    # 这条路径几乎不会走到，因为 burner.yaml 总是显式写 safety_margin。
    cfg_margin = float(config.get("safety_margin") or 0.0)
    if cfg_margin > 0:
        status.safety_margin = cfg_margin
    elif data.get("safety_margin"):
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
        "anchor_ts": acct.anchor_ts,
        "week_anchor_ts": acct.week_anchor_ts,
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


__all__ = [
    "WIN_5H",
    "WIN_WEEK",
    "AccountStatus",
    "BurnerStatus",
    "_parse_global_week_anchor",
    "_warnings",
    "account_view",
    "estimate_request_cost",
    "read_status",
    "snapshot",
]
