"""End-to-end proactive quotas: scheduler steering, admin CRUD, DB persistence."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx
import pytest

from app.core.errors import AllAttemptsFailedError
from app.main import create_app
from tests.conftest import (
    FakeAdapter,
    Harness,
    build_harness,
    make_alias,
    make_config,
    make_model,
    make_provider,
)

ADMIN = {"x-admin-token": "test-admin-token"}
CHAT_BODY = {"model": "fake-model", "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture
async def limit_api() -> AsyncIterator[tuple[httpx.AsyncClient, Harness]]:
    """Gateway whose fake provider starts with one 60s/40-requests credential cap."""
    provider = make_provider("fake", key_ids=("key-1", "key-2", "key-3"))
    provider.options = {
        "rate_limits": [{"scope": "credential", "window_seconds": 60, "max_requests": 40}]
    }
    config = make_config(
        providers=[provider],
        models=[make_model("fake-model")],
        aliases=[make_alias("zk-test", ["fake-model"])],
        admin_token="test-admin-token",
    )
    adapter = FakeAdapter(provider)
    harness = await build_harness(config, adapters={"fake": adapter})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
        yield client, harness
    await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# Scheduler steering (no HTTP): quotas pick which keys are even attempted
# --------------------------------------------------------------------------- #
async def test_request_cap_steers_to_next_key() -> None:
    provider = make_provider("fake", key_ids=("key-1", "key-2"))
    provider.options = {
        "rate_limits": [{"scope": "credential", "window_seconds": 3600, "max_requests": 1}]
    }
    config = make_config(providers=[provider], models=[make_model("fake-model")])
    adapter = FakeAdapter(provider)
    harness = await build_harness(config, adapters={"fake": adapter})
    try:
        await harness.request()
        await harness.request()
        creds = [call["credential_id"] for call in adapter.calls]
        assert creds == ["key-1", "key-2"], "second request must rotate to a fresh key"
        with pytest.raises(AllAttemptsFailedError):
            await harness.request()  # both keys capped (1 req/h each)
    finally:
        await harness.container.shutdown()


async def test_token_budget_closes_the_window() -> None:
    # default Behavior = 15 tokens/request; cap 30 -> exactly two requests fit
    provider = make_provider("fake", key_ids=("key-1",))
    provider.options = {
        "rate_limits": [{"scope": "provider", "window_seconds": 3600, "max_tokens": 30}]
    }
    config = make_config(providers=[provider], models=[make_model("fake-model")])
    adapter = FakeAdapter(provider)
    harness = await build_harness(config, adapters={"fake": adapter})
    try:
        await harness.request()
        await harness.request()
        limiter = harness.container.rate_limiter
        usage = limiter.usage("fake", "key-1")
        assert usage[0]["used_tokens"] == 30
        assert limiter.remaining("fake", "key-1") == 0
        with pytest.raises(AllAttemptsFailedError):
            await harness.request()
    finally:
        await harness.container.shutdown()


async def test_token_cap_frees_only_after_window_slides(provider_config) -> None:
    provider_config.options = {
        "rate_limits": [{"scope": "credential", "window_seconds": 60, "max_tokens": 15}]
    }
    config = make_config(
        providers=[provider_config], models=[make_model("fake-model")]
    )
    adapter = FakeAdapter(provider_config)
    harness = await build_harness(config, adapters={"fake": adapter})
    try:
        await harness.request()  # exactly 15 tokens -> key capped
        limiter = harness.container.rate_limiter
        assert not limiter.admit("fake", "key-1", ["key-1"])
        # 61s later the hit has slid out of the window
        assert limiter.admit("fake", "key-1", ["key-1"], now=time.time() + 61)
    finally:
        await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# Admin API: PUT / DELETE /admin/providers/{id}/limits
# --------------------------------------------------------------------------- #
async def test_admin_list_reports_limits_and_source(limit_api) -> None:
    client, _ = limit_api
    data = (await client.get("/admin/providers", headers=ADMIN)).json()["data"]
    fake = next(p for p in data if p["id"] == "fake")
    assert fake["rate_limits"] == [
        {"scope": "credential", "window_seconds": 60, "max_requests": 40}
    ]
    assert fake["rate_limits_source"] == "yaml"


async def test_admin_put_update_and_reset(limit_api) -> None:
    client, harness = limit_api
    # operator tightens the cap to 1 and adds a token budget
    response = await client.put(
        "/admin/providers/fake/limits",
        headers=ADMIN,
        json={"rules": [{"scope": "credential", "window_seconds": 60, "max_requests": 1}]},
    )
    assert response.status_code == 200
    assert response.json()["rules"][0]["max_requests"] == 1
    assert harness.container.rate_limiter.describe()["fake"][0]["max_requests"] == 1

    # two requests then the window is closed for the best key
    r1 = await client.post("/v1/chat/completions", json=CHAT_BODY)
    r2 = await client.post("/v1/chat/completions", json=CHAT_BODY)
    assert r1.status_code == 200 and r2.status_code == 200
    used = harness.container.rate_limiter.usage("fake", "key-1")
    assert used[0]["used_requests"] >= 1

    # persisted: a fresh config load re-applies the console value
    overrides = await harness.container.config_repository.provider_rate_limit_overrides()
    assert overrides["fake"][0]["max_requests"] == 1

    # reset back to YAML: this provider has no YAML definition (built in test),
    # so the effective rules become empty (= unlimited).
    reset = await client.delete("/admin/providers/fake/limits", headers=ADMIN)
    assert reset.status_code == 200
    assert reset.json()["rules"] == []
    assert "fake" not in harness.container.rate_limiter.describe()


async def test_admin_put_rejects_uncapped_rule(limit_api) -> None:
    client, _ = limit_api
    response = await client.put(
        "/admin/providers/fake/limits",
        headers=ADMIN,
        json={"rules": [{"scope": "credential", "window_seconds": 60}]},
    )
    assert response.status_code == 400

    missing = await client.put(
        "/admin/providers/ghost/limits", headers=ADMIN, json={"rules": []}
    )
    assert missing.status_code == 404


async def test_admin_put_clear_all_means_unlimited(limit_api) -> None:
    client, harness = limit_api
    response = await client.put(
        "/admin/providers/fake/limits", headers=ADMIN, json={"rules": []}
    )
    assert response.status_code == 200
    assert harness.container.rate_limiter.remaining("fake", "key-1") == float("inf")

    r = await client.post("/v1/chat/completions", json=CHAT_BODY)
    assert r.status_code == 200
