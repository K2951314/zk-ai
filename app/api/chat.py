"""``POST /v1/chat/completions`` - the OpenAI-compatible entry point.

Compatibility notes
-------------------
* The request model accepts **every** parameter an OpenAI SDK may send; unknown
  ones are recorded and (only when a provider can map them) forwarded. Nothing is
  ever dropped silently.
* Streaming uses pure ``data:`` chunks terminated by ``data: [DONE]`` so the
  official SDKs parse it unchanged. Routing metadata travels in an SSE
  **comment** (``: zkai-meta {...}``) plus response headers, which every SSE
  parser ignores - it can never corrupt a client's chunk stream.
* ``usage`` is only included in the final chunk when ``stream_options.include_usage``
  is set, exactly like OpenAI.
* Client disconnects cancel the upstream request and are recorded as ``cancelled``.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import ContainerDep, client_metadata, require_client_auth
from app.core.errors import ZKAIError
from app.core.logging import get_logger
from app.models.request import ChatCompletionRequest, new_request_id
from app.models.response import ChatCompletionChunk

logger = get_logger("api.chat")

router = APIRouter(tags=["chat"], dependencies=[Depends(require_client_auth)])

SSE_DONE = "data: [DONE]\n\n"


_REASONING_KEYS = ("reasoning_content", "reasoning", "content_recovered_from_reasoning")


def _strip_reasoning_fields(payload: dict[str, Any]) -> None:
    """Drop thinking fields from a serialized response/chunk *when a real answer
    exists*. If content is empty the thinking has already been promoted into it
    (never a blank reply), so we only strip when text is present.
    """
    for choice in payload.get("choices") or []:
        for section in ("delta", "message"):
            body = choice.get(section)
            if isinstance(body, dict) and body.get("content"):
                for key in _REASONING_KEYS:
                    body.pop(key, None)


def _chunk_payload(
    chunk: ChatCompletionChunk, *, include_usage: bool, strip_reasoning: bool = False
) -> dict[str, Any]:
    payload = chunk.model_dump(exclude_none=True)
    if not include_usage:
        payload.pop("usage", None)
    if strip_reasoning:
        _strip_reasoning_fields(payload)
    return payload


def retry_after_headers(exc: ZKAIError) -> dict[str, str]:
    """``Retry-After`` for throttled / pool-blackout errors, if known.

    A client that hammers a pool in cooldown every 2 seconds only makes the
    blackout worse; the header (paired with ``error.retry_after`` in the body)
    gives well-behaved SDKs something to back off by.
    """
    retry_after = getattr(exc, "retry_after", None)
    if not retry_after or retry_after <= 0:
        return {}
    return {"retry-after": str(math.ceil(retry_after))}


def error_response(exc: ZKAIError) -> JSONResponse:
    """Serialise a gateway error into an OpenAI-shaped error envelope."""
    return JSONResponse(
        status_code=exc.http_status, content=exc.to_dict(), headers=retry_after_headers(exc)
    )


def routing_headers(response_meta: dict[str, Any]) -> dict[str, str]:
    """Headers that expose routing decisions without touching the body."""
    headers = {"x-zkai-request-id": str(response_meta.get("request_id", ""))}
    for key, header in (
        ("provider", "x-zkai-provider"),
        ("credential_id", "x-zkai-credential"),
        ("alias", "x-zkai-alias"),
        ("resolved_model", "x-zkai-model"),
        ("attempt", "x-zkai-attempt"),
    ):
        value = response_meta.get(key)
        if value is not None:
            headers[header] = str(value)
    if response_meta.get("fallback_used"):
        headers["x-zkai-fallback"] = "true"
    return headers


@router.post("/v1/chat/completions", summary="Create a chat completion")
async def chat_completions(
    payload: ChatCompletionRequest,
    container: ContainerDep,
    request: Request,
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
) -> Any:
    """Route, execute and account for a chat completion."""
    request_id = x_request_id or new_request_id()
    metadata = client_metadata(request)

    if payload.stream:
        return await _stream_response(payload, container, request_id, metadata, request)
    return await _json_response(payload, container, request_id, metadata)


async def _json_response(
    payload: ChatCompletionRequest,
    container: ContainerDep,
    request_id: str,
    metadata: dict[str, str | None],
) -> Any:
    try:
        response = await container.request_service.chat(
            payload,
            request_id=request_id,
            client_ip=metadata.get("client_ip"),
            user_agent=metadata.get("user_agent"),
        )
    except ZKAIError as exc:
        logger.warning("request %s failed: %s (%s)", request_id, exc.error_type, exc.message)
        return error_response(exc)

    headers = routing_headers(response.zk_ai.model_dump()) if response.zk_ai else {}
    body = response.model_dump(exclude_none=True)
    if container.settings.strip_reasoning:
        _strip_reasoning_fields(body)
    return JSONResponse(status_code=200, content=body, headers=headers)


async def _stream_response(
    payload: ChatCompletionRequest,
    container: ContainerDep,
    request_id: str,
    metadata: dict[str, str | None],
    request: Request,
) -> Any:
    """Prefetch the first event (so pre-flight errors return real HTTP codes), then stream."""
    generator = container.request_service.stream(
        payload,
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
        logger.warning("stream request %s rejected: %s", request_id, exc.error_type)
        return error_response(exc)
    except asyncio.CancelledError:
        await _aclose(generator)
        raise

    include_usage = bool((payload.stream_options or {}).get("include_usage"))
    strip_reasoning = container.settings.strip_reasoning
    headers = {
        "x-zkai-request-id": request_id,
        "x-zkai-stream": "true",
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
    }

    async def event_stream() -> AsyncIterator[str]:
        watcher = asyncio.create_task(_watch_disconnect(request))
        try:
            event = first_event
            while True:
                if event is None:
                    break
                if event.type == "chunk" and event.chunk is not None:
                    payload_json = json.dumps(
                        _chunk_payload(
                            event.chunk,
                            include_usage=include_usage,
                            strip_reasoning=strip_reasoning,
                        ),
                        ensure_ascii=False,
                    )
                    yield f"data: {payload_json}\n\n"
                elif event.type == "meta" and event.meta is not None:
                    # SSE comment: invisible to OpenAI SDK parsers by design.
                    yield f": zkai-meta {json.dumps(event.meta.model_dump(), ensure_ascii=False)}\n\n"
                elif event.type == "error" and event.error is not None:
                    yield f"data: {json.dumps(event.error, ensure_ascii=False)}\n\n"
                elif event.type == "end":
                    break

                # ``anext`` returns an Awaitable, not a Coroutine, so wrap it with
                # ensure_future (create_task would reject it).
                next_task: asyncio.Future[Any] = asyncio.ensure_future(anext(iterator))
                done, _ = await asyncio.wait(
                    {next_task, watcher}, return_when=asyncio.FIRST_COMPLETED
                )
                if next_task not in done:
                    # Client vanished: cancel upstream and stop producing.
                    next_task.cancel()
                    logger.info("client disconnected during stream %s", request_id)
                    break
                try:
                    event = next_task.result()
                except StopAsyncIteration:
                    break
            yield SSE_DONE
        finally:
            watcher.cancel()
            await _aclose(generator)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


async def _watch_disconnect(request: Request, interval: float = 0.25) -> None:
    """Complete as soon as the client goes away."""
    while True:
        if await request.is_disconnected():
            return
        await asyncio.sleep(interval)


async def _aclose(generator: Any) -> None:
    """Close an async generator without masking the original error."""
    aclose = getattr(generator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        logger.debug("error closing stream generator", exc_info=True)


__all__ = ["SSE_DONE", "error_response", "retry_after_headers", "router", "routing_headers"]
