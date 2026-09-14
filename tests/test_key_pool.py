"""Credential pool: explicit state machine, rotation, cooldown and recovery."""

from __future__ import annotations

import random
import time

import pytest

from app.credentials.cooldown import CooldownPolicy
from app.credentials.health import CredentialHealthTracker
from app.credentials.pool import CredentialPool
from app.credentials.rotation import (
    FastestRotation,
    LeastFailuresRotation,
    PriorityRotation,
    RoundRobinRotation,
    WeightedRotation,
    available_rotations,
    get_rotation,
)
from app.models.credential import CredentialStatus
from app.models.provider import CredentialConfig, ProviderConfig, ProviderType
from app.retry.classifier import ErrorClass, ErrorClassifier, ErrorInfo
from tests.conftest import make_provider

CLASSIFIER = ErrorClassifier()


def failure(status: int) -> ErrorInfo:
    return CLASSIFIER.classify_status(status, body={"error": {"message": "x"}})


def build_pool(
    *,
    rotation: str = "priority",
    keys: tuple[tuple[str, int], ...] = (("k1", 100), ("k2", 90), ("k3", 80)),
    env_prefix: str | None = None,
) -> tuple[CredentialPool, ProviderConfig]:
    credentials = [
        CredentialConfig(
            id=key_id,
            env=f"${{{env_prefix}_{key_id.upper()}}}" if env_prefix else None,
            value=None if env_prefix else f"secret-{key_id}",
            priority=priority,
        )
        for key_id, priority in keys
    ]
    provider = ProviderConfig(
        id="p1", type=ProviderType.OPENAI, base_url="http://127.0.0.1:9/v1", credentials=credentials
    )
    pool = CredentialPool(rotation=rotation, allow_inline_secrets=True)
    pool.register_provider(provider)
    return pool, provider


# --------------------------------------------------------------------------- #
# Selection and rotation
# --------------------------------------------------------------------------- #
def test_candidates_are_ordered_by_priority() -> None:
    """Priority dominates: the highest priority key is always tried first."""
    pool, _ = build_pool()
    assert [c.id for c in pool.candidates("p1")] == ["k1", "k2", "k3"]
    pool.pick("p1")  # marks k1 as most recently used
    assert [c.id for c in pool.candidates("p1")] == ["k1", "k2", "k3"]


def test_equal_priority_keys_rotate_least_recently_used_first() -> None:
    pool, _ = build_pool(keys=(("k1", 100), ("k2", 100), ("k3", 100)))
    pool.pick("p1")  # k1 becomes the most recently used
    assert [c.id for c in pool.candidates("p1")] == ["k2", "k3", "k1"]


def test_round_robin_rotation_cycles() -> None:
    pool, _ = build_pool(rotation="round_robin", keys=(("k1", 100), ("k2", 100), ("k3", 100)))
    first = [c.id for c in pool.candidates("p1")]
    second = [c.id for c in pool.candidates("p1")]
    assert first != second
    assert set(first) == {"k1", "k2", "k3"}


def test_weighted_rotation_is_seeded_and_covers_every_key() -> None:
    pool, _ = build_pool(rotation="weighted", keys=(("k1", 100), ("k2", 100)))
    pool._rotation.rng = random.Random(42)  # deterministic ordering for the assertion
    ordered = [c.id for c in pool.candidates("p1")]
    assert sorted(ordered) == ["k1", "k2"]


def test_least_failures_rotation_prefers_healthy_keys() -> None:
    rotation = LeastFailuresRotation()
    pool, _ = build_pool()
    pool.get("k1").failure_count = 5
    pool.get("k1").consecutive_failures = 5
    assert next(c.id for c in rotation.order(pool.for_provider("p1"))) == "k2"


def test_fastest_rotation_prefers_observed_latency() -> None:
    rotation = FastestRotation()
    pool, _ = build_pool()
    pool.get("k3").latency_ema_ms = 12.0
    pool.get("k1").latency_ema_ms = 300.0
    assert next(c.id for c in rotation.order(pool.for_provider("p1"))) == "k3"


def test_rotation_registry_and_defaults() -> None:
    assert "priority" in available_rotations()
    assert "round_robin" in available_rotations()
    assert isinstance(get_rotation(None), PriorityRotation)
    assert isinstance(get_rotation("nonexistent"), PriorityRotation)
    assert isinstance(get_rotation("round_robin"), RoundRobinRotation)
    assert isinstance(get_rotation("weighted"), WeightedRotation)


def test_pick_marks_usage_and_in_flight() -> None:
    pool, _ = build_pool()
    credential = pool.pick("p1")
    assert credential is not None
    assert credential.in_flight == 1
    assert credential.last_used_at is not None
    pool.release(credential.id)
    assert credential.in_flight == 0


def test_pick_respects_exclusions() -> None:
    pool, _ = build_pool()
    credential = pool.pick("p1", exclude=["k1"])
    assert credential is not None and credential.id == "k2"


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #
def test_client_errors_never_count_as_credential_failures() -> None:
    """A malformed request is the caller's fault: no counter, no status change.

    Otherwise a stream of 400s would slowly "damage" every key in `success_rate`,
    reorder the `least_failures` rotation and inflate the reported error rate.
    """
    pool, _ = build_pool(rotation="least_failures")
    credential = pool.get("k1")
    pool.report_failure("k1", failure(400))

    assert credential.failure_count == 0
    assert credential.consecutive_failures == 0
    assert credential.status is CredentialStatus.HEALTHY
    assert credential.cooldown_until is None
    # The observation is still recorded for the operator.
    assert credential.last_error_type == "invalid_request_error"
    # 1.0 == "no failure evidence"; a counted failure would have dragged it to 0.0.
    assert credential.success_rate() == 1.0
    assert [c.id for c in pool.candidates("p1")] == ["k1", "k2", "k3"]


def test_429_moves_the_credential_to_cooldown() -> None:
    pool, _ = build_pool()
    transition = pool.report_failure("k1", failure(429))
    assert transition is not None
    assert transition.previous is CredentialStatus.HEALTHY
    assert transition.current is CredentialStatus.COOLDOWN
    credential = pool.get("k1")
    assert credential.cooldown_remaining() > 0
    assert credential.rate_limit_count == 1
    assert [c.id for c in pool.candidates("p1")] == ["k2", "k3"]


def test_cooldown_expires_and_the_key_recovers() -> None:
    pool, _ = build_pool()
    credential = pool.get("k1")
    pool.report_failure("k1", failure(429))
    credential.cooldown_until = time.time() - 1  # simulate elapsed cooldown
    assert [c.id for c in pool.candidates("p1")] == ["k1", "k2", "k3"]
    assert credential.status is CredentialStatus.HEALTHY


def test_repeated_rate_limits_grow_the_cooldown() -> None:
    policy = CooldownPolicy(rate_limit_base=60, rate_limit_multiplier=2, rate_limit_max=600, jitter=0)
    tracker = CredentialHealthTracker(policy)
    pool, _ = build_pool()
    credential = pool.get("k1")
    first = tracker.on_failure(credential, failure(429))
    second = tracker.on_failure(credential, failure(429))
    assert first.cooldown_until is not None and second.cooldown_until is not None
    assert credential.consecutive_rate_limits == 2
    # Second cooldown is twice as long (minus jitter, which is disabled).
    assert (second.cooldown_until - first.cooldown_until) == pytest.approx(60, abs=1)


def test_served_cooldown_resets_the_rate_limit_ladder() -> None:
    """Cooldown expiry always restarts the ladder at the base rung.

    Regression: the ladder used to survive short rests, so one busy burst
    pinned every key at the 900s ceiling - each expiry re-throttled into an
    even longer cooldown and only /admin/cooldowns/clear brought the key back.
    The punishment is the cooldown itself; once served, history is wiped.
    Sustained throttling still backs off: the re-throttle happens *inside* the
    decay window, so the very next failure climbs the ladder again.
    """
    policy = CooldownPolicy(rate_limit_base=60, rate_limit_max=600, rate_limit_decay=1200, jitter=0)
    tracker = CredentialHealthTracker(policy)
    pool, _ = build_pool()
    credential = pool.get("k1")
    tracker.on_failure(credential, failure(429), now=1000.0)
    tracker.on_failure(credential, failure(429), now=1001.0)
    assert credential.consecutive_rate_limits == 2
    # Second cooldown (120s) elapses; the key has served its sentence.
    tracker.refresh(credential, now=1121.0)
    assert credential.status is CredentialStatus.HEALTHY
    assert credential.consecutive_rate_limits == 0  # fresh ladder
    # Sustained throttling: the next 429 is within the decay window of the
    # last one, so the ladder climbs again from rung 1 -> 2 -> ...
    tracker.on_failure(credential, failure(429), now=1121.0)
    assert credential.consecutive_rate_limits == 1
    tracker.on_failure(credential, failure(429), now=1122.0)
    assert credential.consecutive_rate_limits == 2


def test_stale_rate_limit_history_decays_away() -> None:
    """A 429 arriving long after the previous one is a new burst, not a
    continuation - the ladder must not accumulate across quiet hours."""
    policy = CooldownPolicy(rate_limit_base=60, rate_limit_max=600, rate_limit_decay=1200, jitter=0)
    tracker = CredentialHealthTracker(policy)
    pool, _ = build_pool()
    credential = pool.get("k1")
    for i in range(4):
        tracker.on_failure(credential, failure(429), now=1000.0 + i)
    assert credential.consecutive_rate_limits == 4
    # Hours later (beyond the decay window) a single 429 hits. The old burst
    # is stale history; the ladder restarts at the base instead of the ceiling.
    tracker.on_failure(credential, failure(429), now=1000.0 + 5000.0)
    assert credential.consecutive_rate_limits == 1
    assert credential.cooldown_until is not None
    assert credential.cooldown_until - 6000.0 == pytest.approx(60, abs=1)


def test_full_max_cooldown_rest_resets_the_rate_limit_ladder() -> None:
    """Once a key has quietly rested through a full max cooldown, the history
    is stale: the user's wait must buy a fresh ladder, not a higher one.
    """
    policy = CooldownPolicy(rate_limit_base=60, rate_limit_max=600, rate_limit_decay=1200, jitter=0)
    tracker = CredentialHealthTracker(policy)
    pool, _ = build_pool()
    credential = pool.get("k1")
    # Hammer the ladder up: several 429s in a row.
    for i in range(4):
        tracker.on_failure(credential, failure(429), now=1000.0 + i)
    assert credential.consecutive_rate_limits == 4
    # Cooldown expires long after the last 429 (quiet for >= rate_limit_max).
    tracker.refresh(credential, now=1000.0 + 700.0)
    assert credential.status is CredentialStatus.HEALTHY
    assert credential.consecutive_rate_limits == 0  # fresh ladder
    # The next 429 starts over at the base cooldown, not at the ceiling.
    tracker.on_failure(credential, failure(429), now=1700.0)
    assert credential.consecutive_rate_limits == 1
    assert credential.cooldown_until is not None
    assert credential.cooldown_until - 1700.0 == pytest.approx(60, abs=1)


def test_401_marks_unhealthy_without_a_cooldown() -> None:
    pool, _ = build_pool()
    transition = pool.report_failure("k1", failure(401))
    assert transition is not None
    assert transition.current is CredentialStatus.UNHEALTHY
    credential = pool.get("k1")
    assert credential.cooldown_until is None
    assert "401" in (credential.disabled_reason or "")
    assert "k1" not in [c.id for c in pool.candidates("p1")]


def test_403_records_a_permission_problem() -> None:
    pool, _ = build_pool()
    pool.report_failure("k1", failure(403))
    assert pool.get("k1").status is CredentialStatus.UNHEALTHY
    assert "无权限" in (pool.get("k1").disabled_reason or "")


def test_400_does_not_penalise_the_credential() -> None:
    pool, _ = build_pool()
    transition = pool.report_failure("k1", failure(400))
    assert transition is not None
    assert transition.current is CredentialStatus.HEALTHY
    assert transition.changed is False
    assert pool.get("k1").cooldown_until is None
    assert len(pool.candidates("p1")) == 3


def test_413_does_not_penalise_the_credential() -> None:
    pool, _ = build_pool()
    pool.report_failure("k1", failure(413))
    assert pool.get("k1").status is CredentialStatus.HEALTHY


def test_repeated_5xx_parks_the_key_after_the_threshold() -> None:
    pool, _ = build_pool()
    pool.get("k1").max_consecutive_failures = 3
    for _ in range(2):
        pool.report_failure("k1", failure(500))
    assert pool.get("k1").status is CredentialStatus.HEALTHY
    pool.report_failure("k1", failure(500))
    assert pool.get("k1").status is CredentialStatus.COOLDOWN


def test_success_resets_counters_and_recovers_the_key() -> None:
    pool, _ = build_pool()
    pool.report_failure("k1", failure(401))
    pool.report_success("k1", latency_ms=250.0)
    credential = pool.get("k1")
    assert credential.status is CredentialStatus.HEALTHY
    assert credential.consecutive_failures == 0
    assert credential.success_count == 1
    assert credential.latency_ema_ms == 250.0


def test_latency_ema_is_smoothed() -> None:
    pool, _ = build_pool()
    pool.report_success("k1", latency_ms=100.0)
    pool.report_success("k1", latency_ms=200.0)
    assert 100.0 < pool.get("k1").latency_ema_ms < 200.0


# --------------------------------------------------------------------------- #
# Administration
# --------------------------------------------------------------------------- #
def test_disable_and_enable_round_trip() -> None:
    pool, _ = build_pool()
    pool.disable("k1", "rotating")
    assert pool.get("k1").status is CredentialStatus.DISABLED
    assert "k1" not in [c.id for c in pool.candidates("p1")]
    pool.enable("k1")
    assert pool.get("k1").status is CredentialStatus.HEALTHY
    assert "k1" in [c.id for c in pool.candidates("p1")]


def test_invalidate_cooldowns_clears_every_parked_key() -> None:
    pool, _ = build_pool()
    pool.report_failure("k1", failure(429))
    pool.report_failure("k2", failure(429))
    assert pool.invalidate_cooldowns("p1") == 2
    assert len(pool.candidates("p1")) == 3


def test_health_check_result_updates_state() -> None:
    pool, _ = build_pool()
    pool.report_failure("k1", failure(401))
    assert pool.get("k1").status is CredentialStatus.UNHEALTHY
    pool.apply_health_check("k1", healthy=True)
    assert pool.get("k1").status is CredentialStatus.HEALTHY
    pool.apply_health_check("k1", healthy=False, detail="bad key")
    assert pool.get("k1").status is CredentialStatus.UNHEALTHY


# --------------------------------------------------------------------------- #
# Configuration / secrets
# --------------------------------------------------------------------------- #
def test_missing_environment_variable_disables_the_credential() -> None:
    pool, _ = build_pool(keys=(("k1", 100),), env_prefix="ZKAI_ABSENT_KEY")
    credential = pool.get("k1")
    assert credential.status is CredentialStatus.DISABLED
    assert "ZKAI_ABSENT_KEY_K1" in (credential.disabled_reason or "")
    assert pool.candidates("p1") == []


def test_environment_variable_is_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZKAI_POOL_K1", "sk-from-env")
    pool, _ = build_pool(keys=(("k1", 100),), env_prefix="ZKAI_POOL")
    credential = pool.get("k1")
    assert credential.is_configured()
    assert credential.secret == "sk-from-env"
    assert credential.secret_source == "env"


def test_keyless_provider_gets_a_synthetic_credential() -> None:
    provider = ProviderConfig(
        id="ollama", type=ProviderType.OLLAMA, base_url="http://127.0.0.1:11434", credentials=[]
    )
    pool = CredentialPool()
    pool.register_provider(provider)
    credentials = pool.for_provider("ollama")
    assert len(credentials) == 1
    assert credentials[0].secret is None
    assert credentials[0].is_usable()
    assert pool.pick("ollama") is not None


def test_provider_without_credentials_reports_no_candidates() -> None:
    provider = ProviderConfig(
        id="openai", type=ProviderType.OPENAI, base_url="http://127.0.0.1:9/v1", credentials=[]
    )
    pool = CredentialPool()
    pool.register_provider(provider)
    assert pool.candidates("openai") == []
    assert pool.provider_availability()["openai"] is False


def test_snapshot_never_contains_the_secret() -> None:
    pool, _ = build_pool()
    pool.get("k1").secret = "sk-super-secret-value"
    snapshot = pool.snapshot("p1")
    rendered = str(snapshot)
    assert "sk-super-secret-value" not in rendered
    entry = next(item for item in snapshot if item["id"] == "k1")
    assert entry["configured"] is True
    assert entry["secret_masked"].startswith("sk-s")
    assert "authorization" not in rendered.lower()


def test_stats_aggregate_pool_health() -> None:
    pool, _ = build_pool()
    pool.report_success("k1")
    pool.report_failure("k2", failure(429))
    stats = pool.stats("p1")
    assert stats["total"] == 3
    assert stats["by_status"]["healthy"] == 2
    assert stats["by_status"]["cooldown"] == 1
    assert stats["rate_limits"] == 1
    assert stats["rotation"] == "priority"


def test_describe_selection_explains_the_order() -> None:
    pool, _ = build_pool()
    pool.report_failure("k1", failure(429))
    described = pool.describe_selection("p1")
    assert [item["id"] for item in described] == ["k2", "k3"]
    assert described[0]["selected"] is True


def test_failure_for_unknown_credential_is_ignored() -> None:
    pool, _ = build_pool()
    assert pool.report_failure("does-not-exist", failure(500)) is None
    assert pool.report_success("does-not-exist") is None
    assert pool.enable("does-not-exist") is None
    assert pool.disable("does-not-exist") is None


def test_health_tracker_client_error_is_a_no_op() -> None:
    tracker = CredentialHealthTracker()
    pool, _ = build_pool()
    transition = tracker.on_failure(
        pool.get("k1"),
        ErrorInfo("invalid_request_error", ErrorClass.CLIENT, 400, "bad"),
    )
    assert transition.changed is False
    assert transition.reason == "client-error:invalid_request_error"


def test_make_provider_helper_builds_credentials() -> None:
    provider = make_provider("p9", key_ids=("a", "b"))
    assert [c.id for c in provider.credentials] == ["a", "b"]


# --------------------------------------------------------------------------- #
# Regression: pool blackouts (gateway-side parse errors, quota 429s, probe noise)
# --------------------------------------------------------------------------- #
def test_internal_error_does_not_penalise_the_credential() -> None:
    """A failure inside the gateway must not cool the key that happened to serve it.

    One unparseable streaming frame used to be counted as an upstream failure:
    three requests later every key of every account sat in COOLDOWN while the
    upstreams had been perfectly healthy.
    """
    pool, _ = build_pool()
    info = ErrorInfo("unknown_error", ErrorClass.INTERNAL, 500, "ValidationError: ...")
    transition = pool.report_failure("k1", info)
    credential = pool.get("k1")
    assert transition.changed is False
    assert credential.status is CredentialStatus.HEALTHY
    assert credential.failure_count == 0
    assert credential.consecutive_failures == 0
    # The operator still learns what went wrong:
    assert credential.last_error_type == "unknown_error"


def test_quota_exhaustion_uses_a_flat_long_cooldown() -> None:
    """Plan exhaustion parks a key for hours and must not escalate exponentially."""
    pool, _ = build_pool()
    quota = CLASSIFIER.classify_status(
        429, body={"error": {"message": "token plan entitlement exhausted"}}
    )
    pool.report_failure("k1", quota)
    first = pool.get("k1").cooldown_remaining()
    assert 1500 <= first <= 2100  # quota_cooldown 1800 ± jitter
    pool.report_failure("k1", quota)  # same key, second consecutive quota hit
    second = pool.get("k1").cooldown_remaining()
    assert 1500 <= second <= 2100  # flat, NOT doubled


def test_per_minute_throttle_still_escalates_exponentially() -> None:
    """tpm/rpm style 429s keep the escalating ladder (they reset within a minute)."""
    pool, _ = build_pool()
    tpm = CLASSIFIER.classify_status(
        429, body={"error": {"message": "inference exceeds tpm/rpm limit"}}
    )
    pool.report_failure("k1", tpm)
    first = pool.get("k1").cooldown_remaining()
    assert 45 <= first <= 75  # 60 ± 15% jitter
    pool.report_failure("k1", tpm)
    second = pool.get("k1").cooldown_remaining()
    assert 100 <= second <= 140  # 120 ± 15%
    assert second > first


def test_next_available_in_reports_the_pool_recovery_time() -> None:
    pool, _ = build_pool()
    assert pool.next_available_in("p1") is None  # everything healthy
    for key_id in ("k1", "k2", "k3"):
        pool.report_failure(key_id, failure(429))
    wait = pool.next_available_in("p1")
    assert wait is not None
    assert 0 < wait <= 900
    pool.invalidate_cooldowns("p1")
    assert pool.next_available_in("p1") is None


def test_probe_failure_takes_only_authentication_seriously() -> None:
    """A timeout on the probe endpoint must not park a perfectly good key."""
    pool, _ = build_pool()
    pool.apply_health_check(
        "k1", healthy=False, detail="connect timed out", error_type="timeout"
    )
    assert pool.get("k1").status is CredentialStatus.HEALTHY
    assert pool.get("k1").last_error_type == "timeout"
    # ...while a 401 (or the legacy no-error-type call) still does.
    pool.apply_health_check("k2", healthy=False, detail="revoked")
    assert pool.get("k2").status is CredentialStatus.UNHEALTHY
    pool.apply_health_check("k3", healthy=False, error_type="permission_denied")
    assert pool.get("k3").status is CredentialStatus.UNHEALTHY


# --------------------------------------------------------------------------- #
# Conversation affinity (sticky sessions across failover)
# --------------------------------------------------------------------------- #
def test_affinity_pins_a_session_to_its_last_successful_key() -> None:
    """A pinned conversation is served by the pinned key, not by priority order."""
    pool, _ = build_pool()
    # Priority order alone: k1 first. With a pin on k3 (lowest priority), k3 leads.
    assert [c.id for c in pool.candidates("p1", session_key="s-1")] == ["k1", "k2", "k3"]
    pool.note_affinity("s-1", "k3")
    assert [c.id for c in pool.candidates("p1", session_key="s-1")] == ["k3", "k1", "k2"]
    # A different session is unaffected; the pin is per-conversation.
    assert next(c.id for c in pool.candidates("p1", session_key="s-2")) == "k1"
    # No session key => pure rotation order.
    assert next(c.id for c in pool.candidates("p1")) == "k1"


def test_affinity_falls_back_and_reanchors_on_failure() -> None:
    """A cooling pinned key simply drops out; the pin re-rolls on the next win."""
    pool, _ = build_pool()
    pool.note_affinity("s-1", "k1")
    pool.report_failure("k1", failure(429))  # k1 -> COOLDOWN
    order = [c.id for c in pool.candidates("p1", session_key="s-1")]
    assert order == ["k2", "k3"]  # normal rotation takes over, nothing crashes
    # Re-anchor to the key that actually served the next turn.
    pool.note_affinity("s-1", "k2")
    assert next(c.id for c in pool.candidates("p1", session_key="s-1")) == "k2"


def test_affinity_expiry_and_lru_cap() -> None:
    pool, _ = build_pool()
    pool.note_affinity("s-old", "k2")
    # Simulate a stale binding.
    cred_id, _ = pool._affinity["s-old"]
    pool._affinity["s-old"] = (cred_id, time.time() - 1)
    assert next(c.id for c in pool.candidates("p1", session_key="s-old")) == "k1"
    assert "s-old" not in pool._affinity  # expired entry pruned on touch

    tight = build_pool()[0]
    tight._affinity_max_sessions = 3
    for index in range(5):
        tight.note_affinity(f"s-{index}", "k2")
    assert len(tight._affinity) == 3
    assert "s-0" not in tight._affinity and "s-4" in tight._affinity  # oldest dropped


def test_affinity_can_be_disabled() -> None:
    _pool, provider = build_pool()
    off = CredentialPool(
        rotation="priority", allow_inline_secrets=True, affinity_enabled=False
    )
    off.register_provider(provider)
    off.note_affinity("s-1", "k3")
    assert off._affinity == {}
    assert next(c.id for c in off.candidates("p1", session_key="s-1")) == "k1"


def test_affinity_bindings_are_provider_scoped_and_masked() -> None:
    pool, _ = build_pool()
    pool.note_affinity("s-secret-session", "k2")
    bindings = pool.affinity_bindings_for_provider("p1")
    assert len(bindings) == 1
    assert bindings[0]["credential_id"] == "k2"
    assert "secret" not in bindings[0]["session"]  # only the hash prefix is shown
    assert pool.affinity_bindings_for_provider("nope") == []


# --------------------------------------------------------------------------- #
# UNHEALTHY auto-recovery (auto_recover_after) and pool reconcile on reload
# --------------------------------------------------------------------------- #
def test_unhealthy_key_auto_recovers_after_a_rest() -> None:
    """A 401 may be a transient upstream glitch: park, rest, then one free retry.

    Without this the key stays UNHEALTHY until an operator notices; with a
    genuinely revoked key the next failure just re-parks it.
    """
    pool, _ = build_pool()
    credential = pool.get("k1")
    pool.report_failure("k1", failure(401))
    assert credential.status is CredentialStatus.UNHEALTHY

    # Too soon: still parked.
    assert pool.candidates("p1", session_key=None)[0].id != "k1"
    # Past the rest window: refresh revives it.
    credential.last_error_at = time.time() - (pool.policy.auto_recover_after + 1)
    assert next(c.id for c in pool.candidates("p1")) == "k1"
    assert credential.status is CredentialStatus.HEALTHY
    assert credential.disabled_reason == "auto-recovered"


def test_reconcile_drops_credentials_removed_from_config() -> None:
    """Hot reload must mirror deletions, not only additions."""
    pool, provider = build_pool()
    pool.note_affinity("s-1", "k2")
    assert len(pool.all()) == 3

    # Provider reload with k2 gone.
    provider.credentials = [c for c in provider.credentials if c.id != "k2"]
    removed = pool.reconcile({"p1": provider})
    assert removed == 1
    assert pool.get("k2") is None
    assert "k2" not in [c.id for c in pool.candidates("p1")]

    # Whole provider removed.
    removed = pool.reconcile({})
    assert removed == 2
    assert pool.all() == []
