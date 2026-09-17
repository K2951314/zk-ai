"""Sliding-window proactive quotas (app/routing/limits.py)."""

from __future__ import annotations

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
