"""``POST /v1/messages`` - the Anthropic Messages API entry point.

Why this exists
---------------
Claude Code speaks the Anthropic protocol, and CC Switch's ``openai_chat``
translation never emits ``content_block_stop`` — the Anthropic SDK then refuses
to finalise the text block and the reply arrives empty (diagnosed 2026-09-13).
Serving the native protocol lets Anthropic-format clients consume the same
routing / key-pool / accounting core as ``/v1/chat/completions``.

Compatibility notes
-------------------
* Streaming follows the real Anthropic event grammar: ``message_start``,
  ``content_block_start``, ``content_block_delta``, **``content_block_stop``**,
  ``message_delta``, ``message_stop``. Every opened block is always closed —
  that is the invariant the CC Switch translation was missing.
* ``reasoning_content`` deltas become ``thinking`` blocks unless
  ``ZKAI_STRIP_REASONING`` is set; a reasoning-only answer that was promoted
  into ``content`` (never-blank guard) is blanked under the strip switch,
  mirroring ``/v1/chat/completions``.
* Errors use the Anthropic envelope ``{"type": "error", "error": {...}}``.
* ``thinking`` / ``context_management`` / ``metadata`` are accepted and ignored.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.chat import _aclose, _watch_disconnect, retry_after_headers, routing_headers
from app.api.deps import ContainerDep, client_metadata, get_container
from app.core.errors import ZKAIError
from app.core.logging import get_logger
from app.core.security import constant_time_equals
from app.models.anthropic import AnthropicMessagesRequest, strip_context_suffix
from app.models.request import ChatCompletionRequest, new_request_id
from app.models.response import ChatCompletionChunk, ChatCompletionResponse

logger = get_logger("api.messages")

#: OpenAI ``finish_reason`` -> Anthropic ``stop_reason``.
_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

#: Gateway ``error_type`` -> Anthropic ``error.type``.
_ANTHROPIC_ERROR_TYPES = {
    "invalid_request_error": "invalid_request_error",
    "unsupported_parameter": "invalid_request_error",
    "context_length_exceeded": "invalid_request_error",
    "content_filter": "invalid_request_error",
    "authentication_error": "authentication_error",
    "credential_error": "authentication_error",
    "no_available_credential": "authentication_error",
    "permission_denied": "permission_error",
    "model_not_found": "not_found_error",
    "alias_not_found": "not_found_error",
    "provider_not_found": "not_found_error",
    "rate_limit_error": "rate_limit_error",
    "overloaded": "overloaded_error",
}


async def require_anthropic_auth(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Anthropic-flavoured sibling of :func:`app.api.deps.require_client_auth`.

    Anthropic SDKs authenticate with ``x-api-key`` (Claude Code with
    ``ANTHROPIC_AUTH_TOKEN`` may send ``Authorization: Bearer`` instead); both
    are accepted against ``ZKAI_API_TOKEN``. Unset => open, as everywhere else.
    """
    container = get_container(request)
    expected = container.settings.api_token
    if not expected:
        return
    provided = x_api_key
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not constant_time_equals(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid or missing api key"},
            },
        )


def anthropic_error_response(exc: ZKAIError) -> JSONResponse:
    """Serialise a gateway error into the Anthropic error envelope."""
    error_type = _ANTHROPIC_ERROR_TYPES.get(exc.error_type, "api_error")
    return JSONResponse(
        status_code=exc.http_status,
        content={"type": "error", "error": {"type": error_type, "message": exc.message}},
        headers=retry_after_headers(exc),
    )


router = APIRouter(tags=["messages"], dependencies=[Depends(require_anthropic_auth)])


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class _StreamAssembler:
    """Turn canonical ``ChatCompletionChunk`` events into Anthropic SSE.

    State machine over content blocks: at most one ``thinking`` and one
    ``text`` block, one ``tool_use`` block per parallel tool call. Every block
    opened here is closed exactly once in :meth:`finish` — the missing
    ``content_block_stop`` is precisely the failure mode this endpoint exists
    to avoid.
    """

    def __init__(self, model: str, *, strip_reasoning: bool) -> None:
        self.model = model
        self.strip_reasoning = strip_reasoning
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self._next_index = 0
        #: block index -> block kind, in opening order (closed in the same order).
        self._open_blocks: list[tuple[int, str]] = []
        self._payloads: dict[int, dict[str, Any]] = {}
        self._singleton_blocks: dict[str, int] = {}
        self._tool_blocks: dict[int, int] = {}
        self._stop_reason: str | None = None
        self._usage: tuple[int, int] | None = None

    def start(self) -> str:
        message_start = {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
        return _sse("message_start", message_start) + _sse("ping", {"type": "ping"})

    def feed_chunk(self, chunk: ChatCompletionChunk) -> list[str]:
        out: list[str] = []
        if chunk.usage is not None:
            self._usage = (chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
        if not chunk.choices:
            return out
        choice = chunk.choices[0]
        delta = choice.delta
        extra = delta.model_extra or {}

        thinking = delta.thinking
        if thinking and not self.strip_reasoning:
            index, opening = self._ensure_singleton("thinking", {"type": "thinking"})
            out.extend(opening)
            out.append(self._delta_event(index, {"type": "thinking_delta", "thinking": thinking}))

        content = delta.content
        recovered = bool(extra.get("content_recovered_from_reasoning"))
        if content and not (recovered and self.strip_reasoning):
            index, opening = self._ensure_singleton("text", {"type": "text", "text": ""})
            out.extend(opening)
            out.append(self._delta_event(index, {"type": "text_delta", "text": content}))

        for call in delta.tool_calls or []:
            key = call.index if call.index is not None else len(self._tool_blocks)
            index, opening = self._ensure_tool_block(key, call)
            out.extend(opening)
            if call.function.arguments:
                out.append(
                    self._delta_event(
                        index, {"type": "input_json_delta", "partial_json": call.function.arguments}
                    )
                )

        if choice.finish_reason:
            self._stop_reason = _STOP_REASONS.get(choice.finish_reason, "end_turn")
        return out

    def set_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self._usage = (prompt_tokens, completion_tokens)

    def finish(self) -> str:
        """Close every open block, then ``message_delta`` + ``message_stop``."""
        out: list[str] = []
        for index, _kind in self._open_blocks:
            out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
        self._open_blocks.clear()
        self._singleton_blocks.clear()
        self._tool_blocks.clear()
        usage = {
            "input_tokens": self._usage[0] if self._usage else 0,
            "output_tokens": self._usage[1] if self._usage else 0,
        }
        out.append(
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": self._stop_reason or "end_turn",
                        "stop_sequence": None,
                    },
                    "usage": usage,
                },
            )
        )
        out.append(_sse("message_stop", {"type": "message_stop"}))
        return "".join(out)

    def error_event(self, envelope: dict[str, Any]) -> str:
        error = envelope.get("error", envelope)
        return _sse(
            "error",
            {
                "type": "error",
                "error": {
                    "type": _ANTHROPIC_ERROR_TYPES.get(str(error.get("type", "")), "api_error"),
                    "message": str(error.get("message", "upstream error")),
                },
            },
        )

    # ------------------------------------------------------------------ #
    def _delta_event(self, index: int, delta: dict[str, Any]) -> str:
        return _sse(
            "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}
        )

    def _open_block(self, kind: str, payload: dict[str, Any]) -> tuple[int, str]:
        index = self._next_index
        self._next_index += 1
        self._open_blocks.append((index, kind))
        self._payloads[index] = payload
        start = _sse(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": payload},
        )
        return index, start

    def _ensure_singleton(self, kind: str, payload: dict[str, Any]) -> tuple[int, list[str]]:
        index = self._singleton_blocks.get(kind)
        if index is not None:
            return index, []
        index, start = self._open_block(kind, payload)
        self._singleton_blocks[kind] = index
        return index, [start]

    def _ensure_tool_block(self, key: int, call: Any) -> tuple[int, list[str]]:
        index = self._tool_blocks.get(key)
        if index is not None:
            return index, []
        payload = {
            "type": "tool_use",
            "id": call.id or f"toolu_{uuid.uuid4().hex[:24]}",
            "name": call.function.name,
        }
        index, start = self._open_block("tool_use", payload)
        self._tool_blocks[key] = index
        return index, [start]


@router.post("/v1/messages", summary="Create an Anthropic Messages response")
async def create_message(
    payload: AnthropicMessagesRequest,
    container: ContainerDep,
    request: Request,
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
) -> Any:
    """Route, execute and account for an Anthropic Messages call."""
    request_id = x_request_id or new_request_id()
    metadata = client_metadata(request)
    chat_request = payload.to_chat_request(
        default_model=container.settings.anthropic_default_model,
        fallback_max_tokens=container.settings.default_max_tokens,
    )

    if chat_request.stream:
        return await _stream_response(chat_request, payload.model, container, request_id, metadata, request)
    return await _json_response(chat_request, payload.model, container, request_id, metadata)


@router.post(
    "/v1/messages/count_tokens",
    summary="Estimate input tokens for a Messages payload",
)
async def count_tokens(payload: AnthropicMessagesRequest, container: ContainerDep) -> dict[str, int]:
    """Cheap heuristic count; exact upstream tokenisation is never consulted."""
    return {
        "input_tokens": payload.estimate_input_tokens(
            default_model=container.settings.anthropic_default_model,
            fallback_max_tokens=container.settings.default_max_tokens,
        )
    }


async def _json_response(
    chat_request: ChatCompletionRequest,
    requested_model: str,
    container: ContainerDep,
    request_id: str,
    metadata: dict[str, str | None],
) -> Any:
    try:
        response = await container.request_service.chat(
            chat_request,
            request_id=request_id,
            client_ip=metadata.get("client_ip"),
            user_agent=metadata.get("user_agent"),
        )
    except ZKAIError as exc:
        logger.warning(
            "messages request %s failed: %s (%s)", request_id, exc.error_type, exc.message
        )
        return anthropic_error_response(exc)

    headers = routing_headers(response.zk_ai.model_dump()) if response.zk_ai else {}
    return JSONResponse(
        status_code=200,
        content=_message_payload(response, requested_model, container),
        headers=headers,
    )


def _message_payload(
    response: ChatCompletionResponse, requested_model: str, container: ContainerDep
) -> dict[str, Any]:
    """Non-streaming ``ChatCompletionResponse`` -> Anthropic ``Message``."""
    strip = container.settings.strip_reasoning
    content_blocks: list[dict[str, Any]] = []
    stop_reason = "end_turn"
    choice = response.choices[0] if response.choices else None

    if choice is not None:
        message = choice.message
        extra = message.model_extra or {}
        thinking = None
        for key in ("reasoning_content", "reasoning"):
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                thinking = value
                break
        if thinking and not strip:
            content_blocks.append({"type": "thinking", "thinking": thinking})
        text = message.text()
        if text:
            content_blocks.append({"type": "text", "text": text})
        for call in message.tool_calls or []:
            try:
                tool_input: Any = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                tool_input = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.function.name,
                    "input": tool_input if isinstance(tool_input, dict) else {},
                }
            )
        stop_reason = _STOP_REASONS.get(choice.finish_reason or "", "end_turn")

    usage = response.usage
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": strip_context_suffix(requested_model) or response.model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "cache_read_input_tokens": usage.cached_tokens,
        },
    }


async def _stream_response(
    chat_request: ChatCompletionRequest,
    requested_model: str,
    container: ContainerDep,
    request_id: str,
    metadata: dict[str, str | None],
    request: Request,
) -> Any:
    """Prefetch the first event (real HTTP codes for pre-flight errors), then stream."""
    generator = container.request_service.stream(
        chat_request,
        request_id=request_id,
        client_ip=metadata.get("client_ip"),
        user_agent=metadata.get("user_agent"),
    )
    iterator = generator.__aiter__()
    try:
        first_event = await anext(iterator)
    except StopAsyncIteration:
        first_event = None
    except ZKAIError as exc:
        await _aclose(generator)
        logger.warning("messages stream %s rejected: %s", request_id, exc.error_type)
        return anthropic_error_response(exc)
    except asyncio.CancelledError:
        await _aclose(generator)
        raise

    assembler = _StreamAssembler(
        strip_context_suffix(requested_model) or chat_request.model,
        strip_reasoning=container.settings.strip_reasoning,
    )
    headers = {
        "x-zkai-request-id": request_id,
        "x-zkai-stream": "true",
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
    }

    async def event_stream() -> AsyncIterator[str]:
        watcher = asyncio.create_task(_watch_disconnect(request))
        try:
            yield assembler.start()
            event = first_event
            while True:
                if event is None:
                    break
                if event.type == "chunk" and event.chunk is not None:
                    for piece in assembler.feed_chunk(event.chunk):
                        yield piece
                elif event.type == "usage" and event.usage is not None:
                    assembler.set_usage(event.usage.prompt_tokens, event.usage.completion_tokens)
                elif event.type == "error" and event.error is not None:
                    yield assembler.error_event(event.error)
                    return
                elif event.type == "end":
                    break

                next_task: asyncio.Future[Any] = asyncio.ensure_future(anext(iterator))
                done, _ = await asyncio.wait(
                    {next_task, watcher}, return_when=asyncio.FIRST_COMPLETED
                )
                if next_task not in done:
                    next_task.cancel()
                    logger.info("client disconnected during messages stream %s", request_id)
                    return
                try:
                    event = next_task.result()
                except StopAsyncIteration:
                    break
            # Invariant of this endpoint: blocks are always closed, even when
            # the upstream produced nothing but silence.
            yield assembler.finish()
        finally:
            watcher.cancel()
            await _aclose(generator)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


__all__ = [
    "anthropic_error_response",
    "count_tokens",
    "create_message",
    "require_anthropic_auth",
    "router",
]
