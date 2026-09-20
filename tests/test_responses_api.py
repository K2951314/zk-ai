"""Responses API surface: translation in, event grammar out.

The regression suite for the Codex CLI integration (2026-09-20). Codex 0.154+
speaks *only* the Responses wire, so this file pins down the two things its
agent loop cannot live without:

1. **Tool round trips.** A ``function_call`` / ``function_call_output`` input
   pair must reach the upstream as an assistant message with ``tool_calls``
   plus a ``tool`` message, and the model's tool call must come back as a
   ``function_call`` output item.
2. **The streaming grammar.** Every delta is announced by
   ``response.output_item.added`` first - Codex logs "OutputTextDelta without
   active item" and drops the reply when a text delta arrives with no open
   item - and ``response.completed`` carries the full response object.
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
    ChatCompletionResponse,
    ChunkDelta,
    ToolCall,
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


class ScriptedAdapter(FakeAdapter):
    """FakeAdapter that records translated requests and replays a script."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.requests: list[ChatCompletionRequest] = []
        self.chunk_script: list[ChatCompletionChunk] | None = None
        self.chat_output: ChatCompletionResponse | None = None
        #: Raise after the scripted chunks are exhausted (mid-flight failure).
        self.raise_after_script: BaseException | None = None

    async def chat(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> ChatCompletionResponse:
        self.requests.append(request)
        if self.chat_output is not None:
            return self.chat_output
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
        if self.raise_after_script is not None:
            raise self.raise_after_script
        self.streams_completed += 1
        self.streams_closed += 1


def _chunk(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    usage: Usage | None = None,
    role: str | None = None,
) -> ChatCompletionChunk:
    delta = ChunkDelta(role=role, content=content, tool_calls=tool_calls)
    return ChatCompletionChunk(
        model="fake-model",
        choices=[
            ChatCompletionChunkChoice(delta=delta, finish_reason=finish_reason)
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
                try:
                    data = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    data = {}
        if name is not None and data is not None:
            events.append((name, data))
    return events


def _payloads(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [data for event, data in events if event == name]


@pytest.fixture
async def responses_api(
    provider_config,
) -> AsyncIterator[tuple[httpx.AsyncClient, ScriptedAdapter, Any]]:
    """ASGI client + scripted adapter on the plain (non-alias) model."""
    adapter = ScriptedAdapter(provider_config)
    config = make_config(
        providers=[provider_config],
        models=[make_model("fake-model", capabilities={"coding": 6.0})],
        aliases=[make_alias("zk-test", ["fake-model"])],
        admin_token="test-admin-token",
    )
    harness = await build_harness(config, adapters={"fake": adapter})
    # Pin the switch the local .env would otherwise leak in (this machine sets
    # ZKAI_STRIP_REASONING=true); individual tests flip it as needed.
    harness.container.settings.strip_reasoning = False
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
        yield client, adapter, harness
    await harness.container.shutdown()


CODEX_HEAD: dict[str, Any] = {
    "instructions": "You are a coding agent running in the Codex CLI.",
    "tools": [
        {
            "type": "function",
            "name": "exec_command",
            "description": "Runs a command in a PTY.",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        }
    ],
    "tool_choice": "auto",
    "parallel_tool_calls": True,
    "reasoning": {"effort": "medium", "summary": "auto"},
    "store": False,
    "include": ["reasoning.encrypted_content"],
    "prompt_cache_key": "01a0bdcb",
}


# --------------------------------------------------------------------------- #
# Request translation
# --------------------------------------------------------------------------- #
async def test_flat_tools_become_chat_tools(responses_api) -> None:
    client, adapter, _harness = responses_api
    adapter.chat_output = ChatCompletionResponse.simple(model="fake-model", content="done")
    body = {
        "model": "fake-model",
        "input": [{"type": "message", "role": "user", "content": "list files"}],
        **CODEX_HEAD,
    }
    response = await client.post("/v1/responses", json=body)
    assert response.status_code == 200

    sent = adapter.requests[0]
    assert sent.tools == [
        {
            "type": "function",
            "function": {
                "name": "exec_command",
                "description": "Runs a command in a PTY.",
                "parameters": CODEX_HEAD["tools"][0]["parameters"],
            },
        }
    ]
    assert sent.tool_choice == "auto"
    assert sent.parallel_tool_calls is True
    assert sent.messages[0].role == "system"  # instructions
    assert sent.extra_params()["reasoning_effort"] == "medium"


async def test_function_call_items_become_tool_round_trip(responses_api) -> None:
    client, adapter, _harness = responses_api
    adapter.chat_output = ChatCompletionResponse.simple(model="fake-model", content="ok")
    body = {
        "model": "fake-model",
        "input": [
            {"type": "message", "role": "user", "content": "say hi"},
            {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd": "echo hi"}',
                "call_id": "call_01",
            },
            {"type": "function_call_output", "call_id": "call_01", "output": "hi\n"},
            {"type": "message", "role": "user", "content": "and now?"},
            # Items with no chat equivalent must be dropped, not mangled.
            {"type": "reasoning", "summary": []},
            {"type": "web_search_call", "id": "ws_1"},
        ],
        **CODEX_HEAD,
    }
    response = await client.post("/v1/responses", json=body)
    assert response.status_code == 200

    roles = [(m.role, m.tool_call_id) for m in adapter.requests[0].messages]
    assert roles == [
        ("system", None),
        ("user", None),
        ("assistant", None),
        ("tool", "call_01"),
        ("user", None),
    ]
    assistant = adapter.requests[0].messages[2]
    assert assistant.tool_calls[0].id == "call_01"
    assert assistant.tool_calls[0].function.name == "exec_command"
    assert assistant.tool_calls[0].function.arguments == '{"cmd": "echo hi"}'
    assert adapter.requests[0].messages[3].content == "hi\n"


async def test_developer_role_maps_to_system(responses_api) -> None:
    client, adapter, _harness = responses_api
    adapter.chat_output = ChatCompletionResponse.simple(model="fake-model", content="ok")
    body = {
        "model": "fake-model",
        "input": [{"type": "message", "role": "developer", "content": "be terse"}],
        **CODEX_HEAD,
    }
    await client.post("/v1/responses", json=body)
    assert adapter.requests[0].messages[-1].role == "system"


async def test_non_streaming_output_carries_function_call(responses_api) -> None:
    client, adapter, _harness = responses_api
    adapter.chat_output = ChatCompletionResponse.simple(
        model="fake-model",
        content="running it",
        finish_reason="tool_calls",
        tool_calls=[
            ToolCall(
                id="call_42",
                function={"name": "exec_command", "arguments": '{"cmd": "ls"}'},
            )
        ],
        usage=Usage.build(10, 5),
    )
    body = {
        "model": "fake-model",
        "input": [{"type": "message", "role": "user", "content": "list files"}],
        **CODEX_HEAD,
    }
    response = await client.post("/v1/responses", json=body)
    assert response.status_code == 200
    payload = response.json()

    assert payload["output_text"] == "running it"
    kinds = [item["type"] for item in payload["output"]]
    assert kinds == ["message", "function_call"]
    call_item = payload["output"][1]
    assert call_item["call_id"] == "call_42"
    assert call_item["name"] == "exec_command"
    assert json.loads(call_item["arguments"]) == {"cmd": "ls"}


# --------------------------------------------------------------------------- #
# Streaming grammar
# --------------------------------------------------------------------------- #
async def test_streaming_announces_items_before_deltas(responses_api) -> None:
    """The Codex regression: no delta may precede its ``output_item.added``."""
    client, adapter, _harness = responses_api
    adapter.chunk_script = [
        _chunk(role="assistant"),
        _chunk(content="part0 "),
        _chunk(content="part1"),
        _chunk(finish_reason="stop", usage=Usage.build(10, 5)),
    ]
    response = await client.post(
        "/v1/responses",
        json={"model": "fake-model", "input": "hello", "stream": True, **CODEX_HEAD},
    )
    assert response.status_code == 200
    events = parse_sse(response.text)
    names = [name for name, _data in events]

    # Canonical order, no exceptions.
    assert names[0] == "response.created"
    assert names[1] == "response.in_progress"
    assert names.index("response.output_item.added") < names.index(
        "response.output_text.delta"
    )
    assert names.index("response.content_part.added") < names.index(
        "response.output_text.delta"
    )
    assert names.index("response.output_text.done") < names.index(
        "response.content_part.done"
    )
    assert names.index("response.content_part.done") < names.index("response.output_item.done")
    assert names[-1] == "response.completed"

    # Deltas carry the item coordinates Codex correlates on.
    delta = _payloads(events, "response.output_text.delta")[0]
    assert delta["item_id"]
    assert delta["output_index"] == 0
    assert delta["content_index"] == 0
    assert "".join(
        item["delta"] for item in _payloads(events, "response.output_text.delta")
    ) == "part0 part1"

    # Every added item is closed exactly once.
    added = _payloads(events, "response.output_item.added")
    done = _payloads(events, "response.output_item.done")
    assert len(added) == len(done) == 1
    assert added[0]["item"]["type"] == "message"
    assert done[0]["item"]["status"] == "completed"
    assert done[0]["item"]["content"][0]["text"] == "part0 part1"

    # completed carries the full response object, including usage.
    completed = _payloads(events, "response.completed")[0]["response"]
    assert completed["status"] == "completed"
    assert completed["output"][0]["content"][0]["text"] == "part0 part1"
    assert completed["output_text"] == "part0 part1"
    assert completed["usage"]["input_tokens"] == 10
    assert completed["usage"]["output_tokens"] == 5
    assert completed["usage"]["total_tokens"] == 15


async def test_streaming_function_call_events(responses_api) -> None:
    client, adapter, _harness = responses_api
    adapter.chunk_script = [
        _chunk(role="assistant"),
        _chunk(
            tool_calls=[
                {
                    "id": "call_01",
                    "index": 0,
                    "function": {"name": "exec_command", "arguments": '{"cmd":'},
                }
            ]
        ),
        _chunk(tool_calls=[{"index": 0, "function": {"arguments": ' "ls"}'}}]),
        _chunk(finish_reason="tool_calls", usage=Usage.build(10, 5)),
    ]
    response = await client.post(
        "/v1/responses",
        json={"model": "fake-model", "input": "list files", "stream": True, **CODEX_HEAD},
    )
    assert response.status_code == 200
    events = parse_sse(response.text)
    names = [name for name, _data in events]

    assert names.index("response.output_item.added") < names.index(
        "response.function_call_arguments.delta"
    )
    added = _payloads(events, "response.output_item.added")[0]
    assert added["item"]["type"] == "function_call"
    assert added["item"]["name"] == "exec_command"
    assert added["item"]["call_id"] == "call_01"

    arguments = "".join(
        item["delta"] for item in _payloads(events, "response.function_call_arguments.delta")
    )
    assert json.loads(arguments) == {"cmd": "ls"}

    done = _payloads(events, "response.output_item.done")[0]["item"]
    assert done["type"] == "function_call"
    assert done["status"] == "completed"
    assert json.loads(done["arguments"]) == {"cmd": "ls"}

    completed = _payloads(events, "response.completed")[0]["response"]
    assert [item["type"] for item in completed["output"]] == ["function_call"]
    assert completed["output"][0]["call_id"] == "call_01"


async def test_streaming_mixed_text_and_tool_call(responses_api) -> None:
    """Text and a tool call in one turn: two items, both closed."""
    client, adapter, _harness = responses_api
    adapter.chunk_script = [
        _chunk(role="assistant"),
        _chunk(content="Let me look. "),
        _chunk(
            tool_calls=[
                {
                    "id": "call_07",
                    "index": 0,
                    "function": {"name": "exec_command", "arguments": '{"cmd": "pwd"}'},
                }
            ]
        ),
        _chunk(finish_reason="tool_calls", usage=Usage.build(10, 5)),
    ]
    response = await client.post(
        "/v1/responses",
        json={"model": "fake-model", "input": "where am i", "stream": True, **CODEX_HEAD},
    )
    events = parse_sse(response.text)
    added = _payloads(events, "response.output_item.added")
    done = _payloads(events, "response.output_item.done")
    assert [item["item"]["type"] for item in added] == ["message", "function_call"]
    assert [item["item"]["type"] for item in done] == ["message", "function_call"]
    assert added[0]["output_index"] == 0 and added[1]["output_index"] == 1

    completed = _payloads(events, "response.completed")[0]["response"]
    assert completed["output_text"] == "Let me look. "
    assert len(completed["output"]) == 2


async def test_streaming_error_emits_failed_then_error(responses_api) -> None:
    client, adapter, _harness = responses_api
    adapter.chunk_script = [
        _chunk(role="assistant"),
        _chunk(content="partial"),
    ]
    # A mid-flight upstream failure arrives as a scheduler error event.
    adapter.raise_after_script = RuntimeError("upstream died")
    response = await client.post(
        "/v1/responses",
        json={"model": "fake-model", "input": "hello", "stream": True, **CODEX_HEAD},
    )
    events = parse_sse(response.text)
    names = [name for name, _data in events]
    assert "response.failed" in names
    assert "error" in names
    assert names.index("response.failed") < names.index("error")
    failed = _payloads(events, "response.failed")[0]["response"]
    assert failed["status"] == "failed"


# --------------------------------------------------------------------------- #
# Recovered reasoning must not leak as the answer (2026-09-20 desktop bug)
# --------------------------------------------------------------------------- #
def _recovered_chunk(thinking: str) -> ChatCompletionChunk:
    """A chunk the base adapter's never-blank guard has already rewritten.

    The adapter promotes a reasoning-only delta into ``content`` and marks it
    ``content_recovered_from_reasoning``. The Responses surface must honour the
    strip switch for that text exactly like /v1/chat/completions does - without
    it, a thinking model's chain of thought streams to the client as the answer.
    """
    choice = ChatCompletionChunkChoice(delta=ChunkDelta(reasoning_content=thinking))
    choice.recover_reasoning_only_delta()
    return ChatCompletionChunk(model="fake-model", choices=[choice])


async def test_streaming_recovered_reasoning_is_stripped(responses_api) -> None:
    client, adapter, harness = responses_api
    harness.container.settings.strip_reasoning = True
    adapter.chunk_script = [
        _chunk(role="assistant"),
        _recovered_chunk("thinking very hard"),
        _chunk(finish_reason="stop", usage=Usage.build(10, 5)),
    ]
    response = await client.post(
        "/v1/responses",
        json={"model": "fake-model", "input": "hello", "stream": True, **CODEX_HEAD},
    )
    events = parse_sse(response.text)
    assert _payloads(events, "response.output_text.delta") == []
    completed = _payloads(events, "response.completed")[0]["response"]
    assert completed["output"] == []


async def test_streaming_recovered_reasoning_kept_when_not_stripping(responses_api) -> None:
    client, adapter, harness = responses_api
    harness.container.settings.strip_reasoning = False
    adapter.chunk_script = [
        _chunk(role="assistant"),
        _recovered_chunk("thinking very hard"),
        _chunk(finish_reason="stop", usage=Usage.build(10, 5)),
    ]
    response = await client.post(
        "/v1/responses",
        json={"model": "fake-model", "input": "hello", "stream": True, **CODEX_HEAD},
    )
    events = parse_sse(response.text)
    text = "".join(item["delta"] for item in _payloads(events, "response.output_text.delta"))
    assert text == "thinking very hard"


async def test_non_streaming_recovered_reasoning_is_stripped(responses_api) -> None:
    client, adapter, harness = responses_api
    harness.container.settings.strip_reasoning = True
    adapter.chat_output = ChatCompletionResponse.simple(
        model="fake-model",
        content="thinking very hard",
        finish_reason="length",
        usage=Usage.build(10, 5),
    )
    adapter.chat_output.choices[0].message.__pydantic_extra__ = {
        "content_recovered_from_reasoning": True
    }
    response = await client.post("/v1/responses", json={"model": "fake-model", "input": "hello"})
    body = response.json()
    assert body["output_text"] == ""
    assert body["output"] == []
