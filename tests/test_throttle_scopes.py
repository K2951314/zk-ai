"""Throttle classification and how far a 429's cooldown reaches.

Three distinct bugs are pinned here, all of them measured on this gateway:

1. A 429 used to mean "rotate to the next key of the *same* deployment",
   unconditionally. SenseNova's tpm/rpm bucket is shared across that provider's
   accounts, so six of the seven attempts were guaranteed 429s - measured 19.7%
   of all attempts spinning. A shared-bucket 429 must fail over instead.
2. The cooldown only ever parked the one reported key, so a per-account quota
   left the twin key of the same account hammering the dead bucket.
3. ``rpm``/``rps`` limits really are per-key (NVIDIA's 40 rpm), so those must
   keep rotating - over-correcting to provider scope would abandon healthy keys.

``ThrottleKind`` classification itself is tested in ``tests/test_errors.py``;
this file is about what the scheduler and the pool *do* with it.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from app.core.errors import GatewayTimeoutError
from app.credentials.cooldown import CooldownPolicy
from app.retry.classifier import ThrottleKind
from app.routing.scheduler import Scheduler
from tests.conftest import (
    Behavior,
    FakeAdapter,
    Harness,
    build_harness,
    make_alias,
    make_config,
    make_model,
    make_provider,
)

#: SenseNova's combined limit message: it names tpm *and* rpm. The classifier
#: deliberately resolves it to THROUGHPUT (see ``_THROUGHPUT_RE``).
TPM_429 = Behavior(status=429, error_message="inference exceeds tpm/rpm limit")
RPM_429 = Behavior(status=429, error_message="Too Many Requests")
QUOTA_429 = Behavior(status=429, error_message="token plan entitlement exhausted")


async def _two_provider_harness(**policy_overrides: object) -> tuple[Harness, list[FakeAdapter]]:
    """One alias spanning two providers, so failover is observable as a call."""
    prov_a = make_provider("prov-a", key_ids=("a-1", "a-2"))
    prov_b = make_provider("prov-b", key_ids=("b-1",))
    # 真实配置里每把 Key 都带 account-* 归属标签（providers.yaml），而 account
    # 范围靠它识别「共享同一个桶」。没有标签时 account 会降级成 credential
    # （CredentialPool.effective_throttle_scope），那就测不到范围本身了。
    for credential in prov_a.credentials:
        credential.tags = ["account-a"]
    models = [
        make_model("model-a", provider_id="prov-a"),
        make_model("model-b", provider_id="prov-b"),
    ]
    config = make_config(
        providers=[prov_a, prov_b],
        models=models,
        aliases=[make_alias("zk-auto", ["model-a", "model-b"])],
    )
    adapters = {"prov-a": FakeAdapter(prov_a), "prov-b": FakeAdapter(prov_b)}
    harness = await build_harness(config, adapters=adapters)
    for key, value in policy_overrides.items():
        setattr(harness.pool.policy, key, value)
    return harness, list(adapters.values())


def _providers_of(harness: Harness) -> list[str]:
    return [call["provider"] for call in harness.all_calls()]


def _stream_text(events: list) -> str:
    """Concatenate the content deltas of a scheduler stream run."""
    chunks = [e.chunk for e in events if e.type == "chunk" and e.chunk is not None]
    return "".join(
        (chunk.choices[0].delta.content or "")
        for chunk in chunks
        if chunk.choices
    )


# --------------------------------------------------------------------------- #
# Default policy
# --------------------------------------------------------------------------- #

def test_default_scopes_match_the_measured_behaviour() -> None:
    """2026-09-28 实测修正后的默认值。

    ``provider`` 曾是 quota/throughput 的默认，依据「商汤 tpm 账号间共享」。
    那个依据被同一时刻的观测推翻：账号 04/05 正以并发 16 稳定烧穿，而
    03/06/07/08/09 全部 429——桶按账号分。判成 provider 的代价实测很重
    （见 test_throttle_scopes.py 顶部 docstring 与 cooldown.py 的长注释），
    所以默认退回 ``account``：只停靠能证明共享桶的 Key。
    """
    policy = CooldownPolicy()
    # Buckets are per-account -> park only keys that share the account tag.
    assert policy.throttle_scope("quota") == "account"
    assert policy.throttle_scope("throughput") == "account"
    # Per-key buckets -> keep rotating (NVIDIA 40 rpm).
    assert policy.throttle_scope("frequency") == "credential"
    # Unknown 429 stays on the old, conservative behaviour.
    assert policy.throttle_scope("unknown") == "credential"
    # Garbage in, safe default out.
    assert policy.throttle_scope("nonsense") == "credential"


def test_throttle_scopes_are_a_partial_override_not_a_replacement() -> None:
    """Supplying one key must not silently reset the other three."""
    policy = CooldownPolicy.from_mapping({"throttle_scopes": {"frequency": "provider"}})
    assert policy.throttle_scope("frequency") == "provider"
    # 其余三个保持各自默认，没有被这一次覆盖重置掉
    assert policy.throttle_scope("quota") == "account"
    assert policy.throttle_scope("throughput") == "account"
    assert policy.throttle_scope("unknown") == "credential"


def test_invalid_scope_falls_back_to_credential() -> None:
    policy = CooldownPolicy.from_mapping({"throttle_scopes": {"quota": "galaxy"}})
    assert policy.throttle_scope("quota") == "credential"


def test_throttle_kind_values_are_stable_strings() -> None:
    """The policy table is keyed by these strings; they are config surface."""
    assert ThrottleKind.QUOTA.value == "quota"
    assert ThrottleKind.THROUGHPUT.value == "throughput"
    assert ThrottleKind.FREQUENCY.value == "frequency"
    assert ThrottleKind.UNKNOWN.value == "unknown"


# --------------------------------------------------------------------------- #
# Scheduler: shared bucket -> fail over
# --------------------------------------------------------------------------- #

async def test_tpm_429_fails_over_instead_of_sweeping_sibling_keys() -> None:
    """The core fix: a shared-bucket 429 must not burn the sibling keys."""
    harness, _ = await _two_provider_harness()
    try:
        # prov-a has two keys; a rotating scheduler would also try a-2.
        harness.adapters["prov-a"].queue(
            TPM_429, TPM_429, TPM_429, TPM_429,
            Behavior(text="never used"),
        )
        harness.adapters["prov-b"].queue(Behavior(text="served by b"))
        response = await harness.request("zk-auto")
        assert response.text() == "served by b"
        # Exactly one attempt on prov-a, then straight to prov-b.
        assert _providers_of(harness) == ["prov-a", "prov-b"]
        # And the sibling key was parked, not merely skipped.
        assert harness.pool.get("a-2").status.value == "cooldown"
    finally:
        await harness.container.shutdown()


async def test_quota_429_parks_the_account_and_fails_over() -> None:
    """同 account-* 标签的兄弟 Key 一起停靠，然后直接换部署。"""
    harness, _ = await _two_provider_harness()
    try:
        harness.adapters["prov-a"].queue(QUOTA_429, Behavior(text="never used"))
        harness.adapters["prov-b"].queue(Behavior(text="served by b"))
        response = await harness.request("zk-auto")
        assert response.text() == "served by b"
        assert _providers_of(harness) == ["prov-a", "prov-b"]
        # The key that actually reported the 429 keeps the *specific* reason -
        # "套餐额度耗尽长休" tells the operator far more than "同供应商限流" does.
        assert harness.pool.get("a-1").status.value == "cooldown"
        assert harness.pool.get("a-1").disabled_reason == "套餐额度耗尽长休"
        # The sibling never spoke to the upstream, so it gets the generic one.
        assert harness.pool.get("a-2").status.value == "cooldown"
        assert harness.pool.get("a-2").disabled_reason == "同账号限流冷却中"
    finally:
        await harness.container.shutdown()


async def test_frequency_429_still_rotates_keys() -> None:
    """rpm/rps are counted per key - over-correcting would abandon healthy keys."""
    harness, _ = await _two_provider_harness()
    try:
        harness.adapters["prov-a"].queue(RPM_429, Behavior(text="served by a-2"))
        harness.adapters["prov-b"].queue(Behavior(text="served by b"))
        response = await harness.request("zk-auto")
        assert response.text() == "served by a-2"
        # Both attempts stayed on prov-a: the sibling key really could serve it.
        assert _providers_of(harness) == ["prov-a", "prov-a"]
        assert harness.pool.get("a-2").status.value == "healthy"
    finally:
        await harness.container.shutdown()


async def test_unknown_429_keeps_the_legacy_per_key_behaviour() -> None:
    """No recognisable marker: the pre-existing contract must not change."""
    harness, _ = await _two_provider_harness()
    try:
        harness.adapters["prov-a"].queue(
            Behavior(status=429), Behavior(text="served by a-2"),
        )
        harness.adapters["prov-b"].queue(Behavior(text="served by b"))
        response = await harness.request("zk-auto")
        assert response.text() == "served by a-2"
        assert _providers_of(harness) == ["prov-a", "prov-a"]
    finally:
        await harness.container.shutdown()


async def test_credential_scope_config_makes_tpm_rotate_again() -> None:
    """The scope is a policy knob, not a hardcoded guess."""
    harness, _ = await _two_provider_harness(throttle_scopes={"throughput": "credential"})
    try:
        harness.adapters["prov-a"].queue(TPM_429, Behavior(text="served by a-2"))
        harness.adapters["prov-b"].queue(Behavior(text="served by b"))
        response = await harness.request("zk-auto")
        assert response.text() == "served by a-2"
        assert _providers_of(harness) == ["prov-a", "prov-a"]
        assert harness.pool.get("a-2").status.value == "healthy"
    finally:
        await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# Pool: sibling keys parked for the same stretch
# --------------------------------------------------------------------------- #

async def test_siblings_recover_with_the_leader_not_before_it() -> None:
    """Parking is pointless if the twin wakes up first and re-feeds the bucket."""
    harness, _ = await _two_provider_harness()
    try:
        harness.adapters["prov-a"].queue(TPM_429)
        harness.adapters["prov-b"].queue(Behavior(text="served by b"))
        await harness.request("zk-auto")
        leader = harness.pool.get("a-1")
        sibling = harness.pool.get("a-2")
        assert leader.cooldown_until is not None
        assert sibling.cooldown_until == leader.cooldown_until
    finally:
        await harness.container.shutdown()


async def test_untagged_keys_do_not_spread_the_cooldown() -> None:
    """没有 account-* 标签就无权证明共享桶 → 不扩散，兄弟 Key 继续可用。

    ``CredentialPool.effective_throttle_scope`` 的降级规则：猜错方向（把健康的
    兄弟 Key 一起停靠）比多试一次贵得多，所以宁可不扩散。
    """
    prov = make_provider("prov-a", key_ids=("a-1", "a-2"))       # 刻意不打标签
    other = make_provider("prov-b", key_ids=("b-1",))
    for credential in prov.credentials:
        credential.tags = []
    models = [
        make_model("model-a", provider_id="prov-a"),
        make_model("model-b", provider_id="prov-b"),
    ]
    config = make_config(
        providers=[prov, other],
        models=models,
        aliases=[make_alias("zk-auto", ["model-a", "model-b"])],
    )
    harness = await build_harness(
        config, adapters={"prov-a": FakeAdapter(prov), "prov-b": FakeAdapter(other)}
    )
    try:
        harness.adapters["prov-a"].queue(QUOTA_429, Behavior(text="served by a-2"))
        harness.adapters["prov-b"].queue(Behavior(text="served by b"))
        response = await harness.request("zk-auto")
        assert response.text() == "served by a-2"
        assert harness.pool.get("a-1").status.value == "cooldown"
        assert harness.pool.get("a-2").status.value == "healthy"
    finally:
        await harness.container.shutdown()


async def test_tagged_accounts_are_parked_together() -> None:
    """Same ``account-*`` tag = same quota bucket, so both keys must rest."""
    prov = make_provider("prov-a", key_ids=("a-1", "a-2"))
    for credential in prov.credentials:
        credential.tags = ["account-x"]
    other = make_provider("prov-b", key_ids=("b-1",))
    models = [
        make_model("model-a", provider_id="prov-a"),
        make_model("model-b", provider_id="prov-b"),
    ]
    config = make_config(
        providers=[prov, other],
        models=models,
        aliases=[make_alias("zk-auto", ["model-a", "model-b"])],
    )
    harness = await build_harness(
        config, adapters={"prov-a": FakeAdapter(prov), "prov-b": FakeAdapter(other)}
    )
    harness.pool.policy.throttle_scopes["quota"] = "account"
    try:
        harness.adapters["prov-a"].queue(QUOTA_429, Behavior(text="never used"))
        harness.adapters["prov-b"].queue(Behavior(text="served by b"))
        response = await harness.request("zk-auto")
        assert response.text() == "served by b"
        assert harness.pool.get("a-1").disabled_reason == "套餐额度耗尽长休"
        assert harness.pool.get("a-2").status.value == "cooldown"
        assert harness.pool.get("a-2").disabled_reason == "同账号限流冷却中"
    finally:
        await harness.container.shutdown()


async def test_disabled_keys_keep_their_audit_trail() -> None:
    """Never overwrite DISABLED/UNHEALTHY - the operator's reason is evidence."""
    from app.core.errors import AllAttemptsFailedError

    prov = make_provider("prov-a", key_ids=("a-1", "a-2"))
    config = make_config(
        providers=[prov],
        models=[make_model("model-a", provider_id="prov-a")],
        aliases=[make_alias("zk-auto", ["model-a"])],
    )
    harness = await build_harness(config, adapters={"prov-a": FakeAdapter(prov)})
    try:
        harness.pool.disable("a-2", "操作员手动禁用")
        harness.adapters["prov-a"].queue(TPM_429, Behavior(text="never used"))
        # model-a is the only deployment, so the shared-bucket failover has
        # nowhere to go: the request is exhausted, which is the honest outcome.
        with pytest.raises(AllAttemptsFailedError):
            await harness.request("zk-auto")
        assert harness.pool.get("a-2").status.value == "disabled"
        assert harness.pool.get("a-2").disabled_reason == "操作员手动禁用"
    finally:
        await harness.container.shutdown()


async def test_a_five_hundred_does_not_spread() -> None:
    """A 500 is TRANSIENT, not THROTTLE: no provider-wide sweep."""
    harness, _ = await _two_provider_harness()
    try:
        harness.adapters["prov-a"].queue(
            Behavior(status=500), Behavior(text="served by a-2"),
        )
        response = await harness.request("zk-auto")
        assert response.text() == "served by a-2"
        assert harness.pool.get("a-2").status.value == "healthy"
    finally:
        await harness.container.shutdown()


async def test_stream_also_fails_over_on_shared_bucket_throttle() -> None:
    """The streaming path must not keep the per-key-only behaviour."""
    harness, _ = await _two_provider_harness()
    try:
        harness.adapters["prov-a"].queue(TPM_429)
        harness.adapters["prov-b"].queue(Behavior(chunks=2))
        events = await harness.request("zk-auto", stream=True)
        # Streamed content comes from the *chunks*, not ``Behavior.text``.
        assert _stream_text(events).strip()
        assert _providers_of(harness) == ["prov-a", "prov-b"]
    finally:
        await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# 墙钟预算：次数闸看不见「每次都刚好不超、合计十几分钟」（2026-09-28 实测）
# --------------------------------------------------------------------------- #

async def test_wall_clock_budget_stops_the_grind() -> None:
    """max_total_attempts 数的是次数，20 × 60s = 20 分钟，客户端一个字节都收不到。

    实测场景：商汤 429（与消耗器共抢同一把 Key）→ 逐个部署故障转移到 nvidia →
    glm-5.3 连续 3 次 60 秒超时。每次尝试单独看都没超时，所以次数闸认为还有预算。
    """
    harness, _ = await _two_provider_harness()
    harness.scheduler.max_request_seconds = 0.05     # 50ms，必然触发
    try:
        # 让 prov-a 一直超时，不给它成功的机会
        async def _hang(self, request, ctx):
            await asyncio.sleep(5)
            raise AssertionError("should not get here")
        FakeAdapter.chat, _orig = _hang, FakeAdapter.chat
        try:
            with pytest.raises(GatewayTimeoutError):
                await harness.request("zk-auto")
        finally:
            FakeAdapter.chat = _orig
    finally:
        await harness.container.shutdown()


async def test_wall_clock_zero_disables_the_budget() -> None:
    """0 = 关闭，行为与加预算之前完全一致（不破坏既有契约）。"""
    harness, _ = await _two_provider_harness()
    harness.scheduler.max_request_seconds = 0
    try:
        harness.adapters["prov-a"].queue(Behavior(status=500), Behavior(text="ok"))
        response = await harness.request("zk-auto")
        assert response.text() == "ok"
    finally:
        await harness.container.shutdown()


def test_default_budget_matches_the_slowest_provider() -> None:
    """默认值必须 >= 现役最大上游超时（商汤 300s），否则会误杀慢生成。"""
    sig = inspect.signature(Scheduler.__init__)
    assert sig.parameters["max_request_seconds"].default == 300.0


# --------------------------------------------------------------------------- #
# 自动隔离的端到端：连续失败后，请求不再重试那个坏部署（2026-09-29）
# --------------------------------------------------------------------------- #

async def test_repeated_failures_take_the_deployment_out_of_the_plan() -> None:
    """端到端：prov-a 连续超时 → 第 3 次之后请求直接走 prov-b，不再陪它等。

    这是运营者要的效果：「总是检测失败的模型应该自动屏蔽，而不是每次都重新检测」。
    单测状态机不够——必须证明**路由计划里真的不再包含它**，否则隔离只是记账。
    """
    import httpx

    from app.routing.scheduler import QUARANTINE_THRESHOLD

    harness, _adapters = await _two_provider_harness()
    try:
        # prov-a 每次都在建立流时超时；prov-b 正常
        harness.adapters["prov-a"].queue(*[
            Behavior(error=httpx.ReadTimeout("hang")) for _ in range(QUARANTINE_THRESHOLD + 2)
        ])
        harness.adapters["prov-b"].queue(*[Behavior(text="from-b") for _ in range(4)])

        for i in range(QUARANTINE_THRESHOLD):
            response = await harness.request("zk-auto")
            assert response.text() == "from-b", f"第 {i+1} 次就故障转移失败"

        quarantined = harness.scheduler.quarantined_deployments()
        # 部署 id 形如 model-a-dep（见 conftest.make_model），不是 prov-a
        assert "model-a-dep" in quarantined, (
            f"连续 {QUARANTINE_THRESHOLD} 次超时后应隔离 model-a-dep，实际 {quarantined}"
        )
        assert quarantined["model-a-dep"]["quarantined"] is True

        # 关键：隔离后再请求，prov-a 不应被再试一次
        before = len(harness.adapters["prov-a"].calls)
        response = await harness.request("zk-auto")
        assert response.text() == "from-b"
        assert len(harness.adapters["prov-a"].calls) == before, (
            "隔离后仍在重试 prov-a——「每次都重新检测」的问题没解决"
        )
    finally:
        await harness.container.shutdown()


async def test_rate_limits_alone_never_quarantine_a_deployment() -> None:
    """端到端：只有 429 时不该隔离——那是偶发，且属凭据层处置。"""
    harness, _ = await _two_provider_harness()
    try:
        harness.adapters["prov-a"].queue(*[
            Behavior(status=429, error_message="tpm exhausted") for _ in range(6)
        ])
        harness.adapters["prov-b"].queue(*[Behavior(text="from-b") for _ in range(6)])
        for _ in range(5):
            await harness.request("zk-auto")
        assert harness.scheduler.quarantined_deployments() == {}, (
            "限流被当成「部署坏了」——这正是要避免的偶发误伤"
        )
    finally:
        await harness.container.shutdown()
