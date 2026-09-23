"""``POST /v1/responses`` - OpenAI Responses API surface, chat-backed.

ZK-AI implements the Responses API by translating it onto a chat completion
(``instructions`` -> system message, ``input`` items -> user/assistant/tool
turns, flat ``tools`` -> nested chat tools). The streaming variant emits the
full Responses event grammar so agent clients (Codex CLI 0.154+ speaks *only*
this wire) can drive a tool loop:

    response.created
    response.in_progress
    response.output_item.added          <- one per output item
    response.content_part.added         <- text items only
    response.output_text.delta          <- text deltas
    response.function_call_arguments.delta
    response.output_text.done / response.function_call_arguments.done
    response.content_part.done / response.output_item.done
    response.completed                  <- full response object + usage

Stateful conversations and hosted tools (web_search, file_search, ...) are out
of scope: the gateway is stateless and always receives the whole transcript.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.chat import _aclose, error_response, routing_headers
from app.api.deps import ContainerDep, client_metadata, require_client_auth
from app.core.errors import ZKAIError
from app.core.logging import get_logger
from app.models.request import new_request_id
from app.models.response import (
    ChatCompletionChunk,
    ResponsesRequest,
    ResponsesUsage,
    message_output_item,
)

logger = get_logger("api.responses")

router = APIRouter(tags=["responses"], dependencies=[Depends(require_client_auth)])


@router.post("/v1/responses", summary="创建一次响应（Responses API 子集）")
async def create_response(
    payload: ResponsesRequest,
    container: ContainerDep,
    request: Request,
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
) -> Any:
    """Serve a Responses API call (chat-backed)."""
    request_id = x_request_id or f"resp_{new_request_id()[9:]}"
    metadata = client_metadata(request)

    if payload.stream:
        return await _stream_response(payload, container, request_id, metadata)

    try:
        response = await container.request_service.responses(
            payload,
            request_id=request_id,
            client_ip=metadata.get("client_ip"),
            user_agent=metadata.get("user_agent"),
            strip_reasoning=container.settings.strip_reasoning,
        )
    except ZKAIError as exc:
        logger.warning("responses call %s failed: %s", request_id, exc.error_type)
        return error_response(exc)

    headers = routing_headers(response.zk_ai.model_dump()) if response.zk_ai else {}
    return JSONResponse(
        status_code=200, content=response.model_dump(exclude_none=True), headers=headers
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class _ResponsesAssembler:
    """Canonical chat stream -> Responses SSE event grammar.

    Every output item is announced with ``response.output_item.added`` *before*
    its first delta - Codex rejects a ``response.output_text.delta`` that
    arrives with no active item ("OutputTextDelta without active item") - and
    closed with ``response.output_item.done`` before ``response.completed``.
    ``response.completed`` carries the full response object (output items +
    usage), which is where clients harvest the final tool calls.
    """

    def __init__(self, model: str, *, response_id: str, strip_reasoning: bool = False) -> None:
        self.model = model
        self.response_id = response_id
        #: When true, thinking text that the never-blank guard promoted into
        #: ``content`` (``content_recovered_from_reasoning``) is dropped, same
        #: contract as /v1/chat/completions and /v1/messages.
        self.strip_reasoning = strip_reasoning
        self._sequence = 0
        self._output_index = 0
        #: The single text item (``None`` until the first content delta).
        self._message: dict[str, Any] | None = None
        #: tool-call key -> open item state, in opening order.
        self._calls: dict[int, dict[str, Any]] = {}
        self._usage: ResponsesUsage | None = None
        self._finish_reason: str | None = None
        self._zk_ai: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # Event plumbing
    # ------------------------------------------------------------------ #
    def _event(self, name: str, **data: Any) -> str:
        self._sequence += 1
        payload: dict[str, Any] = {**data, "type": name, "sequence_number": self._sequence}
        return _sse(name, payload)

    def _response_object(self, *, status: str, output: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": self.model,
            "output": output,
        }
        if self._usage is not None:
            payload["usage"] = self._usage.model_dump()
        if self._zk_ai is not None:
            payload["zk_ai"] = self._zk_ai
        return payload

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> str:
        created = self._response_object(status="in_progress", output=[])
        return self._event("response.created", response=created) + self._event(
            "response.in_progress", response=created
        )

    def feed_chunk(self, chunk: ChatCompletionChunk) -> list[str]:
        out: list[str] = []
        if chunk.usage is not None:
            self._usage = ResponsesUsage(
                input_tokens=chunk.usage.prompt_tokens,
                output_tokens=chunk.usage.completion_tokens,
                total_tokens=chunk.usage.total_tokens,
            )
        if chunk.model:
            self.model = chunk.model
        if not chunk.choices:
            return out
        choice = chunk.choices[0]
        if choice.finish_reason:
            self._finish_reason = choice.finish_reason
        delta = choice.delta
        extra = delta.model_extra or {}
        content = delta.content
        recovered = bool(extra.get("content_recovered_from_reasoning"))
        if content and not (recovered and self.strip_reasoning):
            state, opening = self._ensure_message()
            out.extend(opening)
            out.append(
                self._event(
                    "response.output_text.delta",
                    item_id=state["item_id"],
                    output_index=state["output_index"],
                    content_index=0,
                    delta=content,
                )
            )
            state["text"].append(content)
        for call in delta.tool_calls or []:
            key = call.index if call.index is not None else len(self._calls)
            call_state = self._calls.get(key)
            if call_state is None:
                call_state = self._open_call(call)
                out.append(
                    self._event(
                        "response.output_item.added",
                        output_index=call_state["output_index"],
                        item={
                            "id": call_state["item_id"],
                            "type": "function_call",
                            "status": "in_progress",
                            "arguments": "",
                            "call_id": call_state["call_id"],
                            "name": call_state["name"],
                        },
                    )
                )
            if call.function.arguments:
                out.append(
                    self._event(
                        "response.function_call_arguments.delta",
                        item_id=call_state["item_id"],
                        output_index=call_state["output_index"],
                        delta=call.function.arguments,
                    )
                )
                call_state["arguments"].append(call.function.arguments)
        return out

    def set_routing_meta(self, meta: Any) -> None:
        self._zk_ai = meta.model_dump()

    def set_usage(self, prompt_tokens: int, completion_tokens: int, total_tokens: int) -> None:
        self._usage = ResponsesUsage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    def finish(self) -> str:
        """Close every open item, then emit ``response.completed``."""
        out: list[str] = []
        output: list[dict[str, Any]] = []
        message = self._message
        if message is not None:
            text = "".join(message["text"])
            out.append(
                self._event(
                    "response.output_text.done",
                    item_id=message["item_id"],
                    output_index=message["output_index"],
                    content_index=0,
                    text=text,
                )
            )
            out.append(
                self._event(
                    "response.content_part.done",
                    item_id=message["item_id"],
                    output_index=message["output_index"],
                    content_index=0,
                    part={"type": "output_text", "text": text, "annotations": []},
                )
            )
            item = message_output_item(text, item_id=message["item_id"])
            out.append(
                self._event(
                    "response.output_item.done",
                    output_index=message["output_index"],
                    item=item,
                )
            )
            output.append(item)
        for state in self._calls.values():
            arguments = "".join(state["arguments"])
            out.append(
                self._event(
                    "response.function_call_arguments.done",
                    item_id=state["item_id"],
                    output_index=state["output_index"],
                    arguments=arguments,
                )
            )
            item = {
                "id": state["item_id"],
                "type": "function_call",
                "status": "completed",
                "arguments": arguments,
                "call_id": state["call_id"],
                "name": state["name"],
            }
            out.append(
                self._event("response.output_item.done", output_index=state["output_index"], item=item)
            )
            output.append(item)
        completed = self._response_object(status="completed", output=output)
        if message is not None:
            completed["output_text"] = "".join(message["text"])
        out.append(self._event("response.completed", response=completed))
        return "".join(out)

    def failed(self, envelope: dict[str, Any]) -> str:
        """``response.failed`` + ``error`` for a stream that died mid-flight."""
        error = envelope.get("error", envelope)
        code = str(error.get("type", "server_error"))
        message = str(error.get("message", "upstream error"))
        failure = {
            "id": self.response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "failed",
            "model": self.model,
            "output": [],
            "error": {"code": code, "message": message},
        }
        return self._event("response.failed", response=failure) + self._event(
            "error", code=code, message=message, param=None
        )

    # ------------------------------------------------------------------ #
    # Item bookkeeping
    # ------------------------------------------------------------------ #
    def _ensure_message(self) -> tuple[dict[str, Any], list[str]]:
        if self._message is not None:
            return self._message, []
        item_id = f"msg_{uuid.uuid4().hex[:24]}"
        output_index = self._output_index
        self._output_index += 1
        state: dict[str, Any] = {
            "item_id": item_id,
            "output_index": output_index,
            "text": [],
        }
        self._message = state
        opening = self._event(
            "response.output_item.added",
            output_index=output_index,
            item={
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        )
        part = self._event(
            "response.content_part.added",
            item_id=item_id,
            output_index=output_index,
            content_index=0,
            part={"type": "output_text", "text": "", "annotations": []},
        )
        return state, [opening, part]

    def _open_call(self, call: Any) -> dict[str, Any]:
        call_id = call.id or f"call_{uuid.uuid4().hex[:12]}"
        state = {
            "item_id": f"fc_{uuid.uuid4().hex[:24]}",
            "call_id": call_id,
            "name": call.function.name,
            "output_index": self._output_index,
            "arguments": [],
        }
        self._output_index += 1
        key = call.index if call.index is not None else len(self._calls)
        self._calls[key] = state
        return state


async def _stream_response(
    payload: ResponsesRequest,
    container: ContainerDep,
    request_id: str,
    metadata: dict[str, str | None],
) -> Any:
    chat_payload = payload.to_chat_request()
    chat_payload.stream = True
    generator = container.request_service.stream(
        chat_payload,
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
        logger.warning("responses stream %s rejected: %s", request_id, exc.error_type)
        return error_response(exc)
    except asyncio.CancelledError:
        await _aclose(generator)
        raise

    assembler = _ResponsesAssembler(
        payload.model,
        response_id=request_id,
        strip_reasoning=container.settings.strip_reasoning,
    )
    headers = {
        "x-zkai-request-id": request_id,
        "x-zkai-stream": "true",
        "cache-control": "no-cache",
    }

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield assembler.start()
            event = first_event
            while event is not None:
                if event.type == "chunk" and event.chunk is not None:
                    for piece in assembler.feed_chunk(event.chunk):
                        yield piece
                elif event.type == "usage" and event.usage is not None:
                    assembler.set_usage(
                        event.usage.prompt_tokens,
                        event.usage.completion_tokens,
                        event.usage.total_tokens,
                    )
                elif event.type == "meta" and event.meta is not None:
                    assembler.set_routing_meta(event.meta)
                elif event.type == "error" and event.error is not None:
                    yield assembler.failed(event.error)
                    return
                elif event.type == "end":
                    break
                try:
                    event = await anext(iterator)
                except StopAsyncIteration:
                    break
            yield assembler.finish()
            yield "data: [DONE]\n\n"
        finally:
            await _aclose(generator)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


__all__ = ["_ResponsesAssembler", "create_response", "router"]
