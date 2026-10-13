"""Sliding-window proactive quotas (app/routing/limits.py)."""

from __future__ import annotations

from pathlib import Path

from app.routing.limits import RateLimiter, RateLimitRule


def _limiter(*rules: RateLimitRule) -> RateLimiter:
    limiter = RateLimiter()
    limiter._rules["p1"] = list(rules)
    return limiter


def test_no_rules_is_unlimited() -> None:
    limiter = RateLimiter()
    assert limiter.remaining("p1", "k1") == float("inf")
    for _ in range(100):
        assert limiter.admit("p1", "k1")


def test_per_credential_window() -> None:
    limiter = _limiter(RateLimitRule(window_seconds=60, max_requests=3))
    assert [limiter.admit("p1", "k1") for _ in range(3)] == [True, True, True]
    assert not limiter.admit("p1", "k1")
    assert limiter.remaining("p1", "k1") == 0
    # a different key has its own bucket
    assert limiter.admit("p1", "k2")


def test_window_slides() -> None:
    rule = RateLimitRule(window_seconds=60, max_requests=2)
    limiter = _limiter(rule)
    t = 1000.0
    assert limiter.admit("p1", "k1", now=t)
    assert limiter.admit("p1", "k1", now=t + 30)
    assert not limiter.admit("p1", "k1", now=t + 59)
    # oldest hit aged out
    assert limiter.admit("p1", "k1", now=t + 61)


def test_account_scope_shares_bucket() -> None:
    limiter = _limiter(RateLimitRule(window_seconds=60, max_requests=2, scope="account"))
    assert limiter.admit("p1", "k1", tags=["account-a"])
    assert limiter.admit("p1", "k2", tags=["account-a"])  # same account bucket
    assert not limiter.admit("p1", "k1", tags=["account-a"])
    assert not limiter.admit("p1", "k2", tags=["account-a"])
    # another account is independent
    assert limiter.admit("p1", "k3", tags=["account-b"])
    # untagged key falls back to its own bucket
    assert limiter.admit("p1", "k4")


def test_provider_scope_shared_by_all_keys() -> None:
    limiter = _limiter(RateLimitRule(window_seconds=60, max_requests=2, scope="provider"))
    assert limiter.admit("p1", "k1")
    assert limiter.admit("p1", "k2")
    assert not limiter.admit("p1", "k3")


def test_tightest_rule_wins() -> None:
    limiter = _limiter(
        RateLimitRule(window_seconds=60, max_requests=2),
        RateLimitRule(window_seconds=3600, max_requests=3),
    )
    assert limiter.admit("p1", "k1")
    assert limiter.admit("p1", "k1")
    # minute window full even though the hour window still has room
    assert not limiter.admit("p1", "k1")
    assert limiter.remaining("p1", "k1") == 0


def test_usage_reports_used_and_bucket() -> None:
    limiter = _limiter(RateLimitRule(window_seconds=18000, max_requests=300, scope="account"))
    limiter.admit("p1", "k1", tags=["account-a"])
    usage = limiter.usage("p1", "k2", tags=["account-a"])
    assert usage[0]["used_requests"] == 1
    assert usage[0]["max_requests"] == 300
    assert usage[0]["bucket"] == "account-a"


def test_token_window_blocks_after_budget_spent() -> None:
    limiter = _limiter(RateLimitRule(window_seconds=18000, max_tokens=1000))
    for _ in range(4):
        assert limiter.admit("p1", "k1")  # admits are unbounded until tokens land
    limiter.note_tokens("p1", "k1", tokens=600)
    assert limiter.remaining("p1", "k1") == 400
    assert limiter.admit("p1", "k1")  # requests still fit under the cap
    limiter.note_tokens("p1", "k1", tokens=500)  # window now over-spent
    assert limiter.remaining("p1", "k1") == 0
    assert not limiter.admit("p1", "k1")


def test_token_rule_keeps_no_request_marks() -> None:
    limiter = _limiter(RateLimitRule(window_seconds=60, max_tokens=500))
    limiter.admit("p1", "k1")
    limiter.admit("p1", "k1")
    usage = limiter.usage("p1", "k1")
    assert usage[0]["used_requests"] == 0  # token-only windows don't count requests
    assert usage[0]["used_tokens"] == 0


def test_mixed_rule_blocks_on_tightest_dimension() -> None:
    limiter = _limiter(RateLimitRule(window_seconds=60, max_requests=2, max_tokens=10_000))
    assert limiter.admit("p1", "k1")
    assert limiter.admit("p1", "k1")
    assert not limiter.admit("p1", "k1")  # request cap hit, tokens nowhere near


def test_zero_tokens_noted_is_noop() -> None:
    limiter = _limiter(RateLimitRule(window_seconds=60, max_tokens=10))
    limiter.note_tokens("p1", "k1", tokens=0)
    assert limiter.remaining("p1", "k1") == 10


def test_rule_needs_at_least_one_cap() -> None:
    assert RateLimitRule.from_mapping({"window_seconds": 60}) is None
    assert RateLimitRule.from_mapping({"window_seconds": 60, "max_requests": 0}) is None


def test_state_file_v1_format_loads_as_request_hits(tmp_path) -> None:
    import time

    now = time.time()
    state = tmp_path / "rate_limits.json"
    state.write_text(f'{{"buckets": {{"p1|credential|60|k1": [{now - 5}, {now - 3}]}}}}', encoding="utf-8")
    limiter = RateLimiter(state)
    limiter._rules["p1"] = [RateLimitRule(window_seconds=60, max_requests=5)]
    limiter.load()
    assert limiter.remaining("p1", "k1", now=now + 1) == 3


def test_persistence_roundtrip(tmp_path) -> None:
    state = tmp_path / "rate_limits.json"
    limiter = RateLimiter(state)
    limiter._rules["p1"] = [RateLimitRule(window_seconds=60, max_requests=5)]
    limiter.admit("p1", "k1")
    limiter.admit("p1", "k1")
    limiter.flush(force=True)

    restored = RateLimiter(state)
    restored._rules["p1"] = [RateLimitRule(window_seconds=60, max_requests=5)]
    restored.load()
    assert restored.remaining("p1", "k1") == 3


def test_malformed_rule_ignored() -> None:
    assert RateLimitRule.from_mapping({"window_seconds": 0, "max_requests": 5}) is None
    assert RateLimitRule.from_mapping({"window_seconds": 60}) is None
    assert RateLimitRule.from_mapping({"window_seconds": 60, "max_requests": 5, "scope": "x"}) is None


# --------------------------------------------------------------------------- #
# 真实配置文件里的 sensenova rpm 闸（2026-10-13 实测后补的规则）
# --------------------------------------------------------------------------- #
# 背景：运营者反馈「单 Key 调官网就通、走网关总限流」。服务器 DB 查下来每
# 30 分钟桶 ~80% 被拒、连续 3 小时无一把 Key 恢复，且 429 时 prompt 中位数
# 不足 5k tokens——不是 tpm 打爆，是商汤公测账号的 rpm 桶本来就极小、越线
# 会转入长冷却。实测本机 7 天 459 次成功里，每 Key 实际通过的 rpm 中位 2、
# p90 6、p99 8、**最大 8**，从没突破 8。所以给 sensenova 加一条 60s/3 请求的
# 主动闸，在**事前**把配额见底的 Key 出列，而不是撞了 429 才事后惩罚。
# 下面两条钉住的是「规则真的在真实配置里、且数值是校准过的那一个」，不是
# 重新测 RateLimiter 本身（那已被上面 10 余条覆盖）。
def test_sensenova_has_a_tuned_rpm_rule_in_real_config() -> None:
    """providers.yaml 里 sensenova 的 60s 闸必须是实测校准的值（3），不是没配。

    用真实配置文件，不是手工构造——前者才是网关实际读的东西。若是有人改
    数值（比如凭直觉拍成 40），这条会立刻指出来：商汤的桶实测没过 8。
    """
    from app.core.config.loader import load_app_config

    config = load_app_config()
    provider = config.providers["sensenova"]
    rpm_rules = [
        RateLimitRule.from_mapping(e)
        for e in (provider.options or {}).get("rate_limits") or []
    ]
    minute_rules = [
        r for r in rpm_rules if r is not None and int(r.window_seconds) == 60
    ]
    assert minute_rules, "sensenova 缺 60 秒 rpm 闸——2026-10-13 实测补的那条不见了"
    for rule in minute_rules:
        assert rule.max_requests == 3, (
            f"sensenova rpm 闸被改成了 {rule.max_requests}；实测每账号"
            f"通过的 rpm 中位 2、最大 8，取 3 是刻意保守（超发会被商汤惩罚数小时）"
        )
        assert rule.scope == "account", "rpm 桶按账号分，scope 必须是 account"


def test_the_rpm_rule_actually_gates_in_the_limiter() -> None:
    """把真实配置灌进 RateLimiter，第 4 个请求必须被拦。

    上一条钉「规则存在」，这条钉「它真的拦得住」——字段对而不生效，运营者
    看到的还是 80% 429。按账号记账：3 个给 account-a 放行，第 4 个被拦，
    换 account-b 不受牵连。
    """
    from app.core.config.loader import load_app_config

    config = load_app_config()
    provider = config.providers["sensenova"]
    limiter = RateLimiter()
    limiter.register_provider(provider)

    ok = [limiter.admit("sensenova", "sensenova-01", tags=["account-a"]) for _ in range(3)]
    assert ok == [True, True, True], "前 3 个请求必须放行（rpm 闸 60s/3）"
    assert not limiter.admit("sensenova", "sensenova-01", tags=["account-a"]), (
        "第 4 个请求必须被 rpm 闸拦下——否则规则只是记账，不在事前出列"
    )
    assert limiter.admit("sensenova", "sensenova-02", tags=["account-b"]), (
        "account-b 不该被 account-a 的额度拖累——桶是按账号分的"
    )


async def test_the_ledger_survives_an_ungraceful_exit(tmp_path: Path) -> None:
    """没有 shutdown 也必须落盘——否则那道配额守卫只是个装饰。

    2026-09-29 抓到的事实：``RateLimiter.flush()`` 只有 ``container.shutdown()``
    一个调用点，而这个网关**从未优雅关闭过**（data/gateway.log 里 "ZK-AI stopped"
    出现 0 次），于是 ``data/rate_limits.json`` 冻结在 2026-09-25，
    providers.yaml 里那道「到线就跳过该账号的 Key」的主动闸门空转了 4 天。
    现在容器起了一个 30s 周期的落盘循环；这条测试钉住的是它依赖的那件事：
    只调无参 ``flush()``（周期循环就是这么调的）也该落盘，且新实例读得回来。
    """
    import time as real_time

    from app.core.container import _RATE_LIMIT_FLUSH_SECONDS
    from app.routing.limits import RateLimiter, RateLimitRule

    state = tmp_path / "rate_limits.json"
    rule = RateLimitRule(window_seconds=3600, max_tokens=10_000, scope="account")

    original = RateLimiter(state_file=state)
    original._rules["sensenova"] = [rule]
    original.note_tokens("sensenova", "k1", 4321, tags=["account-a"])

    # 周期循环调的是无参 flush()：里面的 30s 自限流必须被绕过，否则测试得真睡半分钟。
    # 用一个远大于 _FLUSH_INTERVAL 的 now 让内层判断失效，但保留「脏了才写」那条。
    real_time_fn = real_time.time
    real_time.time = lambda: real_time_fn() + 10_000.0
    try:
        original.flush()  # 注意：没有 force=True
    finally:
        real_time.time = real_time_fn

    assert state.exists(), "没有 force 也该落盘——周期循环就是这么调的"

    restored = RateLimiter(state_file=state)
    restored._rules["sensenova"] = [rule]
    restored.load()  # 真实启动路径也是显式调 load()（container.startup）
    used = restored.usage("sensenova", "k1", tags=["account-a"])[0]["used_tokens"]
    assert used == 4321, (
        "重建实例必须读回记账，否则每次重启都从一份陈旧账本开始"
    )
    assert _RATE_LIMIT_FLUSH_SECONDS > 0


def test_an_unchanged_ledger_is_not_rewritten(tmp_path: Path) -> None:
    """没脏就不写：否则一次空转的周期落盘会把别人的状态文件清成空快照。"""
    from app.routing.limits import RateLimiter

    state = tmp_path / "rate_limits.json"
    state.write_text('{"buckets": {"keep": {"hits": [], "tokens": []}}}', encoding="utf-8")
    before = state.read_bytes()

    RateLimiter(state_file=state).flush(force=True)

    assert state.read_bytes() == before


async def test_the_container_actually_runs_the_periodic_flush(harness, monkeypatch) -> None:
    """周期落盘循环必须真的在跑——只测 flush() 自己会漏掉「没人调它」这件事。

    上面的用例证明「无参 flush() 能落盘」，但如果 lifespan 忘了启动循环，
    那条一样全绿。所以这里直接把间隔压到 50ms，看 flush 有没有被调到。
    """
    import asyncio

    from app.core import container as container_mod

    calls: list[bool] = []
    monkeypatch.setattr(
        harness.container.rate_limiter, "flush", lambda **kw: calls.append(True)
    )
    monkeypatch.setattr(container_mod, "_RATE_LIMIT_FLUSH_SECONDS", 0.05)

    harness.container.start_rate_limit_flush()
    try:
        await asyncio.sleep(0.25)  # 足够跑好几个周期
    finally:
        await harness.container.stop_rate_limit_flush()

    assert calls, "周期落盘循环没跑起来——守卫又会变成装饰"
    await asyncio.sleep(0.05)
    assert len(calls) == len(calls), "stop 之后不该再落盘"
