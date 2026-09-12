"""Request orchestration: the seam between HTTP, the scheduler and the database.

Responsibilities
----------------
1. Open a ``requests`` row and keep the whole lifecycle (start, attempts, finish)
   in one place - so a crashed client still leaves a usable audit trail.
2. Delegate execution to the :class:`~app.routing.scheduler.Scheduler`.
3. Persist attempts, usage and costs; convert failures into typed errors.
4. Never let telemetry break a request.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.core.errors import ClientDisconnected, ZKAIError
from app.core.logging import get_logger, request_id_var
from app.database.repository import RequestRepository
from app.models.request import ChatCompletionRequest
from app.models.response import (
    AttemptOutcome,
    ChatCompletionResponse,
    ResponsesRequest,
    ResponsesResponse,
    ResponsesUsage,
    RoutingMeta,
    StreamEvent,
    Usage,
)
from app.routing.scheduler import ExecutionResult, Scheduler
from app.services.usage_service import UsageService

logger = get_logger("services.request")


class RequestService:
    """Execute chat/responses requests end to end."""

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        request_repository: RequestRepository | None = None,
        usage_service: UsageService | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.requests = request_repository
        self.usage = usage_service

    # ------------------------------------------------------------------ #
    # Non-streaming
    # ------------------------------------------------------------------ #
    async def chat(
        self,
        request: ChatCompletionRequest,
        *,
        request_id: str,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> ChatCompletionResponse:
        """Run a non-streaming completion and return the canonical response."""
        request_id_var.set(request_id)
        await self._start(request_id, request, client_ip=client_ip, user_agent=user_agent)

        try:
            result: ExecutionResult = await self.scheduler.execute(
                request, request_id=request_id
            )
        except ZKAIError as exc:
            await self._finalize_failure(
                request_id,
                error=exc,
                http_status=getattr(exc, "http_status", 502),
                attempts=getattr(exc, "attempts", []) or [],
            )
            raise
        except asyncio.CancelledError:
            await self._finalize_failure(
                request_id,
                error=ClientDisconnected("client disconnected before completion"),
                http_status=499,
                attempts=[],
            )
            raise

        response = result.response
        response.zk_ai = result.meta
        await self._finalize_success(request_id, result)
        return response

    async def _finalize_success(self, request_id: str, result: ExecutionResult) -> None:
        meta: RoutingMeta = result.meta
        cost = 0.0
        if self.usage is not None:
            cost = await self.usage.record(
                request_id=request_id,
                provider_id=meta.provider,
                model_id=meta.resolved_model,
                deployment_id=meta.deployment_id,
                credential_id=meta.credential_id,
                usage=result.usage,
                latency_ms=meta.latency_ms,
            )
        if self.requests is not None:
            await self._guard(
                self.requests.add_attempts(request_id, result.attempts),
                "persist attempts",
            )
            await self._guard(
                self.requests.finish(
                    request_id,
                    status="success",
                    http_status=200,
                    provider_id=meta.provider,
                    deployment_id=meta.deployment_id,
                    resolved_model=meta.resolved_model,
                    alias=meta.alias,
                    credential_id=meta.credential_id,
                    attempt_count=len(result.attempts),
                    fallback_used=meta.fallback_used,
                    routing_reason=meta.routing_reason,
                    latency_ms=meta.latency_ms,
                    input_tokens=result.usage.prompt_tokens,
                    output_tokens=result.usage.completion_tokens,
                    cost_usd=cost,
                ),
                "persist request",
            )

    async def _finalize_failure(
        self,
        request_id: str,
        *,
        error: ZKAIError,
        http_status: int,
        attempts: list[AttemptOutcome],
    ) -> None:
        if self.requests is None:
            return
        await self._guard(
            self.requests.add_attempts(request_id, attempts), "persist failed attempts"
        )
        await self._guard(
            self.requests.finish(
                request_id,
                status="cancelled" if http_status == 499 else "error",
                http_status=http_status,
                error_type=error.error_type,
                attempt_count=len(attempts),
                fallback_used=len(attempts) > 1,
                routing_reason=None,
            ),
            "persist failed request",
        )

    # ------------------------------------------------------------------ #
    # Streaming
    # ------------------------------------------------------------------ #
    async def stream(
        self,
        request: ChatCompletionRequest,
        *,
        request_id: str,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yield internal stream events, persisting the full lifecycle.

        Failure semantics:

        * **Pre-flight** (nothing has been yielded yet) - the typed error is
          re-raised so the HTTP layer can still answer with a real JSON status
          code. A client that never received a byte must not be told "200 OK".
        * **Mid-stream** (chunks already left the gateway) - the error is turned
          into an ``error`` event; the status line is long gone.
        """
        request_id_var.set(request_id)
        await self._start(request_id, request, client_ip=client_ip, user_agent=user_agent)

        attempts: list[AttemptOutcome] = []
        usage = Usage()
        meta: RoutingMeta | None = None
        error_message: str | None = None
        error_type: str | None = None
        error_status: int = 502
        cancelled = False
        started_streaming = False
        #: True once the scheduler signalled a terminal ``end`` event. The consumer
        #: is allowed to stop iterating right after that (the HTTP layer does), and
        #: closing the generator then is normal completion, not a cancellation.
        terminated = False

        try:
            async for event in self.scheduler.stream(
                request, request_id=request_id, attempts_sink=attempts
            ):
                if event.type == "meta" and event.meta is not None:
                    meta = event.meta
                elif event.type == "usage" and event.usage is not None:
                    usage = event.usage
                elif event.type == "error" and event.error is not None:
                    envelope = event.error.get("error", event.error)
                    error_message = str(envelope.get("message", "upstream error"))
                    error_type = str(envelope.get("type", "upstream_error"))
                    error_status = int(envelope.get("code") or 502)
                elif event.type == "end":
                    terminated = True
                started_streaming = True
                yield event
        except ZKAIError as exc:
            error_message = exc.message
            error_type = exc.error_type
            error_status = exc.http_status
            if started_streaming:
                yield StreamEvent(type="error", error=exc.to_dict())
            else:
                raise
        except asyncio.CancelledError:
            cancelled = True
            raise
        except GeneratorExit:
            # The consumer stopped iterating. That is a real client disconnect only
            # if the stream had not already terminated - the HTTP layer closes the
            # generator right after consuming our ``end`` event.
            cancelled = not terminated
            raise
        finally:
            # Persist even when the client vanished mid-stream.
            await self._guard(self._finish_stream(
                request_id=request_id,
                meta=meta,
                usage=usage,
                error_type=error_type,
                error_message=error_message,
                error_status=error_status,
                cancelled=cancelled,
                attempts=attempts,
            ), "persist stream lifecycle")

    async def _finish_stream(
        self,
        *,
        request_id: str,
        meta: RoutingMeta | None,
        usage: Usage,
        error_type: str | None,
        error_message: str | None,
        error_status: int,
        cancelled: bool,
        attempts: list[AttemptOutcome],
    ) -> None:
        stream_cost = 0.0
        if self.usage is not None and meta is not None and usage.total_tokens > 0:
            stream_cost = await self.usage.record(
                request_id=request_id,
                provider_id=meta.provider,
                model_id=meta.resolved_model,
                deployment_id=meta.deployment_id,
                credential_id=meta.credential_id,
                usage=usage,
                latency_ms=meta.latency_ms,
            )
        if self.requests is None:
            return
        failed = bool(error_type)
        # Streaming counterpart of _finalize_failure: persist the per-attempt
        # audit trail collected through the scheduler's sink (previously the
        # list was passed around but never written, so failed streams showed
        # attempt_count=0 with no upstream detail).
        if attempts:
            await self.requests.add_attempts(request_id, attempts)
        if meta is not None:
            await self.requests.finish(
                request_id,
                status="cancelled" if cancelled else ("error" if failed else "success"),
                http_status=499 if cancelled else (error_status if failed else 200),
                provider_id=meta.provider,
                deployment_id=meta.deployment_id,
                resolved_model=meta.resolved_model,
                alias=meta.alias,
                credential_id=meta.credential_id,
                error_type=error_type,
                attempt_count=meta.attempt,
                fallback_used=meta.fallback_used,
                routing_reason=meta.routing_reason,
                latency_ms=meta.latency_ms,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cost_usd=stream_cost,
            )
        else:
            await self.requests.finish(
                request_id,
                status="cancelled" if cancelled else "error",
                http_status=499 if cancelled else (error_status if failed else 502),
                error_type=error_type or "no_stream_meta",
                attempt_count=len(attempts),
            )

    # ------------------------------------------------------------------ #
    # Responses API
    # ------------------------------------------------------------------ #
    async def responses(
        self,
        request: ResponsesRequest,
        *,
        request_id: str,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> ResponsesResponse:
        """Serve the Responses API by translating to a chat completion."""
        chat_request = request.to_chat_request()
        chat_response = await self.chat(
            chat_request, request_id=request_id, client_ip=client_ip, user_agent=user_agent
        )
        text = chat_response.text()
        usage = chat_response.usage or Usage()
        return ResponsesResponse(
            model=chat_response.model,
            output=[
                {
                    "id": f"msg_{request_id[:16]}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ],
            output_text=text,
            usage=ResponsesUsage(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            ),
            zk_ai=chat_response.zk_ai,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _start(
        self,
        request_id: str,
        request: ChatCompletionRequest,
        *,
        client_ip: str | None,
        user_agent: str | None,
    ) -> None:
        if self.requests is None:
            return
        await self._guard(
            self.requests.start(
                request_id=request_id,
                requested_model=request.model,
                stream=bool(request.stream),
                client_ip=client_ip,
                user_agent=user_agent,
            ),
            "create request row",
        )

    @staticmethod
    async def _guard(coro: Any, what: str) -> None:
        """Run a telemetry coroutine, shielded from cancellation, never raising.

        ``asyncio.shield`` keeps the write alive even if the client disconnects;
        the cancellation itself is *not* swallowed - it continues to propagate from
        the caller's own frame.
        """
        try:
            await asyncio.shield(coro)
        except asyncio.CancelledError:
            # The shielded write continues in the background.
            logger.debug("telemetry step detached by cancellation: %s", what)
        except Exception:
            logger.exception("telemetry step failed: %s", what)


__all__ = ["RequestService"]
