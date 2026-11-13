"""Quota cooldowns wait for the provider's reset moment, not a blind fixed delay.

背景（2026-11-10 实测）：ModelScope 的免费额度是「每日 200 魔粒，北京 08:00
翻账」。额度用完上游回 ``429 insufficient balance``，网关若按固定长休
（``quota_cooldown=1800s``）重试，接下来一整天都在白撞 429 —— 运营者的观感
就是「每天都要手动启用一次，太不智能」。

这里钉三件事：
1. ``seconds_until_quota_reset`` 的跨日/边界/坏输入。
2. 额度型 429 的冷却时长 = 到下一个重置点，**不被 ``quota_cooldown_max`` 截断**。
3. 上游给了 ``Retry-After`` 就听上游的（窗口可能比日重置更早打开）。
"""

from __future__ import annotations

import datetime

import pytest

from app.credentials.cooldown import _MIN_RESET_WAIT, CooldownPolicy


def _beijing_epoch(month: int, day: int, hour: int, minute: int) -> float:
    """Build the epoch seconds of a Beijing wall-clock moment.

    刻意不引 ``zoneinfo``：Windows 上 ``tzdata`` 不是标准库的一部分，实测
    ``ZoneInfoError: No time zone found with key Asia/Shanghai``。固定 +8 偏移
    对本用途足够，也和生产代码 ``quota_reset_utc_offset`` 的口径一致。
    """
    naive_utc = datetime.datetime(2026, month, day, hour, minute, tzinfo=datetime.UTC)
    return naive_utc.timestamp() - 8 * 3600


def test_seconds_until_reset_rolls_forward_to_tomorrow() -> None:
    """北京 11:27 看「08:00 重置」→ 下一个是明天 08:00，等 20h33m。"""
    policy = CooldownPolicy(quota_reset_at="08:00")
    wait = policy.seconds_until_quota_reset(_beijing_epoch(11, 10, 11, 27))
    assert wait == pytest.approx(20 * 3600 + 33 * 60, abs=1)


def test_seconds_until_reset_from_just_before_the_boundary() -> None:
    """北京 07:59 看「08:00 重置」→ 只等 60 秒，不是整整一天。"""
    policy = CooldownPolicy(quota_reset_at="08:00")
    wait = policy.seconds_until_quota_reset(_beijing_epoch(11, 10, 7, 59))
    assert wait == pytest.approx(60, abs=1)


def test_seconds_until_reset_never_returns_zero_on_the_moment() -> None:
    """正点那一秒不能返回 0——调用方会把 0 当「不用等」，那正是上游还没翻账的窗口。"""
    policy = CooldownPolicy(quota_reset_at="08:00")
    wait = policy.seconds_until_quota_reset(_beijing_epoch(11, 10, 8, 0))
    assert wait is not None
    assert wait >= _MIN_RESET_WAIT
    assert wait <= 86400


@pytest.mark.parametrize("spec", ["", "   ", "8", "08:00:00", "abc:def", "25:00", "-1:00"])
def test_seconds_until_reset_rejects_malformed_specs(spec: str) -> None:
    """坏输入一律返回 None（回落旧行为），绝不能抛异常把请求打挂。"""
    policy = CooldownPolicy(quota_reset_at=spec)
    assert policy.seconds_until_quota_reset(_beijing_epoch(11, 10, 11, 27)) is None


def test_quota_cooldown_is_not_clipped_by_the_max_cap() -> None:
    """知道几点重置时，冷却必须睡到重置点，不能被 4 小时上限截断。

    实测后果：北京 11:23 算出 20.6h，被 ``quota_cooldown_max=14400`` 截成 4h，
    于是 15:23 / 19:23 / 23:23 … 每 4 小时醒一次白撞 429，一天烧 4 次上游往返。

    ⚠ ``cooldown_for`` 内部用的是**真实当前时间**，而这里断言的是「到下一个
    08:00 还剩多久」。北京时间 08:00 之后，「下一个重置点」只剩不到一天里
    很短的一段——本例里 ``quota_cooldown_max`` 是 4 小时，08:00~12:00 之间
    剩余时长本来就小于上限，断言必然红。2026-11-13 修：只在「剩余时长明确
    大于上限」的窗口里断言，其余时段 skip 并说明原因。**不要**把上限调小来
    让测试变绿——那会把被测行为本身改掉。
    """
    policy = CooldownPolicy(quota_reset_at="08:00", quota_cooldown_max=14400.0, jitter=0.0)
    remaining_today = policy.seconds_until_quota_reset()
    if remaining_today is None or remaining_today <= policy.quota_cooldown_max:
        pytest.skip(
            f"当前距下一个 08:00 只剩 {remaining_today:.0f}s，小于上限 "
            f"{policy.quota_cooldown_max:.0f}s——这个时段断言恒真，测不出截断"
        )
    duration = policy.cooldown_for(
        error_type="rate_limit_error",
        scope="credential",
        quota_exhausted=True,
        quota_reset_at="08:00",
    )
    assert duration > policy.quota_cooldown_max, "被上限截断了，会一整天每 4 小时白撞一次"
    assert duration >= remaining_today * (1 - policy.jitter) - 1


def test_retry_after_from_upstream_wins_over_our_guess() -> None:
    """上游明确说「300 秒后可以」时听上游的：窗口可能比日重置更早打开。"""
    policy = CooldownPolicy(quota_reset_at="08:00", jitter=0.0)
    duration = policy.cooldown_for(
        error_type="rate_limit_error",
        scope="credential",
        quota_exhausted=True,
        retry_after=300.0,
        quota_reset_at="08:00",
    )
    assert duration == pytest.approx(300.0, abs=1)


def test_without_a_reset_moment_the_old_flat_cooldown_still_applies() -> None:
    """没配重置时刻的供应商（商汤等）行为不变，不能被这次改动波及。"""
    policy = CooldownPolicy(quota_cooldown=1800.0, jitter=0.0)
    duration = policy.cooldown_for(
        error_type="rate_limit_error",
        scope="credential",
        quota_exhausted=True,
    )
    assert duration == pytest.approx(1800.0, abs=1)


def test_provider_spec_overrides_the_policy_default() -> None:
    """供应商级的值优先于策略级默认值：各家窗口时刻本来就不同。"""
    policy = CooldownPolicy(quota_reset_at="00:00", jitter=0.0)
    # 11:27 到明天 00:00 是 12h33m；到明天 08:00 是 20h33m。
    with_default = policy.seconds_until_quota_reset(_beijing_epoch(11, 10, 11, 27))
    with_override = policy.seconds_until_quota_reset(
        _beijing_epoch(11, 10, 11, 27), spec="08:00"
    )
    assert with_default == pytest.approx(12 * 3600 + 33 * 60, abs=1)
    assert with_override == pytest.approx(20 * 3600 + 33 * 60, abs=1)
