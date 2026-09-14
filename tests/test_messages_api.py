"""Anthropic ``/v1/messages`` surface: translation, streaming grammar, errors.

The streaming grammar tests are the regression suite for the 2026-09-13
diagnosis: CC Switch's openai_chat translation dropped ``content_block_stop``,
so Anthropic SDK clients (Claude Code) discarded every finalised reply. Every
block opened by the gateway must be closed, exactly once, before
``message_stop``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.main import create_app
from app.models.request import ChatCompletionRequest
from app.models.response import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChunkDelta,
    Usage,
)
from app.providers.base import ProviderContext
from tests.conftest import (
    FakeAdapter,
    build_harness,
    make_alias,
    make_config,
    make_model,
)


class CapturingAdapter(FakeAdapter):
    """FakeAdapter that records translated requests and can stream a script."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.requests: list[ChatCompletionRequest] = []
        self.chunk_script: list[ChatCompletionChunk] | None = None

    async def chat(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> Any:
        self.requests.append(request)
        return await super().chat(request, ctx)

    async def stream(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> AsyncIterator[ChatCompletionChunk]:
        self.requests.append(request)
        if self.chunk_script is None:
            async for chunk in super().stream(request, ctx):
                yield chunk
            return
        self._record(ctx, stream=True)
        self.streams_opened += 1
        for chunk in self.chunk_script:
            yield chunk
        self.streams_completed += 1
        self.streams_closed += 1


def _chunk(
    *,
    delta: ChunkDelta | None = None,
    finish_reason: str | None = None,
    usage: Usage | None = None,
) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        model="fake-model",
        choices=[
            ChatCompletionChunkChoice(delta=delta or ChunkDelta(), finish_reason=finish_reason)
        ],
        usage=usage,
    )


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an ``event:``/``data:`` stream into (event, payload) pairs."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name: str | None = None
        data: dict[str, Any] | None = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if name is not None and data is not None:
            events.append((name, data))
    return events


@pytest.fixture
async def messages_api(provider_config) -> AsyncIterator[tuple[httpx.AsyncClient, CapturingAdapter]]:
    """ASGI client + capturing adapter with the default alias router."""
    adapter = CapturingAdapter(provider_config)
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
    )
    # Pin the switches the local .env would otherwise leak in
    # (Settings reads ZKAI_* environment/.env on this machine too).
    config.settings.anthropic_default_model = "zk-test"
    config.settings.strip_reasoning = False
    harness = await build_harness(config, adapters={"fake": adapter})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
        yield client, adapter
    await harness.container.shutdown()


MESSAGES_BODY: dict[str, Any] = {
    "model": "fake-model",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": "hello"}],
}


# --------------------------------------------------------------------------- #
# Non-streaming
# --------------------------------------------------------------------------- #
async def test_messages_non_streaming_shape(messages_api) -> None:
    client, _adapter = messages_api
    response = await client.post("/v1/messages", json=MESSAGES_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == "fake-model"
    assert body["content"] == [{"type": "text", "text": "fake reply"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] == 10
    assert body["usage"]["output_tokens"] == 5


async def test_messages_translates_system_tooluse_and_toolresult(messages_api) -> None:
    client, adapter = messages_api
    body = {
        "model": "fake-model",
        "max_tokens": 256,
        "system": [{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "weather in Paris?"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "checking"},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": [{"type": "text", "text": "18C, sunny"}],
                    },
                    {"type": "text", "text": "and tomorrow?"},
                ],
            },
        ],
        "tools": [
            {
                "name": "get_weather",
                "description": "weather lookup",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ],
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 200

    request = adapter.requests[0]
    roles = [message.role for message in request.messages]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert request.messages[0].content == "be brief"
    calls = request.messages[2].tool_calls
    assert calls is not None and calls[0].function.name == "get_weather"
    assert json.loads(calls[0].function.arguments) == {"city": "Paris"}
    assert request.messages[3].tool_call_id == "toolu_01"
    assert request.messages[3].content == "18C, sunny"
    assert request.messages[4].content == "and tomorrow?"
    assert request.tools is not None
    assert request.tools[0]["type"] == "function"
    assert request.tools[0]["function"]["parameters"]["properties"] == {"city": {"type": "string"}}


async def test_messages_model_fallback_and_context_suffix(messages_api) -> None:
    client, adapter = messages_api
    # Unknown claude-* id falls back to the configured default alias.
    await client.post(
        "/v1/messages",
        json={**MESSAGES_BODY, "model": "claude-sonnet-5[1M]"},
    )
    assert adapter.requests[0].model == "zk-test"
    # A known alias with a [1M] suffix keeps its id, suffix stripped.
    await client.post("/v1/messages", json={**MESSAGES_BODY, "model": "zk-smart[1M]"})
    assert adapter.requests[1].model == "zk-smart"


async def test_messages_count_tokens(messages_api) -> None:
    client, _adapter = messages_api
    response = await client.post("/v1/messages/count_tokens", json=MESSAGES_BODY)
    assert response.status_code == 200
    assert response.json()["input_tokens"] >= 1


async def test_messages_error_uses_anthropic_envelope(messages_api) -> None:
    client, adapter = messages_api
    adapter.queue_status(*([429] * 8))  # exhaust every retry slot
    response = await client.post("/v1/messages", json=MESSAGES_BODY)
    assert response.status_code == 429
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "rate_limit_error"


async def test_messages_auth_accepts_x_api_key(provider_config) -> None:
    adapter = CapturingAdapter(provider_config)
    config = make_config(
        providers=[provider_config],
        models=[make_model("fake-model")],
        aliases=[make_alias("zk-test", ["fake-model"])],
    )
    config.settings.anthropic_default_model = "zk-test"
    config.settings.strip_reasoning = False
    config.settings.api_token = "tok"
    harness = await build_harness(config, adapters={"fake": adapter})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
        denied = await client.post("/v1/messages", json=MESSAGES_BODY)
        assert denied.status_code == 401
        assert denied.json()["detail"]["error"]["type"] == "authentication_error"

        ok = await client.post(
            "/v1/messages", json=MESSAGES_BODY, headers={"x-api-key": "tok"}
        )
        assert ok.status_code == 200
    await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# Streaming grammar
# --------------------------------------------------------------------------- #
async def test_streaming_closes_every_content_block(messages_api) -> None:
    """THE regression: every content_block_start needs a content_block_stop."""
    client, _adapter = messages_api
    response = await client.post("/v1/messages", json={**MESSAGES_BODY, "stream": True})
    assert response.status_code == 200
    events = parse_sse(response.text)
    names = [name for name, _data in events]

    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    assert names.count("content_block_start") == names.count("content_block_stop") > 0

    starts = [data for name, data in events if name == "content_block_start"]
    assert starts[0]["content_block"]["type"] == "text"
    deltas = [data for name, data in events if name == "content_block_delta"]
    assert "".join(
        item["delta"]["text"] for item in deltas if item["delta"]["type"] == "text_delta"
    ) == "part0 part1 part2 "

    message_delta = next(data for name, data in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert message_delta["usage"]["output_tokens"] == 5


async def test_streaming_tool_use_blocks_and_stop_reason(messages_api) -> None:
    client, adapter = messages_api
    adapter.chunk_script = [
        _chunk(delta=ChunkDelta(role="assistant")),
        _chunk(
            delta=ChunkDelta(
                content=None,
                tool_calls=[
                    {
                        "id": "call_01",
                        "index": 0,
                        "function": {"name": "get_weather", "arguments": '{"city":'},
                    }
                ],
            )
        ),
        _chunk(
            delta=ChunkDelta(
                tool_calls=[{"index": 0, "function": {"arguments": ' "Paris"}'}}]
            )
        ),
        _chunk(
            finish_reason="tool_calls",
            usage=Usage.build(10, 5),
        ),
    ]
    response = await client.post("/v1/messages", json={**MESSAGES_BODY, "stream": True})
    events = parse_sse(response.text)
    names = [name for name, _data in events]

    assert names.count("content_block_start") == names.count("content_block_stop") == 1
    start = next(data for name, data in events if name == "content_block_start")
    assert start["content_block"]["type"] == "tool_use"
    assert start["content_block"]["name"] == "get_weather"
    assert start["content_block"]["id"] == "call_01"
    partials = [
        data["delta"]["partial_json"]
        for name, data in events
        if name == "content_block_delta" and item_delta_type(data) == "input_json_delta"
    ]
    assert json.loads("".join(partials)) == {"city": "Paris"}
    message_delta = next(data for name, data in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "tool_use"


async def test_streaming_reasoning_becomes_thinking_block(messages_api) -> None:
    # Reasoning-only deltas are recovered into ``content`` upstream (never-blank
    # guard) before they reach this surface, so the thinking mapping only fires
    # when a provider streams reasoning alongside content.
    client, adapter = messages_api
    adapter.chunk_script = [
        _chunk(
            delta=ChunkDelta(role="assistant", reasoning_content="pondering", content="answer")
        ),
        _chunk(
            finish_reason="stop",
            usage=Usage.build(10, 5),
        ),
    ]
    response = await client.post("/v1/messages", json={**MESSAGES_BODY, "stream": True})
    events = parse_sse(response.text)
    names = [name for name, _data in events]

    assert names.count("content_block_start") == 2  # thinking + text
    assert names.count("content_block_stop") == 2
    blocks = [data["content_block"]["type"] for name, data in events if name == "content_block_start"]
    assert blocks == ["thinking", "text"]
    thinking = next(
        data
        for name, data in events
        if name == "content_block_delta" and item_delta_type(data) == "thinking_delta"
    )
    assert thinking["delta"]["thinking"] == "pondering"


def item_delta_type(data: dict[str, Any]) -> str:
    return str(data["delta"]["type"])
