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
    """Client disconnect mid-attempt must not be swallowed."""
    harness = await build_harness(make_config(providers=[provider_config]),
                                  adapters={"fake": fake_adapter})
    try:
        from app.models.request import ChatCompletionRequest

        request = ChatCompletionRequest(model="fake-model", messages=[{"role": "user", "content": "x"}])
        fake_adapter.queue(Behavior(text="never returned", delay=5.0))
        task = asyncio.create_task(harness.service.chat(request, request_id="req-cancel"))
        await asyncio.sleep(0.05)
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


def test_fake_adapter_is_used_for_every_attempt(harness: Harness) -> None:
    assert isinstance(harness.adapter, FakeAdapter)
