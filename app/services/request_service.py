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

from app.core.errors import (
    AliasNotFoundError,
    ClientDisconnected,
    ContextLengthExceededError,
    ModelNotFoundError,
    ZKAIError,
)
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
    function_call_output_item,
    message_output_item,
)
from app.routing.scheduler import ExecutionResult, Scheduler
from app.services.history import trim_history
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
        trim_history_tokens: int = 0,
    ) -> None:
        self.scheduler = scheduler
        self.requests = request_repository
        self.usage = usage_service
        #: 历史裁剪预算，0 = 关闭。见 ``app/services/history.py`` 的模块文档：
        #: 默认关是因为删上下文可能改变答案，开启与否是运营决策。
        self.trim_history_tokens = max(0, trim_history_tokens)

    # ------------------------------------------------------------------ #
    # Input size guard
    # ------------------------------------------------------------------ #
    def _budget_history(self, request: ChatCompletionRequest) -> None:
        """Trim an over-long history *before* the token gate sees it.

        Ordering matters: the gate asks ``Router.plan`` whether any deployment
        can still hold the request. Trimming first means a 120K-token Codex
        session can be served by a 256K deployment after the old turns are
        dropped, instead of being rejected - or, worse, passed through whole.

        Runs on both ``chat()`` and ``stream()`` because those are the two
        convergence points for all three public paths (chat / responses / stream).
        """
        if self.trim_history_tokens <= 0:
            return
        trim_history(request, budget=self.trim_history_tokens)

    def _guard_input_size(self, request: ChatCompletionRequest) -> None:
        """估算 token 超限就拒，不发上游（``ZKAI_MAX_INPUT_TOKENS``，0=关闭）。

        为什么单独加这道闸，而不是只调大 ``max_request_body_mb``：

        * 字节闸看的是客户端声明的 ``content-length``，10MB ≈ 290 万 est tokens，
          是现役最大上游窗口（1M）的 2.9 倍——放过去只会收到上游 400/429，
          比在门口拦下更糟；而 chunked 上传不带这个首部，字节闸整个失效。
        * token 闸能说清「哪一段太大」：客户端拿到的是可操作的信息，不是一句
          「请求体过大（上限 10MB）」。
        * ``context_length_exceeded`` + 413 是既有契约（``tests/test_errors.py``
          钉住它不触发换 Key、不故障转移），这里复用它，客户端行为不变。

        放在 ``chat()`` / ``stream()`` 而不是各 protocol handler：三条对外路径
        全在这两处收敛。

        **智能之处（2026-09-26）**：不拿一个拍死的数字硬卡。先问路由器「这个
        请求有没有任何部署吃得下」——``Router.plan`` 是零 I/O 的纯计算。只要有
        部署放得下就放行（哪怕超过固定上限），因为拦了等于白丢一个本可成功的
        请求；只有「所有部署都放不下」才拦，那种发出去必然失败，拦下不伤质量。

        这不是理论推演：实测现役部署窗口 1M/256K/128K 三档，历史最大 prompt
        719,090 tokens。若把上限拍成 900,000，那条只差 18 万，估算器一偏差就被
        误杀；而 ``est=1,250,002`` 时可用部署数是 0，那种才该拦。
        """
        estimated = request.estimated_input_tokens()
        limit = self.scheduler.max_input_tokens
        if limit <= 0 or estimated <= limit:
            return
        if self._any_deployment_fits(request):
            # 有部署放得下：本次超限只是超过保守的人为上限，不影响能否成功。
            # context_fits 在调度层做同样的判断，这里放行与它保持一致。
            logger.debug(
                "请求估算 %d tokens 超过上限 %d，但仍有部署可容纳，放行",
                estimated, limit,
            )
            return
        raise self._oversize_error(request, estimated, limit)

    def _any_deployment_fits(self, request: ChatCompletionRequest) -> bool:
        """是否至少有一个启用的部署装得下这个请求（零 I/O 查询）。"""
        try:
            decision = self.scheduler.router.plan(request)
        except (ModelNotFoundError, AliasNotFoundError):
            # 模型名/别名写错与「太大」是两码事：404 才是真相，绝不能拿 413 盖掉。
            raise
        except Exception:
            # 其余规划失败（配置缺部署等）按「没有部署放得下」处理，语义不变。
            return False
        return bool(decision.eligible_candidates())

    def _oversize_error(
        self, request: ChatCompletionRequest, estimated: int, limit: int
    ) -> ContextLengthExceededError:
        """组装可操作的超限错误：指出最重的几段（图片与文本分开表述）。"""
        heaviest = sorted(
            (
                (
                    len(message.text()) + message.estimated_image_tokens() * 4,
                    index,
                    message.role,
                    message.estimated_image_tokens(),
                )
                for index, message in enumerate(request.messages)
            ),
            reverse=True,
        )[:3]
        detail = "、".join(
            f"#{index} {role} "
            + (f"{image_tokens:,} tokens 的图片" if image_tokens else f"{chars:,} 字符")
            for chars, index, role, image_tokens in heaviest
            if chars > 0 or image_tokens > 0
        )
        return ContextLengthExceededError(
            f"估算输入约 {estimated:,} tokens，所有可用部署都装不下"
            f"（当前上限 {limit:,}）。最大几段：" + (detail or "无") + "。"
            "请缩短对话历史、减少图片或分批处理；如需强行放行，"
            "把 ZKAI_MAX_INPUT_TOKENS 调大。"
        )

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
        self._budget_history(request)
        self._guard_input_size(request)
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
        #: 与 chat() 同一道 token 闸。流式路径**不经过** chat()，漏了这里
        #: 就等于 Codex / Chat 的流式请求完全不受限（而它们正是最大的那批：
        #: DB 实测 >256K tokens 的请求 1176 条，全是 streaming）。
        #: 必须在 try 之内：async generator 在第一个 yield 前抛异常时 finally
        #: 不跑，闸门扔在 try 外会把 requests 行永久留在 pending。
        #: True once the scheduler signalled a terminal ``end`` event. The consumer
        #: is allowed to stop iterating right after that (the HTTP layer does), and
        #: closing the generator then is normal completion, not a cancellation.
        terminated = False

        try:
            self._budget_history(request)
            self._guard_input_size(request)
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
        strip_reasoning: bool = False,
        forward_images: bool = True,
    ) -> ResponsesResponse:
        """Serve the Responses API by translating to a chat completion."""
        chat_request = request.to_chat_request(forward_images=forward_images)
        chat_response = await self.chat(
            chat_request, request_id=request_id, client_ip=client_ip, user_agent=user_agent
        )
        usage = chat_response.usage or Usage()
        output: list[dict[str, Any]] = []
        text_parts: list[str] = []
        choice = chat_response.choices[0] if chat_response.choices else None
        if choice is not None:
            text = choice.message.text()
            extra = choice.message.model_extra or {}
            if text and strip_reasoning and extra.get("content_recovered_from_reasoning"):
                # Thinking promoted by the never-blank guard is not an answer.
                text = ""
            if text:
                output.append(message_output_item(text))
                text_parts.append(text)
            for call in choice.message.tool_calls or []:
                output.append(function_call_output_item(call))
        return ResponsesResponse(
            model=chat_response.model,
            output=output,
            output_text="\n".join(text_parts),
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
