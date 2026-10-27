"""Retry, backoff, cooldown and failover behaviour of the scheduler."""

from __future__ import annotations

import asyncio
import random

import httpx
import pytest

from app.core.errors import (
    AllAttemptsFailedError,
    ContextLengthExceededError,
    InvalidRequestError,
)
from app.retry.backoff import BackoffCalculator, compute_delay, parse_retry_after_delay
from app.retry.classifier import ErrorClass, ErrorClassifier, ErrorInfo
from app.retry.policy import RetryPolicy
from tests.conftest import (
    Behavior,
    FakeAdapter,
    Harness,
    build_harness,
    make_alias,
    make_config,
    make_model,
)


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #
def test_exponential_backoff_grows_and_is_capped() -> None:
    delays = [compute_delay(attempt, jitter_mode="none", base_delay=0.5, multiplier=2.0,
                            max_delay=8.0) for attempt in range(1, 8)]
    assert delays == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_backoff_jitter_stays_within_bounds() -> None:
    """attempt 3 with base 1.0 -> 4.0, jittered by +/-30%."""
    rng = random.Random(1234)
    for _ in range(50):
        delay = compute_delay(3, base_delay=1.0, max_delay=10.0, jitter=0.3,
                              jitter_mode="full", rng=rng)
        assert 2.8 <= delay <= 5.2


def test_backoff_equal_jitter_mode() -> None:
    rng = random.Random(7)
    delay = compute_delay(2, base_delay=1.0, jitter=0.5, jitter_mode="equal", rng=rng)
    assert 1.0 <= delay <= 2.0


def test_backoff_none_mode_is_deterministic() -> None:
    assert compute_delay(4, base_delay=1.0, jitter=0.9, jitter_mode="none") == 8.0


def test_retry_after_is_clamped() -> None:
    assert parse_retry_after_delay(None) is None
    assert parse_retry_after_delay(5) == 5.0
    assert parse_retry_after_delay(10_000, cap=60) == 60.0


def test_backoff_honours_retry_after_header() -> None:
    calculator = BackoffCalculator(base_delay=1.0, max_delay=4.0)
    assert calculator.delay_for_error(1, retry_after=3.0) == 3.0
    # The cap is max_delay * 4 for provider-supplied values.
    assert calculator.delay_for_error(1, retry_after=100.0) == 16.0


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
def test_policy_never_retries_client_errors() -> None:
    policy = RetryPolicy(max_retries_per_credential=5)
    info = ErrorClassifier().classify_status(400)
    assert policy.should_retry(info, retries_done=0) is False
    assert info.retryable is False


def test_policy_retries_transient_errors_until_the_budget_is_spent() -> None:
    policy = RetryPolicy(max_retries_per_credential=2, base_delay=0.5, jitter=0.0,
                         jitter_mode="none", max_delay=4.0)
    info = ErrorClassifier().classify_status(500)
    assert policy.should_retry(info, retries_done=0) is True
    assert policy.should_retry(info, retries_done=1) is True
    assert policy.should_retry(info, retries_done=2) is False
    assert policy.delay_for(1, info) == 0.5
    assert policy.delay_for(2, info) == 1.0


def test_policy_clamps_its_own_limits() -> None:
    policy = RetryPolicy(max_total_attempts=0, max_retries_per_credential=-4,
                         max_credentials_per_deployment=0, max_deployments=0)
    assert policy.max_total_attempts == 1
    assert policy.max_retries_per_credential == 0
    assert policy.max_credentials_per_deployment == 1
    assert policy.max_deployments == 1


def test_policy_from_mapping_keeps_unknown_keys_as_options() -> None:
    policy = RetryPolicy.from_mapping(
        {"max_retries_per_credential": 4, "base_delay": 1.5, "something_else": True}
    )
    assert policy.max_retries_per_credential == 4
    assert policy.base_delay == 1.5
    assert policy.options == {"something_else": True}


# --------------------------------------------------------------------------- #
# Scheduler: retry
# --------------------------------------------------------------------------- #
async def test_500_is_retried_then_succeeds(harness: Harness) -> None:
    harness.adapter.queue(Behavior(status=500), Behavior(text="recovered"))
    response = await harness.request()
    assert response.text() == "recovered"
    assert len(harness.all_calls()) == 2
    assert harness.sleeps == [0.0]  # one backoff between the two attempts


async def test_retry_budget_is_exhausted_before_failover(harness: Harness) -> None:
    """1 + max_retries_per_credential attempts per credential, then the next key."""
    harness.adapter.queue_status(500, 500, 500, 500, 500, 500, 500, 500)
    with pytest.raises(AllAttemptsFailedError) as excinfo:
        await harness.request()
    attempts = excinfo.value.attempts
    assert len(attempts) == harness.container.config.retry.max_total_attempts
    # Each key gets 3 attempts (1 initial + 2 retries) before rotation.
    credentials = [call["credential_id"] for call in harness.all_calls()]
    assert credentials[0] == credentials[1] == credentials[2]
    assert credentials[3] != credentials[0]


async def test_400_is_not_retried_and_does_not_rotate(harness: Harness) -> None:
    harness.adapter.queue_status(400)
    with pytest.raises(InvalidRequestError):
        await harness.request()
    assert len(harness.all_calls()) == 1
    assert harness.all_calls()[0]["credential_id"] == "key-1"


async def test_413_is_not_retried_and_does_not_rotate(harness: Harness) -> None:
    harness.adapter.queue_status(413)
    with pytest.raises(ContextLengthExceededError):
        await harness.request()
    assert len(harness.all_calls()) == 1


async def test_max_total_attempts_ceiling_is_respected(harness: Harness) -> None:
    harness.container.config.retry.max_total_attempts = 4
    harness.scheduler.policy = harness.container.config.retry
    harness.adapter.queue_status(*([500] * 20))
    with pytest.raises(AllAttemptsFailedError) as excinfo:
        await harness.request()
    assert len(harness.all_calls()) == 4
    assert len(excinfo.value.attempts) == 4


async def test_retry_respects_the_configured_backoff_schedule(harness: Harness) -> None:
    policy = RetryPolicy(
        max_retries_per_credential=2, max_credentials_per_deployment=1, max_deployments=1,
        max_total_attempts=3, base_delay=0.5, max_delay=4.0, jitter=0.0, jitter_mode="none",
    )
    harness.scheduler.policy = policy
    harness.adapter.queue_status(500, 500, 500)
    with pytest.raises(AllAttemptsFailedError):
        await harness.request()
    assert harness.sleeps == [0.5, 1.0]


# --------------------------------------------------------------------------- #
# Scheduler: cooldown + failover
# --------------------------------------------------------------------------- #
async def test_429_puts_the_key_in_cooldown_and_uses_the_next_one(harness: Harness) -> None:
    harness.adapter.queue(Behavior(status=429), Behavior(text="from key two"))
    response = await harness.request()
    assert response.text() == "from key two"
    calls = harness.all_calls()
    assert [call["credential_id"] for call in calls] == ["key-1", "key-2"]
    assert harness.pool.get("key-1").status.value == "cooldown"
    assert harness.pool.get("key-2").status.value == "healthy"


async def test_401_disables_the_key_and_rotates(harness: Harness) -> None:
    harness.adapter.queue(Behavior(status=401), Behavior(text="ok"))
    response = await harness.request()
    assert response.text() == "ok"
    assert harness.pool.get("key-1").status.value == "unhealthy"
    assert [call["credential_id"] for call in harness.all_calls()] == ["key-1", "key-2"]


# --------------------------------------------------------------------------- #
# Scheduler: conversation affinity (sticky sessions across failover)
# --------------------------------------------------------------------------- #
async def test_affinity_re_anchors_after_failover_and_beats_priority(harness: Harness) -> None:
    """After a conversation lands on key-3, it stays there even once key-1 recovers."""
    # Turn 1: key-1 & key-2 throttle (429), key-3 serves -> pin key-3.
    harness.adapter.queue_status(429, 429, 200)
    await harness.request()
    assert harness.all_calls()[-1]["credential_id"] == "key-3"
    # Bring the higher-priority keys fully back.
    harness.pool.invalidate_cooldowns("fake")
    assert next(c.id for c in harness.pool.candidates("fake")) == "key-1"  # rotation says key-1
    # Turn 2: same conversation -> pinned to key-3, NOT the priority leader key-1.
    harness.adapter.queue(Behavior(text="again"))
    await harness.request()
    assert harness.all_calls()[-1]["credential_id"] == "key-3"


async def test_affinity_is_per_conversation(harness: Harness) -> None:
    """A different first message is a different session: it keeps priority order."""
    harness.adapter.queue_status(429, 429, 200)
    await harness.request()  # "hello" conversation -> pinned to key-3
    harness.pool.invalidate_cooldowns("fake")
    # A brand-new conversation (different opener) has no pin, so key-1 leads.
    harness.adapter.queue(Behavior(text="fresh"))
    await harness.request(messages=[{"role": "user", "content": "totally different"}])
    assert harness.all_calls()[-1]["credential_id"] == "key-1"


async def test_session_key_is_stable_and_prefers_explicit_id(harness: Harness) -> None:
    from app.models.request import ChatCompletionRequest

    sched = harness.scheduler
    base = ChatCompletionRequest(
        model="m", messages=[{"role": "user", "content": "hi"}]
    )
    grown = ChatCompletionRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "x"}],
    )
    assert sched._session_key(base) == sched._session_key(grown)  # length-independent
    explicit = ChatCompletionRequest(
        model="m", messages=[{"role": "user", "content": "hi"}], session_id="abc"
    )
    assert sched._session_key(explicit) == "id:abc"


async def test_timeout_retries_then_fails_over_to_the_next_deployment(
    provider_config, fake_adapter
) -> None:
    from app.models.request import ChatCompletionRequest

    config = make_config(
        providers=[provider_config],
        models=[
            make_model("primary", priority=100),
            make_model("secondary", priority=90),
        ],
        aliases=[make_alias("zk-x", ["primary", "secondary"])],
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    try:
        request = ChatCompletionRequest(
            model="zk-x", messages=[{"role": "user", "content": "hi"}]
        )
        fake_adapter.queue(
            Behavior(error=httpx.ReadTimeout("slow")),
            Behavior(error=httpx.ReadTimeout("slow")),
            Behavior(error=httpx.ReadTimeout("slow")),
            Behavior(text="secondary answered"),
        )
        response = await harness.service.chat(request, request_id="req-timeout")
        assert response.text() == "secondary answered"
        calls = harness.all_calls()
        assert calls[0]["model"] == calls[1]["model"] == calls[2]["model"] == "primary"
        assert calls[3]["model"] == "secondary"
    finally:
        await harness.container.shutdown()


async def test_deployment_without_usable_credentials_is_audited_with_unique_numbers(
    provider_config, fake_adapter
) -> None:
    """A keyless credential pool must produce a monotonic attempt trail, not 503s
    with duplicated ``attempt_number`` values (and never a 401)."""
    config = make_config(
        providers=[provider_config],
        models=[
            make_model("primary", priority=100),
            make_model("secondary", priority=90),
        ],
        aliases=[make_alias("zk-noauth", ["primary", "secondary"])],
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    try:
        from app.models.request import ChatCompletionRequest

        harness.pool.disable("key-1")
        harness.pool.disable("key-2")
        harness.pool.disable("key-3")
        request = ChatCompletionRequest(
            model="zk-noauth", messages=[{"role": "user", "content": "x"}]
        )
        with pytest.raises(AllAttemptsFailedError) as excinfo:
            await harness.service.chat(request, request_id="req-noauth")

        error = excinfo.value
        assert error.http_status == 503  # never 401: our own key is not the problem
        numbers = [attempt.attempt_number for attempt in error.attempts]
        assert numbers == sorted(set(numbers))  # unique and increasing
        assert len(numbers) == 2  # one pseudo-attempt per deployment
        assert all(attempt.credential_id is None for attempt in error.attempts)
        assert harness.all_calls() == []  # nothing was ever sent upstream
    finally:
        await harness.container.shutdown()


async def test_503_cools_the_deployment_down(harness: Harness) -> None:
    harness.adapter.queue_status(503, 503, 503, 503, 503, 503, 503, 503)
    with pytest.raises(AllAttemptsFailedError):
        await harness.request()
    deployment_id = harness.container.config.models["fake-model"].deployments[0].id
    assert harness.scheduler.deployment_cooling_down(deployment_id) is True


async def test_max_deployments_limits_failover_depth(provider_config, fake_adapter) -> None:
    config = make_config(
        providers=[provider_config],
        models=[make_model(f"m{index}", priority=100 - index) for index in range(5)],
        aliases=[make_alias("zk-many", [f"m{index}" for index in range(5)])],
    )
    config.retry.max_deployments = 2
    config.retry.max_credentials_per_deployment = 1
    config.retry.max_retries_per_credential = 0
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    try:
        from app.models.request import ChatCompletionRequest

        fake_adapter.queue_status(*([500] * 10))
        request = ChatCompletionRequest(model="zk-many", messages=[{"role": "user", "content": "x"}])
        with pytest.raises(AllAttemptsFailedError):
            await harness.service.chat(request, request_id="req-depth")
        assert len(harness.all_calls()) == 2
    finally:
        await harness.container.shutdown()


async def test_cancellation_is_recorded_and_re_raised(provider_config, fake_adapter) -> None:
    """Client disconnect mid-attempt must not be swallowed.

    同步方式从「睡 0.05s 赌机器够快」改成**等适配器真的被进入**（FakeAdapter
    在延迟前记录调用）——满载全量套件下旧写法会让 0.05s 睡过头，5s 延迟先
    跑完，cancel 落到已完成的任务上变成 no-op，测试假红（2026-10-24 实测）。
    """
    harness = await build_harness(make_config(providers=[provider_config]),
                                  adapters={"fake": fake_adapter})
    try:
        from app.models.request import ChatCompletionRequest

        request = ChatCompletionRequest(model="fake-model", messages=[{"role": "user", "content": "x"}])
        fake_adapter.queue(Behavior(text="never returned", delay=5.0))
        task = asyncio.create_task(harness.service.chat(request, request_id="req-cancel"))
        # 等到适配器被真正进入（调用已记录）再取消——与机器快慢无关
        for _ in range(500):
            if harness.all_calls():
                break
            await asyncio.sleep(0.01)
        assert harness.all_calls(), "适配器一直没被进入，测试前提不成立"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        recent = await harness.container.request_repository.recent(limit=5)
        assert recent[0]["status"] == "cancelled"
    finally:
        await harness.container.shutdown()


def test_error_info_helper_and_classifier_defaults() -> None:
    info = ErrorInfo("timeout", ErrorClass.TRANSIENT, 408, "t")
    assert info.to_error().error_type == "timeout"
    assert ErrorClassifier(default_cooldown=5).default_cooldown == 5


async def test_every_attempt_goes_through_the_registered_adapter(harness: Harness) -> None:
    """每次尝试都必须走已注册的适配器路径——不能有一条分支绕过它直连 httpx。

    原来这条叫 ``test_fake_adapter_is_used_for_every_attempt``，函数体只有
    ``assert isinstance(harness.adapter, FakeAdapter)``——那是在断言「夹具还是
    夹具」（``harness.adapter`` 的定义就是返回调用方传进来的那个），零不变量。
    2026-09-29 第三轮审查标记为纯自证用例，改成名字承诺的那件事。
    """
    harness.adapter.queue_status(503, 503, 503)  # 逼它换 Key / 换部署，制造多次尝试
    with pytest.raises(AllAttemptsFailedError):
        await harness.request()

    calls = harness.all_calls()
    assert len(calls) > 1, "没有多次尝试就什么都没验"
    # 每一次尝试都落在同一个已注册适配器上（FakeAdapter 只在被调用时记录）
    assert {call["provider"] for call in calls} == {"fake"}, (
        f"有尝试绕过了已注册的适配器：{[call.get('provider') for call in calls]}"
    )
    assert isinstance(harness.adapter, FakeAdapter)


async def test_deployment_affinity_pins_a_session_to_one_upstream(provider_config, fake_adapter) -> None:
    """会话粘性：同一会话钉住上次成功的部署，前缀缓存才能滚雪球。

    2026-10-24 实测：ds 别名 round_robin 在多个上游间轮换，同一场对话每轮
    打到不同端点、每轮撞没暖的缓存——harness 直连 99% 命中，网关只有 3%。
    """
    from app.models.provider import AliasStrategy
    from app.models.request import ChatCompletionRequest
    from tests.conftest import make_alias, make_config, make_model, make_provider

    other = make_provider("other", key_ids=("ok-1",))
    config = make_config(
        providers=[provider_config, other],
        models=[
            make_model("m-a", provider_id="fake", priority=100),
            make_model("m-b", provider_id="other", priority=100),
        ],
        aliases=[make_alias("zk-rr", ["m-a", "m-b"], strategy=AliasStrategy.ROUND_ROBIN)],
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter, "other": fake_adapter})
    try:
        request = ChatCompletionRequest(model="zk-rr", messages=[{"role": "user", "content": "hi"}])
        await harness.service.chat(request, request_id="req-1")
        first_model = harness.all_calls()[-1]["model"]

        request2 = ChatCompletionRequest(model="zk-rr", messages=[
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"},
            {"role": "user", "content": "again"},
        ])
        await harness.service.chat(request2, request_id="req-2")
        assert harness.all_calls()[-1]["model"] == first_model, "同一会话要钉在同一部署"
    finally:
        await harness.container.shutdown()


async def test_deployment_affinity_capacity_follows_the_pool(harness: Harness) -> None:
    """部署粘性的容量必须**活读**池子（credential_affinity.max_sessions）。

    2026-10-24 实锤：scheduler 曾用 getattr 猜属性名（少了下划线）把容量
    钉死在魔数 64——运营者改配置怎么都不生效；对抗式审查又发现构造期快照
    会让热重载后的新值永远到不了这里。现在现场读池子，改容量立刻见效。
    """
    assert harness.pool.affinity_max_sessions >= 4096  # 容器默认，不是 64
    harness.pool._affinity_max_sessions = 3  # 缩小容量，LRU 淘汰必须跟着缩
    for index in range(5):
        harness.scheduler._note_deployment_affinity(f"s-{index}", "fake-model")
    assert len(harness.scheduler._deployment_affinity) == 3
    assert "s-0" not in harness.scheduler._deployment_affinity
    assert "s-4" in harness.scheduler._deployment_affinity


async def test_log_usage_marks_estimates(caplog) -> None:
    """网关估算的 usage 必须带 (estimated)，不许冒充上游实报的缓存数字。"""
    from types import SimpleNamespace

    from app.models.response import Usage
    from app.routing.scheduler import Scheduler

    candidate = SimpleNamespace(deployment=SimpleNamespace(provider_id="p", model="m"))
    credential = SimpleNamespace(id="k1")
    with caplog.at_level("INFO", logger="zkai.routing.scheduler"):
        Scheduler._log_usage(
            candidate, credential, Usage(prompt_tokens=100, cached_tokens=50), estimated=True
        )
        Scheduler._log_usage(candidate, credential, Usage(prompt_tokens=100, cached_tokens=100))
    messages = [r.getMessage() for r in caplog.records]
    assert any("(estimated)" in m for m in messages)
    assert any("cached=100 (100%)" in m and "(estimated)" not in m for m in messages)


async def test_success_logs_cache_numbers(harness: Harness, caplog) -> None:
    """缓存命中数必须落进日志（2026-10-24 缓存分析 §4-E）。

    ``usage.cached_tokens`` 全链打通却没人写日志，``grep cached
    data/gateway.log`` 恒为 0——命中率优化缺第一手数据。
    """
    harness.adapter.queue(Behavior(text="ok", prompt_tokens=1000, completion_tokens=5))
    with caplog.at_level("INFO", logger="zkai.routing.scheduler"):
        await harness.request()
    assert any(
        "cached=" in r.getMessage() and "prompt=1000" in r.getMessage()
        for r in caplog.records
    )


# --------------------------------------------------------------------------- #
# 单腿时间预算：一条慢腿不许吃光整个请求的墙钟（2026-10-25 实测 701s/300s）
# --------------------------------------------------------------------------- #
async def test_a_slow_leg_cannot_consume_the_whole_wall_clock_budget(
    provider_config, fake_adapter
) -> None:
    """两条腿时，第一腿的时间片必须是「剩余预算的一半」，不是全部。

    背景（2026-10-25 实测）：AMD 单次尝试跑 701 秒，而请求预算 300 秒；
    154 条失败请求里 48 条（31%）就这么死的，另外两家供应商全程没被试。
    """
    from app.models.request import ChatCompletionRequest

    config = make_config(
        providers=[provider_config],
        models=[
            make_model("primary", priority=100),
            make_model("secondary", priority=90),
        ],
        aliases=[make_alias("zk-budget", ["primary", "secondary"])],
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    try:
        request = ChatCompletionRequest(
            model="zk-budget", messages=[{"role": "user", "content": "hi"}]
        )
        fake_adapter.queue(
            Behavior(error=httpx.ReadTimeout("slow")),
            Behavior(text="secondary answered"),
        )
        await harness.service.chat(request, request_id="req-budget")
        calls = harness.all_calls()
        first = calls[0]["attempt_timeout"]
        # 300s 预算 / 2 条腿 - 5s 余量 = ~145s，且必须远小于 300s 全额。
        assert 0 < first <= 300.0
        assert first < 200.0, f"第一条腿拿到了 {first}s，等于没给后面的腿留预算"
    finally:
        await harness.container.shutdown()


async def test_attempt_timeout_never_drops_below_the_floor(
    provider_config, fake_adapter
) -> None:
    """腿很多、预算快用光时，时间片收敛到 floor 而不是 0/负数。

    给 100k prefill 切 15 秒是必超时——那会把「试遍每家」变成「每家都失败」，
    比原来的病更糟。所以下限必须存在且可观测。
    """
    from app.models.request import ChatCompletionRequest

    config = make_config(
        providers=[provider_config],
        models=[
            make_model("m1", priority=100),
            make_model("m2", priority=99),
            make_model("m3", priority=98),
            make_model("m4", priority=97),
            make_model("m5", priority=96),
        ],
        aliases=[make_alias("zk-many", ["m1", "m2", "m3", "m4", "m5"])],
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    try:
        # 预算压到很小，逼出 floor 分支。
        harness.scheduler.max_request_seconds = 30.0
        request = ChatCompletionRequest(
            model="zk-many", messages=[{"role": "user", "content": "hi"}]
        )
        fake_adapter.queue(*[Behavior(error=httpx.ReadTimeout("slow")) for _ in range(6)])
        fake_adapter.default = Behavior(error=httpx.ReadTimeout("slow"))
        with pytest.raises((AllAttemptsFailedError, ContextLengthExceededError)):
            await harness.service.chat(request, request_id="req-floor")
        floor = harness.scheduler.policy.attempt_timeout_floor
        assert floor > 0
        for call in harness.all_calls():
            assert call["attempt_timeout"] >= floor or call["attempt_timeout"] == 0.0
    finally:
        await harness.container.shutdown()


async def test_attempt_timeout_can_be_disabled(provider_config, fake_adapter) -> None:
    """``attempt_timeout_floor: 0`` 回到旧行为（不切时间片），供运营者回滚。"""
    from app.models.request import ChatCompletionRequest

    config = make_config(
        providers=[provider_config],
        models=[make_model("primary", priority=100)],
        aliases=[make_alias("zk-legacy", ["primary"])],
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    try:
        harness.scheduler.policy.attempt_timeout_floor = 0.0
        request = ChatCompletionRequest(
            model="zk-legacy", messages=[{"role": "user", "content": "hi"}]
        )
        fake_adapter.queue(Behavior(text="ok"))
        await harness.service.chat(request, request_id="req-legacy")
        assert harness.all_calls()[0]["attempt_timeout"] == 0.0
    finally:
        await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# 整池冷却：短暂排队而不是「一次上游都没发」的瞬时 503
# --------------------------------------------------------------------------- #
async def test_a_cooling_pool_waits_instead_of_failing_fast(
    provider_config, fake_adapter, monkeypatch
) -> None:
    """全部 Key 都在冷却时，先等一会儿（不消耗上游配额），而不是立刻 503。

    实测形状（2026-10-24 起 154 条失败里 55 条）：一个请求扫空整池 → 接下来
    一批请求 0.01 秒内全部 ``no_available_credential``，一次上游都没发。
    容量本来够（商汤 27 req/min vs 客户端 10 req/min），缺的只是让渡。
    """
    from app.models.request import ChatCompletionRequest

    config = make_config(
        providers=[provider_config],
        models=[make_model("primary", priority=100)],
        aliases=[make_alias("zk-wait", ["primary"])],
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    try:
        # 全部 Key 进入冷却（模拟「上一个请求把它们打空」）。
        from app.retry.classifier import ErrorInfo

        throttle = ErrorInfo(
            "rate_limit_error", ErrorClass.THROTTLE, 429, "rpm",
            retryable=False, switch_credential=True, switch_provider=False,
            cooldown_scope="credential", cooldown_seconds=45.0,
        )
        for key in ("key-1", "key-2", "key-3"):
            harness.pool.tracker.on_failure(harness.pool.get(key), throttle)

        request = ChatCompletionRequest(
            model="zk-wait", messages=[{"role": "user", "content": "hi"}]
        )
        fake_adapter.queue(Behavior(text="after wait"))
        # 第一次取候选为空 → 排队 → 重取。这里让排队后仍为空（真实冷却没到期），
        # 但必须看到 sleep 被调用过，且时间不超过上限。
        with pytest.raises((AllAttemptsFailedError, ContextLengthExceededError)):
            await harness.service.chat(request, request_id="req-wait")
        assert harness.sleeps, "整池冷却时应当排队等待，而不是立刻失败"
        assert max(harness.sleeps) <= 10.0 + 0.001
    finally:
        await harness.container.shutdown()
