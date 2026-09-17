"""The Scheduler: executes a :class:`RoutePlan` with retry, cooldown and failover.

Loop structure::

    for deployment in plan.candidates:            # failover level
        for credential in pool.candidates():      # key rotation level
            while retries <= policy.max_retries:  # backoff level
                attempt()

Guarantees
----------
* Bounded: ``max_total_attempts`` caps the whole request, ``max_deployments`` caps
  failover depth and ``max_retries_per_credential`` caps the backoff loop.
* Client errors (400/413/409/422) abort immediately - they never rotate a key and
  never trigger a failover.
* Streaming retries only happen *before* the first chunk reaches the client; after
  that the error is propagated as an SSE error event (data already sent cannot be
  unsent).
* Cancellation (client disconnect) propagates: the upstream stream is closed in a
  ``finally`` block and the attempt is recorded as ``cancelled``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.errors import (
    AllAttemptsFailedError,
    ClientDisconnected,
    NoAvailableCredentialError,
    NoAvailableDeploymentError,
    ZKAIError,
)
from app.core.logging import attempt_var, credential_var, get_logger, model_var, provider_var
from app.credentials.pool import CredentialPool
from app.models.credential import CredentialRuntime
from app.models.request import ChatCompletionRequest
from app.models.response import (
    AttemptOutcome,
    ChatCompletionChunk,
    ChatCompletionResponse,
    RoutingMeta,
    StreamEvent,
    Usage,
)
from app.providers.base import ProviderAdapter, ProviderContext
from app.retry.classifier import ErrorClass, ErrorClassifier, ErrorInfo
from app.retry.policy import RetryPolicy
from app.routing.router import Router, RoutingCandidate, RoutingDecision

logger = get_logger("routing.scheduler")

Sleeper = Callable[[float], Awaitable[None]]


class _Next(str, Enum):
    """Where the scheduler goes after an attempt.

    ``RETRY``      back off and retry the same credential;
    ``CREDENTIAL`` park this key and try a sibling key of the same deployment;
    ``DEPLOYMENT`` abandon this deployment and fail over to the next candidate;
    ``ABORT``      return the error to the client immediately (client mistake);
    ``SUCCESS``    the attempt produced a response.
    """

    RETRY = "retry"
    CREDENTIAL = "credential"
    DEPLOYMENT = "deployment"
    ABORT = "abort"
    SUCCESS = "success"


@dataclass(slots=True)
class ExecutionResult:
    """Outcome of a non-streaming request."""

    response: ChatCompletionResponse
    decision: RoutingDecision
    meta: RoutingMeta
    attempts: list[AttemptOutcome] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    @property
    def succeeded(self) -> bool:
        return True


class Scheduler:
    """Executes routing plans against providers with retry and failover."""

    def __init__(
        self,
        *,
        router: Router,
        pool: CredentialPool,
        policy: RetryPolicy | None = None,
        classifier: ErrorClassifier | None = None,
        sleeper: Sleeper | None = None,
        request_timeout: float = 120.0,
    ) -> None:
        self.router = router
        self.pool = pool
        self.policy = policy or RetryPolicy()
        self.classifier = classifier or ErrorClassifier()
        self._sleep: Sleeper = sleeper or asyncio.sleep
        self.request_timeout = request_timeout
        #: Deployment-level cooldowns applied by 529/503 style errors.
        self._deployment_cooldowns: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def deployment_cooling_down(self, deployment_id: str, *, now: float | None = None) -> bool:
        moment = now if now is not None else time.time()
        until = self._deployment_cooldowns.get(deployment_id)
        return bool(until and until > moment)

    def _apply_deployment_cooldown(self, deployment_id: str, info: ErrorInfo) -> float:
        if info.cooldown_scope != "deployment" or info.cooldown_seconds <= 0:
            return 0.0
        until = time.time() + info.cooldown_seconds
        self._deployment_cooldowns[deployment_id] = until
        logger.info(
            "deployment %s cooled down for %.1fs (%s)",
            deployment_id,
            info.cooldown_seconds,
            info.error_type,
        )
        return info.cooldown_seconds

    def _credentials_for(
        self, candidate: RoutingCandidate, *, session_key: str | None = None
    ) -> list[CredentialRuntime]:
        """Usable credentials for a candidate, bounded by the policy.

        ``session_key`` floats the conversation's pinned credential to the front
        (see :meth:`app.credentials.pool.CredentialPool.candidates`).
        """
        provider = candidate.provider
        credentials = self.pool.candidates(provider.id, session_key=session_key)
        if not credentials and provider.requires_credential:
            return []
        # Proactive quota accounting happens at selection time: each credential
        # we are about to try admits one request into its window(s). A served
        # 429 also consumed upstream quota, so failures are *not* refunded.
        if self.pool.rate_limiter is not None and credentials:
            admitted: list[CredentialRuntime] = []
            for credential in credentials:
                if self.pool.rate_limiter.admit(provider.id, credential.id, credential.tags):
                    admitted.append(credential)
                if len(admitted) >= self.policy.max_credentials_per_deployment:
                    break
            return admitted
        return credentials[: self.policy.max_credentials_per_deployment]

    def _session_key(self, request: ChatCompletionRequest) -> str | None:
        """Stable identity for credential affinity, without persisting any state.

        Prefers an explicit ``session_id``/``user`` the client sends; otherwise a
        fingerprint of (model + first message). Deliberately *not* a function of
        conversation length: the whole conversation must map to one key so the
        pin holds from turn to turn instead of re-rolling every turn. Two
        conversations that share an opener may collide onto one pin - harmless,
        since a pin is only a preference, not a lock. Cheap: no storage, no
        client-side requirement.
        """
        if not self.pool.affinity_enabled:
            return None
        explicit = (request.session_id or request.user or "").strip()
        if explicit:
            return f"id:{explicit[:200]}"
        messages = request.messages
        if not messages:
            return None
        first = messages[0]
        opener = first.content if isinstance(first.content, str) else str(first.content)
        seed = f"{request.model}\x00{opener[:4000]}"
        digest = hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:16]
        return f"h:{digest}"

    @staticmethod
    def _is_abort(info: ErrorInfo) -> bool:
        """True when the error must be returned to the client immediately."""
        return (
            info.is_request_error()
            and not info.retryable
            and not info.switch_credential
            and not info.switch_provider
        )

    def _decide(self, info: ErrorInfo, *, retries_done: int) -> _Next:
        """Translate a classified error into the next scheduling action."""
        if self._is_abort(info):
            return _Next.ABORT
        if info.retryable and self.policy.should_retry(info, retries_done=retries_done):
            return _Next.RETRY
        if info.switch_credential:
            return _Next.CREDENTIAL
        if info.switch_provider:
            return _Next.DEPLOYMENT
        return _Next.ABORT

    def _decide_open_stream(self, info: ErrorInfo, *, stream_open_retries: int) -> _Next:
        """Same as :meth:`_decide`, but bounded by ``stream_open_retries``.

        A stream that failed to *open* costs nothing to retry, so a few retries are
        allowed even for non-retryable-but-switchable errors.
        """
        if self._is_abort(info):
            return _Next.ABORT
        if info.retryable and stream_open_retries < self.policy.stream_open_retries:
            return _Next.RETRY
        if info.switch_credential:
            return _Next.CREDENTIAL
        if info.switch_provider:
            return _Next.DEPLOYMENT
        return _Next.ABORT

    def _attempt_outcome(
        self,
        *,
        attempt_number: int,
        candidate: RoutingCandidate,
        credential: CredentialRuntime | None,
        started: float,
        finished: float,
        status: str,
        usage: Usage | None = None,
        info: ErrorInfo | None = None,
    ) -> AttemptOutcome:
        return AttemptOutcome(
            attempt_number=attempt_number,
            provider=candidate.deployment.provider_id,
            model=candidate.deployment.model,
            credential_id=credential.id if credential else None,
            deployment_id=candidate.deployment.id,
            started_at=started,
            finished_at=finished,
            latency_ms=round((finished - started) * 1000, 2),
            status=status,
            error_type=info.error_type if info else None,
            http_status=info.http_status if info else None,
            detail=(info.message[:300] if info and info.message else None),
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    def _final_meta(
        self,
        *,
        request_id: str,
        decision: RoutingDecision,
        candidate: RoutingCandidate,
        credential: CredentialRuntime | None,
        attempt_number: int,
        latency_ms: float,
        finish_reason: str | None,
    ) -> RoutingMeta:
        return RoutingMeta(
            request_id=request_id,
            requested_model=decision.requested_model,
            alias=decision.alias,
            resolved_model=decision.resolved_models[0] if decision.resolved_models else candidate.model.id,
            provider=candidate.deployment.provider_id,
            deployment_id=candidate.deployment.id,
            credential_id=credential.id if credential else None,
            attempt=attempt_number,
            routing_reason=decision.reason,
            capability_scores={
                key: round(value, 4) for key, value in candidate.breakdown.items() if value
            },
            latency_ms=round(latency_ms, 2),
            finish_reason=finish_reason,
            fallback_used=attempt_number > 1 or candidate.order > 0,
            cooldowns={
                key: round(value - time.time(), 1)
                for key, value in self._deployment_cooldowns.items()
                if value > time.time()
            },
        )

    # ------------------------------------------------------------------ #
    # Non-streaming execution
    # ------------------------------------------------------------------ #
    async def execute(
        self, request: ChatCompletionRequest, *, request_id: str
    ) -> ExecutionResult:
        """Run *request* to completion, retrying and failing over as configured."""
        decision = self.router.plan(request)
        session_key = self._session_key(request)
        attempts: list[AttemptOutcome] = []
        errors: list[ErrorInfo] = []
        attempt_number = 0
        deployments_tried = 0

        for candidate in decision.candidates:
            if not candidate.eligible:
                continue
            # A deployment parked by a previous 503/504/529 is skipped outright:
            # re-trying it mid-cooldown burns an attempt and re-penalises keys
            # that are not the problem.
            if self.deployment_cooling_down(candidate.deployment.id):
                logger.debug(
                    "deployment %s skipped (cooldown active)", candidate.deployment.id
                )
                continue
            if deployments_tried >= self.policy.max_deployments:
                break
            deployments_tried += 1
            adapter = self._adapter_or_none(candidate, errors)
            if adapter is None:
                continue

            credentials = self._credentials_for(candidate, session_key=session_key)
            if not credentials:
                # No key can serve this deployment: record an attempt so the audit
                # trail stays monotonic, then move on. Nothing was sent upstream, so
                # the credential counters are left alone.
                attempt_number += 1
                info = self._no_credential_info(candidate)
                errors.append(info)
                attempts.append(
                    self._attempt_outcome(
                        attempt_number=attempt_number,
                        candidate=candidate,
                        credential=None,
                        started=time.time(),
                        finished=time.time(),
                        status="error",
                        info=info,
                    )
                )
                continue

            for credential in credentials:
                retries_done = 0
                # Which loop should we leave when this credential gives up?
                next_step = _Next.RETRY
                while True:
                    if attempt_number >= self.policy.max_total_attempts:
                        raise self._exhausted(decision, attempts, errors)
                    attempt_number += 1
                    provider_var.set(candidate.deployment.provider_id)
                    model_var.set(candidate.deployment.model)
                    credential_var.set(credential.id)
                    attempt_var.set(attempt_number)

                    started = time.perf_counter()
                    started_wall = time.time()
                    ctx = self._context(request, candidate, credential, attempt_number, request_id)
                    try:
                        response = await adapter.chat(request, ctx)
                    except asyncio.CancelledError:
                        finished_wall = time.time()
                        attempts.append(
                            self._attempt_outcome(
                                attempt_number=attempt_number,
                                candidate=candidate,
                                credential=credential,
                                started=started_wall,
                                finished=finished_wall,
                                status="cancelled",
                            )
                        )
                        self.pool.release(credential.id)
                        logger.info("attempt %d cancelled (client disconnect)", attempt_number)
                        raise
                    except Exception as exc:
                        finished_wall = time.time()
                        info = self.classifier.classify(exc)
                        errors.append(info)
                        self.pool.report_failure(credential.id, info)
                        attempts.append(
                            self._attempt_outcome(
                                attempt_number=attempt_number,
                                candidate=candidate,
                                credential=credential,
                                started=started_wall,
                                finished=finished_wall,
                                status="error",
                                info=info,
                            )
                        )
                        logger.warning(
                            "attempt %d failed: %s (%s) on %s/%s",
                            attempt_number,
                            info.error_type,
                            info.http_status,
                            candidate.deployment.provider_id,
                            candidate.deployment.model,
                        )
                        self._apply_deployment_cooldown(candidate.deployment.id, info)

                        next_step = self._decide(info, retries_done=retries_done)
                        if next_step is _Next.ABORT:
                            raise info.to_error(
                                provider=candidate.deployment.provider_id,
                                model=candidate.deployment.model,
                            ) from exc
                        if next_step is _Next.RETRY:
                            retries_done += 1
                            delay = self.policy.delay_for(attempt_number, info)
                            logger.info(
                                "retrying the same credential in %.2fs (%d/%d)",
                                delay,
                                retries_done,
                                self.policy.max_retries_per_credential,
                            )
                            await self._sleep(delay)
                            continue
                        break  # _Next.CREDENTIAL or _Next.DEPLOYMENT
                    else:
                        next_step = _Next.SUCCESS

                    latency_ms = (time.perf_counter() - started) * 1000
                    self.pool.report_success(credential.id, latency_ms=latency_ms)
                    self.pool.note_affinity(session_key, credential.id)
                    usage = response.usage or Usage()
                    attempts.append(
                        self._attempt_outcome(
                            attempt_number=attempt_number,
                            candidate=candidate,
                            credential=credential,
                            started=started_wall,
                            finished=time.time(),
                            status="success",
                            usage=usage,
                        )
                    )
                    finish_reason = (
                        response.choices[0].finish_reason if response.choices else None
                    )
                    meta = self._final_meta(
                        request_id=request_id,
                        decision=decision,
                        candidate=candidate,
                        credential=credential,
                        attempt_number=attempt_number,
                        latency_ms=latency_ms,
                        finish_reason=finish_reason,
                    )
                    response.model = decision.requested_model
                    return ExecutionResult(
                        response=response,
                        decision=decision,
                        meta=meta,
                        attempts=attempts,
                        usage=usage,
                    )

                # The credential loop only reaches here after a break.
                if next_step is _Next.DEPLOYMENT:
                    logger.info(
                        "failing over from deployment %s (%d attempt(s) so far)",
                        candidate.deployment.id,
                        attempt_number,
                    )
                    break  # leave the credential loop -> next candidate
                # _Next.CREDENTIAL: try the next key of the same deployment.

        raise self._exhausted(decision, attempts, errors)

    # ------------------------------------------------------------------ #
    # Streaming execution
    # ------------------------------------------------------------------ #
    async def stream(
        self,
        request: ChatCompletionRequest,
        *,
        request_id: str,
        attempts_sink: list[AttemptOutcome] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a completion, retrying only while the stream is still closed.

        ``attempts_sink`` lets the caller collect per-attempt outcomes for
        persistence: without it the streaming path would leave no audit trail
        (the service layer's own list was never populated, so failures were
        recorded as ``attempt_count=0`` with no upstream detail at all).
        """
        decision = self.router.plan(request)
        session_key = self._session_key(request)
        attempts: list[AttemptOutcome] = []
        errors: list[ErrorInfo] = []
        attempt_number = 0
        deployments_tried = 0
        stream_open_retries = 0

        def _record(outcome: AttemptOutcome) -> None:
            attempts.append(outcome)
            if attempts_sink is not None:
                attempts_sink.append(outcome)

        for candidate in decision.candidates:
            if not candidate.eligible:
                continue
            if self.deployment_cooling_down(candidate.deployment.id):
                logger.debug(
                    "deployment %s skipped (cooldown active)", candidate.deployment.id
                )
                continue
            if deployments_tried >= self.policy.max_deployments:
                break
            deployments_tried += 1
            adapter = self._adapter_or_none(candidate, errors)
            if adapter is None:
                continue

            credentials = self._credentials_for(candidate, session_key=session_key)
            if not credentials:
                # Same as the non-streaming path: keep the audit trail complete and
                # monotonic even though nothing was sent upstream.
                attempt_number += 1
                info = self._no_credential_info(candidate)
                errors.append(info)
                _record(
                    self._attempt_outcome(
                        attempt_number=attempt_number,
                        candidate=candidate,
                        credential=None,
                        started=time.time(),
                        finished=time.time(),
                        status="error",
                        info=info,
                    )
                )
                continue

            for credential in credentials:
                next_step = _Next.RETRY
                while True:
                    if attempt_number >= self.policy.max_total_attempts:
                        raise self._exhausted(decision, attempts, errors)
                    attempt_number += 1
                    provider_var.set(candidate.deployment.provider_id)
                    model_var.set(candidate.deployment.model)
                    credential_var.set(credential.id)
                    attempt_var.set(attempt_number)

                    ctx = self._context(request, candidate, credential, attempt_number, request_id)
                    started_wall = time.time()
                    started = time.perf_counter()
                    generator = adapter.stream(request, ctx)
                    first_chunk: ChatCompletionChunk | None = None
                    try:
                        first_chunk = await anext(generator)
                    except StopAsyncIteration:
                        first_chunk = None
                    except asyncio.CancelledError:
                        _record(
                            self._attempt_outcome(
                                attempt_number=attempt_number,
                                candidate=candidate,
                                credential=credential,
                                started=started_wall,
                                finished=time.time(),
                                status="cancelled",
                            )
                        )
                        self.pool.release(credential.id)
                        await self._close(generator)
                        raise
                    except Exception as exc:
                        info = self.classifier.classify(exc)
                        errors.append(info)
                        self.pool.report_failure(credential.id, info)
                        _record(
                            self._attempt_outcome(
                                attempt_number=attempt_number,
                                candidate=candidate,
                                credential=credential,
                                started=started_wall,
                                finished=time.time(),
                                status="error",
                                info=info,
                            )
                        )
                        self._apply_deployment_cooldown(candidate.deployment.id, info)
                        await self._close(generator)
                        logger.warning(
                            "stream open attempt %d failed: %s", attempt_number, info.error_type
                        )
                        next_step = self._decide_open_stream(
                            info, stream_open_retries=stream_open_retries
                        )
                        if next_step is _Next.ABORT:
                            raise info.to_error(
                                provider=candidate.deployment.provider_id,
                                model=candidate.deployment.model,
                            ) from exc
                        if next_step is _Next.RETRY:
                            stream_open_retries += 1
                            delay = self.policy.delay_for(attempt_number, info)
                            logger.info("reopening stream in %.2fs", delay)
                            await self._sleep(delay)
                            continue
                        break  # CREDENTIAL or DEPLOYMENT

                    # Stream is open: emit chunks. Failures from here on cannot be
                    # retried (data already sent), so they are surfaced as events.
                    usage = Usage()
                    finish_reason: str | None = None
                    cancelled = False
                    stream_error = False
                    streamed_chars = 0
                    try:
                        chunk = first_chunk
                        while True:
                            if chunk is not None:
                                if chunk.usage:
                                    usage = chunk.usage
                                if chunk.choices:
                                    if chunk.choices[0].finish_reason:
                                        finish_reason = chunk.choices[0].finish_reason
                                    delta = chunk.choices[0].delta
                                    if delta.content:
                                        streamed_chars += len(delta.content)
                                yield StreamEvent(type="chunk", chunk=chunk)
                            try:
                                chunk = await anext(generator)
                            except StopAsyncIteration:
                                break
                    except asyncio.CancelledError:
                        cancelled = True
                        raise
                    except Exception as exc:
                        stream_error = True
                        info = self.classifier.classify(exc)
                        errors.append(info)
                        self.pool.report_failure(credential.id, info)
                        logger.error(
                            "stream aborted mid-flight on %s: %s",
                            candidate.deployment.id,
                            info.error_type,
                        )
                        yield StreamEvent(
                            type="error",
                            error=info.to_error(
                                provider=candidate.deployment.provider_id,
                                model=candidate.deployment.model,
                            ).to_dict(include_raw=False),
                        )
                    finally:
                        await self._close(generator)

                    latency_ms = (time.perf_counter() - started) * 1000
                    if not cancelled and not stream_error:
                        self.pool.report_success(credential.id, latency_ms=latency_ms)
                        self.pool.note_affinity(session_key, credential.id)

                    if usage.total_tokens == 0:
                        # Providers that omit usage during streaming: estimate.
                        usage = Usage.build(
                            request.estimated_input_tokens(),
                            max(0, streamed_chars // 4),
                        )

                    _record(
                        self._attempt_outcome(
                            attempt_number=attempt_number,
                            candidate=candidate,
                            credential=credential,
                            started=started_wall,
                            finished=time.time(),
                            status="cancelled" if cancelled else ("error" if stream_error else "success"),
                            usage=usage,
                            info=errors[-1] if (stream_error and errors) else None,
                        )
                    )
                    meta = self._final_meta(
                        request_id=request_id,
                        decision=decision,
                        candidate=candidate,
                        credential=credential,
                        attempt_number=attempt_number,
                        latency_ms=latency_ms,
                        finish_reason=finish_reason,
                    )
                    yield StreamEvent(type="usage", usage=usage)
                    yield StreamEvent(type="meta", meta=meta)
                    yield StreamEvent(type="end")
                    return

                # The credential loop only reaches here after a break.
                if next_step is _Next.DEPLOYMENT:
                    logger.info(
                        "failing over from deployment %s after stream error",
                        candidate.deployment.id,
                    )
                    break  # leave the credential loop -> next candidate
                # _Next.CREDENTIAL: reopen the stream with the next key.

        raise self._exhausted(decision, attempts, errors)

    # ------------------------------------------------------------------ #
    # Small helpers
    # ------------------------------------------------------------------ #
    def _context(
        self,
        request: ChatCompletionRequest,
        candidate: RoutingCandidate,
        credential: CredentialRuntime,
        attempt_number: int,
        request_id: str,
    ) -> ProviderContext:
        return ProviderContext(
            request_id=request_id,
            deployment=candidate.deployment,
            model=candidate.model,
            credential=credential,
            timeout=self.request_timeout,
            stream=bool(request.stream),
            attempt=attempt_number,
        )

    def _adapter_or_none(
        self, candidate: RoutingCandidate, errors: list[ErrorInfo]
    ) -> ProviderAdapter | None:
        try:
            return self.router.adapter(candidate.deployment.provider_id)
        except ZKAIError as exc:
            info = self.classifier.classify(exc)
            errors.append(info)
            logger.warning(
                "deployment %s skipped: %s", candidate.deployment.id, exc.message
            )
            return None

    def _no_credential_info(self, candidate: RoutingCandidate) -> ErrorInfo:
        wait = self.pool.next_available_in(candidate.deployment.provider_id)
        detail = f"供应商 {candidate.deployment.provider_id} 当前没有可用的 Key"
        if wait:
            detail += f"（约 {int(wait) + 1} 秒后自动恢复）"
        error = NoAvailableCredentialError(
            detail,
            provider=candidate.deployment.provider_id,
            retry_after=wait,
        )
        logger.warning(
            "no usable credential for provider %s (deployment %s)%s",
            candidate.deployment.provider_id,
            candidate.deployment.id,
            f", recovers in ~{wait:.0f}s" if wait else "",
        )
        return self.classifier.classify(error)

    def _exhausted(
        self,
        decision: RoutingDecision,
        attempts: list[AttemptOutcome],
        errors: list[ErrorInfo],
    ) -> AllAttemptsFailedError:
        """Build the aggregated error returned when everything failed.

        The status code tells the caller whose problem it is:

        * caller mistakes (400/404/413/409/422) are mirrored;
        * transient upstream failures (500/502/504) are mirrored;
        * exhausted credentials, throttling and unavailable providers become
          ``503`` / ``429`` - never ``401``, which would wrongly tell the client
          that *its* API key is invalid.
        """
        if errors:
            worst = errors[-1]
            status = self._client_status_for(worst)
            message = (
                f"模型 '{decision.requested_model}' 的 {len(attempts)} 次尝试全部失败："
                f"{worst.error_type}: {worst.message}"
            )
        else:
            worst = None
            status = 503
            message = f"没有任何部署能处理模型 '{decision.requested_model}' 的请求"
        error = AllAttemptsFailedError(message, attempts=attempts)
        error.http_status = status
        error.error_type = worst.error_type if worst else "no_available_deployment"
        # Surface "come back in Ns" when the blocking failure was a cooldown /
        # throttle, so clients back off instead of hammering a blacked-out pool.
        if worst and worst.retry_after:
            error.retry_after = worst.retry_after
        logger.error("request failed after %d attempt(s): %s", len(attempts), message)
        return error

    @staticmethod
    def _client_status_for(info: ErrorInfo) -> int:
        """Map the final failure class onto the status returned to the client."""
        mapping = {
            ErrorClass.CLIENT: info.client_status,
            ErrorClass.TRANSIENT: info.client_status,
            ErrorClass.THROTTLE: 429,
            ErrorClass.CREDENTIAL: 503,
            ErrorClass.AVAILABILITY: 503,
            ErrorClass.INTERNAL: 502,
        }
        return int(mapping.get(info.error_class, 502))

    @staticmethod
    async def _close(generator: Any) -> None:
        """Close an async generator, ignoring secondary failures."""
        aclose = getattr(generator, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:
            logger.debug("error while closing upstream stream", exc_info=True)


__all__ = [
    "AllAttemptsFailedError",
    "ClientDisconnected",
    "ExecutionResult",
    "NoAvailableDeploymentError",
    "Scheduler",
]
