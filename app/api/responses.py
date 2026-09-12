"""``POST /v1/responses`` - minimal OpenAI Responses API surface.

ZK-AI implements the Responses API by translating it onto a chat completion
(``instructions`` -> system message, ``input`` -> user/assistant turns). The
streaming variant emits Responses-style event names
(``response.created`` / ``response.output_text.delta`` / ``response.completed``).

This is intentionally a *subset*: stateful conversations and hosted tools are out
of scope for v1.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.chat import _aclose, error_response, routing_headers
from app.api.deps import ContainerDep, client_metadata, require_client_auth
from app.core.errors import ZKAIError
from app.core.logging import get_logger
from app.models.request import new_request_id
from app.models.response import ResponsesRequest

logger = get_logger("api.responses")

router = APIRouter(tags=["responses"], dependencies=[Depends(require_client_auth)])


@router.post("/v1/responses", summary="Create a response (Responses API subset)")
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
        )
    except ZKAIError as exc:
        logger.warning("responses call %s failed: %s", request_id, exc.error_type)
        return error_response(exc)

    headers = routing_headers(response.zk_ai.model_dump()) if response.zk_ai else {}
    return JSONResponse(
        status_code=200, content=response.model_dump(exclude_none=True), headers=headers
    )


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

    headers = {
        "x-zkai-request-id": request_id,
        "x-zkai-stream": "true",
        "cache-control": "no-cache",
    }

    def sse(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield sse(
                "response.created",
                {"type": "response.created", "response": {"id": request_id}},
            )
            event = first_event
            while event is not None:
                if event.type == "chunk" and event.chunk is not None:
                    delta = event.chunk.choices[0].delta if event.chunk.choices else None
                    if delta and delta.content:
                        yield sse(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "delta": delta.content,
                                "response_id": request_id,
                            },
                        )
                elif event.type == "error" and event.error is not None:
                    yield sse("error", event.error)
                elif event.type == "end":
                    break
                try:
                    event = await anext(iterator)
                except StopAsyncIteration:
                    break
            yield sse(
                "response.completed",
                {"type": "response.completed", "response": {"id": request_id}},
            )
            yield "data: [DONE]\n\n"
        finally:
            await _aclose(generator)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
