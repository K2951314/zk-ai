"""HTTP layer: OpenAI compatibility, streaming, admin API, disconnects."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from app.api.chat import _watch_disconnect
from app.main import create_app
from app.models.provider import AliasStrategy
from app.models.request import ChatCompletionRequest
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


# --------------------------------------------------------------------------- #
# Cost accounting: must bill the deployment that actually served the request
# --------------------------------------------------------------------------- #
def test_estimate_cost_uses_the_served_deployment_price() -> None:
    """Regression: kimi-k3 has 3 priced deployments; a request that failed over to
    the NVIDIA one must be priced at NVIDIA's list price, not the first."""
    from app.models.provider import CapabilityScores, DeploymentConfig, ModelConfig
    from app.models.response import Usage
    from app.services.usage_service import UsageService

    def _cap() -> CapabilityScores:
        return CapabilityScores()

    model = ModelConfig(
        id="kimi-k3",
        context_window=1_000_000,
        capabilities=_cap(),
        deployments=[
            DeploymentConfig(id="k3-sen", provider_id="sensenova", model="kimi-k3",
                             input_cost_per_mtok=1.0, output_cost_per_mtok=5.0),
            DeploymentConfig(id="k3-nv", provider_id="nvidia", model="moonshotai/kimi-k3",
                             input_cost_per_mtok=2.0, output_cost_per_mtok=10.0),
        ],
    )
    from app.core.config import AppConfig, Settings
    cfg = AppConfig(settings=Settings(), models={"kimi-k3": model})
    svc = UsageService(cfg)
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=500_000)
    # served by nvidia -> 1M*2.0 + 0.5M*10.0 = 7.0 USD
    assert svc.estimate_cost(model_id="kimi-k3", deployment_id="k3-nv", usage=usage) == 7.0
    # served by sensenova -> 1M*1.0 + 0.5M*5.0 = 3.5 USD
    assert svc.estimate_cost(model_id="kimi-k3", deployment_id="k3-sen", usage=usage) == 3.5


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
async def api(provider_config, fake_adapter) -> AsyncIterator[tuple[httpx.AsyncClient, Harness]]:
    """A live ASGI client backed by fake providers."""
    config = make_config(
        providers=[provider_config],
        models=[
            make_model("fake-model", capabilities={"coding": 6.0}),
            make_model("fake-smart", priority=90, capabilities={"coding": 9.0}),
        ],
        aliases=[
            make_alias("zk-test", ["fake-model", "fake-smart"]),
            make_alias("zk-smart", ["fake-smart"]),
        ],
        admin_token="test-admin-token",
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
        yield client, harness
    await harness.container.shutdown()


CHAT_BODY = {
    "model": "fake-model",
    "messages": [{"role": "user", "content": "hello"}],
}


# --------------------------------------------------------------------------- #
# Health / models
# --------------------------------------------------------------------------- #
async def test_health_endpoint(api) -> None:
    client, _harness = api
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"healthy", "degraded", "starting"}
    assert body["database"]["ok"] is True
    assert body["providers"]["total"] == 1
    assert body["credentials"]["total"] == 3
    assert "zk-test" in body["aliases"]


async def test_models_endpoint_lists_models_and_aliases(api) -> None:
    client, _ = api
    response = await client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    ids = {item["id"] for item in body["data"]}
    assert {"fake-model", "fake-smart", "zk-test", "zk-smart"} <= ids
    assert all(item["object"] == "model" for item in body["data"])
    alias_entry = next(item for item in body["data"] if item["id"] == "zk-test")
    assert alias_entry["zk_ai"]["kind"] == "alias"
    assert alias_entry["zk_ai"]["targets"] == ["fake-model", "fake-smart"]


async def test_retrieve_single_model_and_alias(api) -> None:
    client, _ = api
    described = await client.get("/v1/models/fake-smart")
    assert described.status_code == 200
    assert described.json()["zk_ai"]["kind"] == "model"

    alias = await client.get("/v1/models/zk-smart")
    assert alias.status_code == 200
    assert alias.json()["zk_ai"]["kind"] == "alias"

    missing = await client.get("/v1/models/nope")
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# Chat completions (non-streaming)
# --------------------------------------------------------------------------- #
async def test_chat_completion_success(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(text="hello from the fake", prompt_tokens=7, completion_tokens=3))
    response = await client.post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 200

    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "fake-model"
    assert body["choices"][0]["message"]["content"] == "hello from the fake"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
                            "cached_tokens": 0}
    # Routing explainability + headers.
    assert body["zk_ai"]["provider"] == "fake"
    assert body["zk_ai"]["credential_id"]
    assert response.headers["x-zkai-provider"] == "fake"
    assert response.headers["x-zkai-request-id"].startswith("chatcmpl-")


async def test_chat_completion_via_alias(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(text="routed by alias"))
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "model": "zk-smart"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "zk-smart"  # the client sees what it asked for
    assert body["zk_ai"]["alias"] == "zk-smart"
    assert body["zk_ai"]["resolved_model"] == "fake-smart"
    assert harness.all_calls()[0]["model"] == "fake-smart"  # upstream name


async def test_request_id_is_echoed(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(text="ok"))
    response = await client.post(
        "/v1/chat/completions", json=CHAT_BODY, headers={"x-request-id": "chatcmpl-fixed-id"}
    )
    assert response.headers["x-zkai-request-id"] == "chatcmpl-fixed-id"


async def test_optional_parameters_are_forwarded(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(text="ok"))
    response = await client.post(
        "/v1/chat/completions",
        json={
            **CHAT_BODY,
            "temperature": 0.2,
            "top_p": 0.9,
            "max_tokens": 64,
            "stop": ["\n"],
            "response_format": {"type": "json_object"},
        },
    )
    assert response.status_code == 200


async def test_unsupported_parameters_are_recorded_not_fatal(api) -> None:
    """Provider-specific gaps are logged, never silently dropped or fatal."""
    client, harness = api
    harness.adapter.queue(Behavior(text="ok"))
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "tools": None, "custom_param": 1}
    )
    assert response.status_code == 200
    assert "custom_param" in harness.adapter.unsupported_seen


async def test_400_is_returned_without_rotation(api) -> None:
    client, harness = api
    harness.adapter.queue_status(400)
    response = await client.post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert len(harness.all_calls()) == 1


async def test_413_is_returned_without_rotation(api) -> None:
    client, harness = api
    harness.adapter.queue_status(413)
    response = await client.post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 413
    assert response.json()["error"]["type"] == "context_length_exceeded"
    assert len(harness.all_calls()) == 1


async def test_429_fails_over_to_the_next_key(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(status=429), Behavior(text="second key served this"))
    response = await client.post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "second key served this"
    assert response.headers["x-zkai-fallback"] == "true"
    assert harness.pool.get("key-1").status.value == "cooldown"


async def test_resolved_model_reflects_the_served_target(provider_config, fake_adapter) -> None:
    """Regression: after failover, resolved_model must be the model that actually
    answered (targets[1] here), not the alias plan head.

    The console actual-model column read the plan head while the provider column
    read the winning candidate, so every fallback row looked like a model/provider
    mismatch (e.g. resolved_model=kimi-k3 paired with provider=StepFun)."""
    from app.models.provider import AliasStrategy

    provider_b = make_provider("fake-b", key_ids=("kb-1",))
    adapter_b = FakeAdapter(provider_b)

    config = make_config(
        providers=[provider_config, provider_b],
        models=[
            make_model("fake-model", provider_id="fake"),
            make_model("fake-smart", provider_id="fake-b"),
        ],
        aliases=[
            make_alias("zk-test", ["fake-model", "fake-smart"], strategy=AliasStrategy.PRIORITY),
        ],
        admin_token="test-admin-token",
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter, "fake-b": adapter_b})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
            fake_adapter.queue(
                Behavior(status=429),
                Behavior(status=429),
                Behavior(status=429),
            )
            adapter_b.queue(Behavior(text="served by the second target"))
            response = await client.post(
                "/v1/chat/completions", json={**CHAT_BODY, "model": "zk-test"}
            )
    finally:
        await harness.container.shutdown()

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "served by the second target"
    assert body["zk_ai"]["resolved_model"] == "fake-smart"
    assert body["zk_ai"]["resolved_model"] != body["zk_ai"]["requested_model"]


async def test_all_providers_failing_returns_error_with_attempts(api) -> None:
    """Every attempt exhausted: the upstream 500 is mirrored and attempts listed."""
    client, harness = api
    harness.adapter.queue_status(*([500] * 12))
    response = await client.post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["type"] in {"all_attempts_failed", "upstream_error"}
    assert body["error"]["attempts"]
    assert "sk-" not in json.dumps(body)


async def test_unknown_model_returns_404(api) -> None:
    client, _ = api
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "model": "does-not-exist"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "model_not_found"


async def test_invalid_payload_returns_400(api) -> None:
    client, _ = api
    response = await client.post("/v1/chat/completions", json={"model": "fake-model"})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert response.json()["error"]["details"]


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #
async def parse_sse(text: str) -> tuple[list[dict], list[str], list[str]]:
    chunks: list[dict] = []
    comments: list[str] = []
    raw: list[str] = []
    for block in text.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                raw.append(payload)
                if payload != "[DONE]":
                    chunks.append(json.loads(payload))
            elif line.startswith(":"):
                comments.append(line[1:].strip())
    return chunks, comments, raw


async def test_streaming_returns_openai_chunks(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(text="ignored", chunks=3, prompt_tokens=4, completion_tokens=6))
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "stream": True}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    chunks, comments, raw = await parse_sse(response.text)
    assert raw[-1] == "[DONE]"
    assert chunks[0]["object"] == "chat.completion.chunk"
    text = "".join(
        (chunk["choices"][0]["delta"].get("content") or "") for chunk in chunks
    )
    assert text == "part0 part1 part2 "
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    # Routing metadata travels as an SSE comment (invisible to OpenAI SDKs).
    assert any(comment.startswith("zkai-meta") for comment in comments)
    assert "usage" not in chunks[-1]  # only sent when explicitly requested


async def test_streaming_usage_only_with_stream_options(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(chunks=1, prompt_tokens=5, completion_tokens=8))
    response = await client.post(
        "/v1/chat/completions",
        json={**CHAT_BODY, "stream": True, "stream_options": {"include_usage": True}},
    )
    chunks, _, _ = await parse_sse(response.text)
    assert chunks[-1]["usage"]["total_tokens"] == 13


async def test_streaming_error_before_first_chunk_fails_over(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(status=429), Behavior(chunks=1, text="ok"))
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "stream": True}
    )
    assert response.status_code == 200
    _, _, raw = await parse_sse(response.text)
    assert raw[-1] == "[DONE]"
    assert harness.pool.get("key-1").status.value == "cooldown"


async def test_streaming_preflight_error_returns_json_status(api) -> None:
    client, harness = api
    harness.adapter.queue_status(*([400] * 3))
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "stream": True}
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_streaming_all_fail_returns_503(api) -> None:
    """A generic 5xx class failure becomes 503 (gateway cannot serve the model)."""
    client, harness = api
    harness.adapter.queue_status(*([520] * 12))
    response = await client.post(
        "/v1/chat/completions", json={**CHAT_BODY, "stream": True}
    )
    assert response.status_code == 503


async def test_client_disconnect_closes_the_upstream_stream(api) -> None:
    """Cancelling the consumer must release the provider connection."""
    _client, harness = api
    harness.adapter.queue(Behavior(chunks=20, delay=0.05))
    request = ChatCompletionRequest(**{**CHAT_BODY, "stream": True})

    async def consume() -> None:
        async for _event in harness.service.stream(request, request_id="req-disconnect"):
            await asyncio.sleep(0.01)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)

    assert harness.adapter.streams_opened == 1
    assert harness.adapter.streams_closed >= 1
    assert harness.adapter.streams_completed == 0
    recent = await harness.container.request_repository.recent(limit=5)
    assert recent[0]["status"] == "cancelled"


async def test_completed_stream_is_recorded_as_success(api) -> None:
    """Closing the generator *after* the ``end`` event is completion, not a cancel.

    The HTTP layer stops consuming as soon as it sees ``end`` and then calls
    ``aclose()``; that must not be misread as a client disconnect.
    """
    client, harness = api
    harness.adapter.queue(Behavior(chunks=2))
    response = await client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": True})
    assert response.status_code == 200
    _, _, raw = await parse_sse(response.text)
    assert raw[-1] == "[DONE]"

    recent = await harness.container.request_repository.recent(limit=5)
    assert recent[0]["status"] == "success"
    assert recent[0]["http_status"] == 200


async def test_disconnect_watcher_completes_on_disconnect() -> None:
    class StubRequest:
        def __init__(self, states: list[bool]) -> None:
            self._states = states

        async def is_disconnected(self) -> bool:
            return self._states.pop(0) if self._states else True

    await asyncio.wait_for(_watch_disconnect(StubRequest([False, False, True]), 0.01), timeout=1)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_watch_disconnect(StubRequest([False] * 50), 0.05), timeout=0.2)


# --------------------------------------------------------------------------- #
# Responses API
# --------------------------------------------------------------------------- #
async def test_responses_endpoint(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(text="responses answer"))
    response = await client.post(
        "/v1/responses", json={"model": "fake-model", "input": "hello", "instructions": "be nice"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["output_text"] == "responses answer"
    assert body["output"][0]["content"][0]["type"] == "output_text"
    assert body["usage"]["total_tokens"] >= 0


async def test_responses_streaming(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(chunks=2))
    response = await client.post(
        "/v1/responses", json={"model": "fake-model", "input": "hello", "stream": True}
    )
    assert response.status_code == 200
    assert "response.created" in response.text
    assert "response.output_text.delta" in response.text
    assert "response.completed" in response.text


# --------------------------------------------------------------------------- #
# Admin API
# --------------------------------------------------------------------------- #
ADMIN = {"x-admin-token": "test-admin-token"}


async def test_admin_requires_the_token(api) -> None:
    client, _ = api
    assert (await client.get("/admin/providers")).status_code == 401
    assert (await client.get("/admin/providers", headers=ADMIN)).status_code == 200
    assert (
        await client.get("/admin/providers", headers={"authorization": "Bearer test-admin-token"})
    ).status_code == 200
    assert (
        await client.get("/admin/providers", headers={"x-admin-token": "wrong"})
    ).status_code == 401


async def test_admin_providers_models_and_credentials(api) -> None:
    client, _ = api
    providers = (await client.get("/admin/providers", headers=ADMIN)).json()
    assert providers["data"][0]["id"] == "fake"
    assert providers["data"][0]["available"] is True

    models = (await client.get("/admin/models", headers=ADMIN)).json()
    assert {item["id"] for item in models["data"]} == {"fake-model", "fake-smart"}
    assert "zk-test" in models["aliases"]

    credentials = (await client.get("/admin/credentials", headers=ADMIN)).json()
    assert len(credentials["data"]) == 3
    assert credentials["stats"]["usable"] == 3
    assert "secret" not in json.dumps(credentials["data"]).lower().replace(
        "secret_masked", ""
    ).replace("secret_fingerprint", "").replace("secret_source", "").replace(
        "secret_ref", ""
    )


async def test_admin_disable_and_enable_credential(api) -> None:
    client, harness = api
    disabled = await client.post("/admin/credentials/key-1/disable", headers=ADMIN)
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert harness.pool.get("key-1").status.value == "disabled"

    enabled = await client.post("/admin/credentials/key-1/enable", headers=ADMIN)
    assert enabled.status_code == 200
    assert harness.pool.get("key-1").status.value == "healthy"

    missing = await client.post("/admin/credentials/nope/enable", headers=ADMIN)
    assert missing.status_code == 404


async def test_admin_health_and_manual_check(api) -> None:
    client, harness = api
    report = (await client.get("/admin/health", headers=ADMIN)).json()
    assert report["summary"]["providers"] == 1

    checked = await client.post("/admin/health/check", headers=ADMIN, json={})
    assert checked.status_code == 200
    assert checked.json()["ok"] == 3  # one probe per credential
    assert harness.adapter.health_calls == 3

    failing = await client.post(
        "/admin/health/check", headers=ADMIN, json={"providers": ["ghost"]}
    )
    assert failing.status_code == 200
    assert failing.json()["failed"] == 1


async def test_admin_router_preview(api) -> None:
    client, _ = api
    response = await client.get(
        "/admin/router/preview",
        params={"model": "zk-test", "prompt": "write a python function", "tools": 1},
        headers=ADMIN,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["requested_model"] == "zk-test"
    assert body["plan"]
    assert body["plan"][0]["reason"]
    assert "pool" in body


async def test_admin_alias_hot_swap(api) -> None:
    client, harness = api
    created = await client.post(
        "/admin/aliases",
        headers=ADMIN,
        json={"name": "zk-new", "targets": ["fake-smart"], "strategy": "priority"},
    )
    assert created.status_code == 200

    harness.adapter.queue(Behavior(text="via new alias"))
    chat = await client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "zk-new"})
    assert chat.status_code == 200
    assert chat.json()["zk_ai"]["alias"] == "zk-new"

    invalid = await client.post(
        "/admin/aliases", headers=ADMIN, json={"name": "zk-bad", "targets": ["ghost"]}
    )
    assert invalid.status_code == 400

    deleted = await client.delete("/admin/aliases/zk-new", headers=ADMIN)
    assert deleted.status_code == 200
    assert (await client.delete("/admin/aliases/zk-new", headers=ADMIN)).status_code == 404


async def test_admin_stats_records_usage(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(text="a", prompt_tokens=10, completion_tokens=5))
    harness.adapter.queue(Behavior(text="b", prompt_tokens=20, completion_tokens=15))
    await client.post("/v1/chat/completions", json=CHAT_BODY)
    await client.post("/v1/chat/completions", json=CHAT_BODY)

    stats = (await client.get("/admin/stats", headers=ADMIN)).json()
    assert stats["usage"]["requests"] == 2
    assert stats["usage"]["total_tokens"] == 50
    assert stats["pool"]["success"] == 2
    assert len(stats["recent_requests"]) == 2
    assert stats["recent_requests"][0]["status"] == "success"

    request_id = stats["recent_requests"][0]["id"]
    detail = (await client.get(f"/admin/requests/{request_id}", headers=ADMIN)).json()
    assert detail["attempts"][0]["attempt_number"] == 1
    assert detail["attempts"][0]["input_tokens"] == 20


async def test_admin_records_failed_attempts(api) -> None:
    client, _ = api
    harness_adapter = api[1].adapter
    harness_adapter.queue_status(429, 200)
    await client.post("/v1/chat/completions", json=CHAT_BODY)
    stats = (await client.get("/admin/stats", headers=ADMIN)).json()
    request_id = stats["recent_requests"][0]["id"]
    detail = (await client.get(f"/admin/requests/{request_id}", headers=ADMIN)).json()
    assert [item["status"] for item in detail["attempts"]] == ["error", "success"]
    assert detail["attempts"][0]["error_type"] == "rate_limit_error"
    assert detail["attempts"][0]["http_status"] == 429


async def test_admin_unknown_request_returns_404(api) -> None:
    client, _ = api
    response = await client.get("/admin/requests/does-not-exist", headers=ADMIN)
    assert response.status_code == 404


async def test_ui_console_is_served_without_token(api) -> None:
    """/ui 只是静态壳（不含任何数据），数据仍走受保护的 /admin/*。"""
    client, _ = api
    response = await client.get("/ui")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "ZK-AI 控制台" in response.text


async def test_admin_requests_list_filters_and_pages(api) -> None:
    client, harness = api
    harness.adapter.queue(Behavior(text="a", prompt_tokens=10, completion_tokens=5))
    harness.adapter.queue_status(429, 200)  # 先 429 后成功：整体仍算 success
    await client.post("/v1/chat/completions", json=CHAT_BODY)
    await client.post("/v1/chat/completions", json=CHAT_BODY)

    listed = (await client.get("/admin/requests", headers=ADMIN)).json()
    assert listed["total"] == 2 and listed["object"] == "list"
    assert [row["status"] for row in listed["data"]] == ["success", "success"]
    assert listed["data"][0]["attempt_count"] >= 1

    by_model = (await client.get("/admin/requests?model=fake", headers=ADMIN)).json()
    assert by_model["total"] == 2
    by_alias = (await client.get("/admin/requests?alias=不存在的别名", headers=ADMIN)).json()
    assert by_alias["total"] == 0
    errored = (await client.get("/admin/requests?error_type=rate_limit_error", headers=ADMIN)).json()
    assert errored["total"] == 0  # 请求最终成功了，错误只存在于尝试明细

    page = (await client.get("/admin/requests?limit=1&offset=1", headers=ADMIN)).json()
    assert page["total"] == 2 and len(page["data"]) == 1
    assert page["data"][0]["id"] != listed["data"][0]["id"]


async def test_admin_request_detail_exposes_row_and_attempt_detail(api) -> None:
    client, harness = api
    harness.adapter.queue_status(429, 200)
    await client.post("/v1/chat/completions", json=CHAT_BODY)
    listed = (await client.get("/admin/requests", headers=ADMIN)).json()
    request_id = listed["data"][0]["id"]

    detail = (await client.get(f"/admin/requests/{request_id}", headers=ADMIN)).json()
    assert detail["request"]["requested_model"] == "fake-model"
    assert detail["request"]["status"] == "success"
    first = detail["attempts"][0]
    assert first["status"] == "error" and first["error_type"] == "rate_limit_error"
    assert first["detail"]  # 上游原文要能在控制台里看到


async def test_admin_endpoints_hidden_when_disabled(provider_config, fake_adapter) -> None:
    config = make_config(providers=[provider_config], models=[make_model("fake-model")])
    config.settings.admin_enabled = False
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
            response = await client.get("/admin/providers")
            assert response.status_code == 404
    finally:
        await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# Retry-After surfacing (pool blackout / throttle back-off for well-behaved SDKs)
# --------------------------------------------------------------------------- #
def test_error_response_carries_retry_after_header() -> None:
    from app.api.chat import error_response
    from app.core.errors import RateLimitError

    throttled = RateLimitError("slow down", provider="p", retry_after=1800.5)
    response = error_response(throttled)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1801"
    assert json.loads(response.body)["error"]["retry_after"] == 1800.5

    naked = RateLimitError("slow down", provider="p")
    assert "retry-after" not in error_response(naked).headers


# --------------------------------------------------------------------------- #
# Strip-reasoning toggle (hide thinking bubbles, keep promoted answers)
# --------------------------------------------------------------------------- #
def test_strip_reasoning_fields_unconditional() -> None:
    from app.api.chat import _strip_reasoning_fields

    payload = {
        "choices": [
            {
                "delta": {
                    "content": "答案",
                    "reasoning_content": "英文思考",
                }
            }
        ]
    }
    _strip_reasoning_fields(payload)
    assert payload["choices"][0]["delta"] == {"content": "答案"}

    # 开关语义是「不想看到思考」：正文为空时 reasoning 键也要剥（允许空白回复，
    # 截断信号由 finish_reason=length 承担）
    empty = {"choices": [{"delta": {"content": "", "reasoning_content": "英文思考"}}]}
    _strip_reasoning_fields(empty)
    assert empty["choices"][0]["delta"] == {"content": ""}

    # 非流式的 message 节同样要剥
    msg = {"choices": [{"message": {"role": "assistant", "content": "答案", "reasoning": "思考"}}]}
    _strip_reasoning_fields(msg)
    assert "reasoning" not in msg["choices"][0]["message"]


def test_strip_reasoning_blanks_backfilled_thinking() -> None:
    """回填场景（缺陷 8）：上游 content 为空、思考被提升进 content 并打标。
    剥离开启后这段思考也必须折叠——否则用户看到的还是英文思考。"""
    from app.api.chat import _strip_reasoning_fields

    promoted = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Thinking Process:\n\n1. **Analyze**...",
                    "content_recovered_from_reasoning": True,
                }
            }
        ]
    }
    _strip_reasoning_fields(promoted)
    msg = promoted["choices"][0]["message"]
    assert msg["content"] == ""
    assert "content_recovered_from_reasoning" not in msg


def test_strip_reasoning_blanks_backfilled_delta() -> None:
    """流式回填路径同样要折叠：delta 节被打标时正文置空。"""
    from app.api.chat import _strip_reasoning_fields

    payload = {
        "choices": [
            {
                "delta": {
                    "content": "Thinking Process: ...",
                    "content_recovered_from_reasoning": True,
                }
            }
        ]
    }
    _strip_reasoning_fields(payload)
    delta = payload["choices"][0]["delta"]
    assert delta == {"content": ""}


def test_strip_reasoning_multi_choice_mixed() -> None:
    """多 choice 混合：带回填标记的清空，正常答案不受影响。"""
    from app.api.chat import _strip_reasoning_fields

    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Thinking Process: ...",
                    "content_recovered_from_reasoning": True,
                }
            },
            {
                "message": {
                    "role": "assistant",
                    "content": "正常答案",
                    "reasoning_content": "英文思考",
                }
            },
        ]
    }
    _strip_reasoning_fields(payload)
    assert payload["choices"][0]["message"]["content"] == ""
    assert payload["choices"][1]["message"] == {"role": "assistant", "content": "正常答案"}


async def test_strip_reasoning_hides_thinking_in_stream(provider_config, fake_adapter) -> None:
    """端到端：开关打开后，流式 chunk 不再带 reasoning 字段。"""
    config = make_config(
        providers=[provider_config], models=[make_model("fake-model")], admin_token="tok"
    )
    config.settings.strip_reasoning = True
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
            harness.adapter.queue(Behavior(text="ok"))
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "fake-model",
                      "messages": [{"role": "user", "content": "hi"}],
                      "stream": True, "stream_options": {"include_usage": True}},
                headers={"Authorization": "Bearer x"},
            )
            assert resp.status_code == 200
            assert "reasoning_content" not in resp.text
            assert "part0" in resp.text
    finally:
        await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# Inference-surface auth (ZKAI_API_TOKEN) and request-size guard
# --------------------------------------------------------------------------- #
async def test_api_token_gates_chat_when_configured(provider_config, fake_adapter) -> None:
    """With api_token set, /v1/chat/completions requires a matching Bearer token."""
    config = make_config(
        providers=[provider_config], models=[make_model("fake-model")], admin_token="tok"
    )
    config.settings.api_token = "secret-tok"
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
            harness.adapter.queue(Behavior(text="ok"))
            unauthed = await client.post(
                "/v1/chat/completions",
                json={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert unauthed.status_code == 401

            harness.adapter.queue(Behavior(text="ok"))
            authed = await client.post(
                "/v1/chat/completions",
                json={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer secret-tok"},
            )
            assert authed.status_code == 200
    finally:
        await harness.container.shutdown()


async def test_oversized_body_is_rejected_with_413(provider_config, fake_adapter) -> None:
    """max_request_body_mb must actually be enforced (it used to be a dead setting)."""
    config = make_config(
        providers=[provider_config], models=[make_model("fake-model")], admin_token="tok"
    )
    config.settings.max_request_body_mb = 0.001  # ~1KB
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "fake-model",
                    "messages": [{"role": "user", "content": "x" * 10_000}],
                },
            )
            assert resp.status_code == 413
    finally:
        await harness.container.shutdown()


async def test_finish_preserves_a_previously_recorded_error_type(provider_config) -> None:
    """Second finalize pass without error_type must not wipe the real one."""
    config = make_config(providers=[provider_config], models=[make_model("fake-model")])
    harness = await build_harness(config, adapters={})
    try:
        repo = harness.container.request_repository
        await repo.start(request_id="req-x", requested_model="fake-model", stream=False)
        await repo.finish("req-x", status="error", http_status=429, error_type="rate_limit_error")
        await repo.finish("req-x", status="error", http_status=429)  # no error_type this time
        row = await repo.get_request("req-x")
        assert row["error_type"] == "rate_limit_error"
    finally:
        await harness.container.shutdown()


async def test_sync_config_prunes_rows_removed_from_yaml(provider_config, fake_adapter) -> None:
    """Sync is a mirror: a credential removed from config must leave the DB too."""
    config = make_config(providers=[provider_config], models=[make_model("fake-model")])
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    try:
        await harness.container.config_repository.sync_config(config)
        before = await harness.container.config_repository.list_credentials()
        assert len(before) == len(provider_config.credentials)

        # Drop every credential except the first one and re-sync.
        provider_config.credentials = provider_config.credentials[:1]
        await harness.container.config_repository.sync_config(config)
        after = await harness.container.config_repository.list_credentials()
        assert [row["id"] for row in after] == [provider_config.credentials[0].id]
    finally:
        await harness.container.shutdown()


async def test_backfill_reprices_zero_cost_usage_rows(api) -> None:
    """The console button: rows recorded before pricing get their cost filled in."""
    client, harness = api
    usage_repo = harness.container.usage_repository
    # Seed a zero-cost usage row (as if written before prices existed).
    await usage_repo.record(
        request_id="req-old",
        provider_id="fake",
        model="fake-model",
        credential_id="key-1",
        input_tokens=1_000_000,
        output_tokens=0,
        cost_usd=0.0,
    )
    # Give the model's deployment a price, then hit the endpoint.
    deployment = harness.container.config.models["fake-model"].deployments[0]
    deployment.input_cost_per_mtok = 2.0
    deployment.output_cost_per_mtok = 10.0

    response = await client.post("/admin/usage/backfill-cost", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["updated"] == 1
    assert body["total_cost_usd"] == 2.0  # 1M input tokens at $2/MTok

    # Idempotent: a second run touches nothing.
    again = await client.post("/admin/usage/backfill-cost", headers=ADMIN)
    assert again.json()["updated"] == 0
# --------------------------------------------------------------------------- #
# ChatGPT hot-swap: the pinned model must actually lead the attempt order
# --------------------------------------------------------------------------- #
async def test_chatgpt_hot_swap_takes_effect_on_the_next_request(provider_config, fake_adapter) -> None:
    """End-to-end regression for the console's model hot-swap.

    ``zk-auto`` uses ``strategy=capability``, so promoting a model to
    ``targets[0]`` used to change nothing - the highest-scoring model kept winning
    and the operator saw no effect at all.
    """
    provider_b = make_provider("fake-b", key_ids=("kb-1",))
    adapter_b = FakeAdapter(provider_b)

    config = make_config(
        providers=[provider_config, provider_b],
        models=[
            # kimi-k3 scores highest, so it wins without a pin.
            make_model("kimi-k3", provider_id="fake", capabilities={"coding": 9.5, "reasoning": 9.5}),
            make_model("glm-5.3", provider_id="fake-b", capabilities={"coding": 8.0, "reasoning": 8.0}),
        ],
        aliases=[make_alias("zk-auto", ["kimi-k3", "glm-5.3"], strategy=AliasStrategy.CAPABILITY)],
        admin_token="test-admin-token",
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter, "fake-b": adapter_b})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
            adapter_b.queue(Behavior(text="glm first"))
            first = await client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "zk-auto"})
            assert first.status_code == 200
            assert first.json()["zk_ai"]["resolved_model"] == "kimi-k3"  # highest score wins

            # Now hot-swap to glm-5.3 through the console endpoint.
            swapped = await client.post(
                "/admin/chatgpt", json={"model": "glm-5.3"}, headers=ADMIN
            )
            assert swapped.status_code == 200
            assert swapped.json()["model"] == "glm-5.3"

            fake_adapter.queue(Behavior(status=500))
            adapter_b.queue(Behavior(text="glm still leads"))
            second = await client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "zk-auto"})
            assert second.status_code == 200
            assert second.json()["zk_ai"]["resolved_model"] == "glm-5.3"
            assert second.json()["zk_ai"]["provider"] == "fake-b"
    finally:
        await harness.container.shutdown()
