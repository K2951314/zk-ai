"""消耗器预算数学回归（scripts/burn_sensenova.py 的纯计算部分）。

背景：2026-09 的「滚动周窗口 + 默认费率偏下沿」组合导致熔断线物理上永远
触不到，专属池被烧穿后静默溢出扣通用池。这里钉死修复后的行为：
固定周窗口记账、安全系数默认 0.45、绝对上限停靠。
"""

from __future__ import annotations

import time

from scripts.burn_sensenova import (
    WIN_5H,
    WIN_WEEK,
    AccountState,
    Burner,
    KeyState,
    parse_args,
    parse_week_anchor,
)

# ---------------------------------------------------------------------------
# parse_week_anchor
# ---------------------------------------------------------------------------


def test_week_anchor_resolves_to_most_recent_weekday() -> None:
    ts = parse_week_anchor("Mon 00:00")
    lt = time.localtime(ts)
    assert lt.tm_wday == 0
    assert (lt.tm_hour, lt.tm_min) == (0, 0)
    assert ts <= time.time() < ts + WIN_WEEK


def test_week_anchor_same_day_future_time_rolls_back_a_week() -> None:
    now = time.time()
    lt = time.localtime(now)
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    # 今天是周几就传周几的 23:59（若当前时刻已过则不回滚；两种情况都必须 <= now+7d）
    ts = parse_week_anchor(f"{names[lt.tm_wday]} 23:59")
    assert now - WIN_WEEK <= ts
    # 恒等式：锚点必然落在 [now-7d, now] 内
    assert ts <= now


def test_week_anchor_rejects_garbage() -> None:
    for bad in ("", "Monday 00:00", "Mon", "Mon 25:00", "Xyz 10:00", "Mon 00:00x"):
        try:
            parse_week_anchor(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")


# ---------------------------------------------------------------------------
# 固定周窗口记账
# ---------------------------------------------------------------------------


def _acct_with(events: list[tuple[float, float]]) -> AccountState:
    acct = AccountState(name="t")
    acct.events = list(events)
    return acct


def test_burned_since_counts_only_events_in_fixed_window() -> None:
    now = time.time()
    acct = _acct_with([(now - 6 * 86400, 100.0), (now - 3600, 50.0), (now - 60, 10.0)])
    ws = now - 2 * 86400
    assert acct.burned_since(ws) == 60.0
    # 滚动 7 天口径仍包含 6 天前那笔（两者差异正是旧版溢出的根因）
    assert acct.burned(now, WIN_WEEK) == 160.0


def test_available_uses_fixed_week_window() -> None:
    now = time.time()
    acct = _acct_with([(now - 6 * 86400, 100.0)])
    ws = now - 86400
    # 固定周窗口内没烧过 → 周余量满；滚动口径则只剩 capweek-100
    assert acct.available(now, cap5h=1000.0, capweek=200.0, week_start=ws) == 200.0
    assert acct.available(now, cap5h=1000.0, capweek=200.0, week_start=0.0) == 100.0
    # 在飞成本要扣掉
    acct.inflight_cost = 30.0
    assert acct.available(now, 1000.0, 200.0, ws) == 170.0


def test_available_respects_5h_floor() -> None:
    now = time.time()
    acct = _acct_with([(now - 600, 400.0)])
    # 5h 余量 600 < 周余量 → 取小
    assert acct.available(now, cap5h=1000.0, capweek=10_000.0, week_start=now) == 600.0


# ---------------------------------------------------------------------------
# budget_allow / resume_time（经 Burner 走全链路）
# ---------------------------------------------------------------------------


def _burner(**overrides: object) -> Burner:
    argv = ["--concurrency", "1", "--summary-interval", "1"]
    for key, value in overrides.items():
        flag = f"--{key.replace('_', '-')}"
        if value is True:
            argv.append(flag)
        else:
            argv.extend([flag, str(value)])
    args = parse_args(argv)
    ks = KeyState(name="K1", key="sk-test", account=AccountState(name="K1"))
    return Burner(args, [ks])


def test_budget_trips_and_parks_until_next_week_boundary() -> None:
    burner = _burner(weekly_credits=1000, safety_margin=0.5, window_credits=10**9)
    acct = burner.keys[0].account
    now = time.time()
    ws = burner.week_start(now)
    acct.events = [(now - 60, 490.0)]  # 已烧 490 / 熔断线 500
    cost = 20.0  # 需要 20 > 余 10 → 触发
    assert burner.budget_allow(acct, cost) is False
    assert acct.parked_until >= ws + WIN_WEEK  # 停靠到下周锚点之后
    assert burner.budget_allow(acct, cost) is False  # 停靠期间继续拒绝


def test_budget_allows_when_within_fixed_week_cap() -> None:
    burner = _burner(weekly_credits=1000, safety_margin=0.5)
    acct = burner.keys[0].account
    now = time.time()
    acct.events = [(now - 60, 100.0)]
    old_park = acct.parked_until
    assert burner.budget_allow(acct, 100.0) is True
    assert acct.parked_until == old_park


def test_inflight_cost_subtracted_from_headroom() -> None:
    burner = _burner(weekly_credits=1000, safety_margin=0.5)
    acct = burner.keys[0].account
    acct.inflight_cost = 460.0  # 在飞已预订 460，余量只剩 40
    assert burner.budget_allow(acct, 50.0) is False


def test_absolute_total_cap_parks_account_forever() -> None:
    burner = _burner(pool_total_credits=500)
    acct = burner.keys[0].account
    acct.credits_total = 500.0
    assert burner.budget_allow(acct, 1.0) is False
    assert acct.parked_until == float("inf")
    assert "绝对上限" in acct.park_reason


def test_default_margin_is_conservative() -> None:
    args = parse_args([])
    # 0.45 = 官方上限的一半：费率即使处在实测区间上沿（≈估算 2 倍）也不烧穿
    assert args.safety_margin == 0.45
    assert args.week_anchor == "Mon 00:00"
    assert args.pool_total_credits == 0


def test_week_start_advances_across_boundaries() -> None:
    burner = _burner()
    now = time.time()
    ws = burner.week_start(now)
    assert ws <= now < ws + WIN_WEEK
    assert burner.week_start(ws + WIN_WEEK + 1) == ws + WIN_WEEK


def test_5h_rolling_still_applies() -> None:
    burner = _burner(window_credits=100, safety_margin=0.5)
    acct = burner.keys[0].account
    now = time.time()
    acct.events = [(now - 60, 60.0), (now - 6 * 3600, 1000.0)]  # 6h 前的不算 5h 窗口
    # 5h 内烧 60 / 熔断线 50 → 拒
    assert burner.budget_allow(acct, 1.0) is False
    # 停靠时刻应按 5h 滚动事件计算（>= 最老窗口内事件 + 5h）
    assert acct.parked_until >= now + WIN_5H - 120
