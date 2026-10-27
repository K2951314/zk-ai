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
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.errors import (
    AllAttemptsFailedError,
    ClientDisconnected,
    GatewayTimeoutError,
    NoAvailableCredentialError,
    NoAvailableDeploymentError,
    ZKAIError,
)
from app.core.logging import attempt_var, credential_var, get_logger, model_var, provider_var
from app.credentials.pool import CredentialPool
from app.models.credential import CredentialRuntime
from app.models.provider import ProviderConfig
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

#: 整池 Key 都在冷却时，最多排队等这么久再报 503（见 ``_wait_for_credential``）。
#: 取 10 秒：商汤 429 的冷却基数就是 60 秒，等更久不如让客户端自己重试；
#: 但 10 秒足以吃掉「刚被上一个请求打空、下一批请求立刻到」的那个尖峰
#: （实测那批请求 0.01 秒就失败，本来一次上游都没发）。
_CREDENTIAL_WAIT_SECONDS = 10.0
#: 短于此就不值得等——马上会再进一次判断，不如早点把 503 + Retry-After 给客户端。
_CREDENTIAL_WAIT_MIN_SECONDS = 0.5


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


#: 连续失败到第几次开始隔离。3 是刻意的：一次网络抖动不该屏蔽一个渠道，
#: 但「连续三次都挂」已经不是偶发了——尤其对 60s 超时这种，每次都要白等。
QUARANTINE_THRESHOLD = 3
#: 隔离时长阶梯（秒），按超出阈值的次数逐级取，取到最后一个就不再涨。
#: 60s 起步是为了不惩罚「刚恢复、偶尔抖一下」的部署；6h 封顶是因为再长就该
#: 由运营者去修配置了，无限隔离会让恢复也进不来。
QUARANTINE_LADDER = (60.0, 300.0, 1800.0, 7200.0, 21600.0)
#: 距上次失败超过这么久，连续计数衰减归零（避免陈旧计数让一次抖动重罚）。
QUARANTINE_DECAY_SECONDS = 3600.0


@dataclass
class DeploymentHealth:
    """一个部署的连续失败记账与隔离窗口。

    与 :attr:`Scheduler._deployment_cooldowns` 的分工：那个是**单次错误的短冷却**
    （30s，让同一次请求换个渠道就好）；这个是**跨请求的持久判断**——同一个部署
    反复失败，说明它现在根本不可用，应该从路由计划里消失一段时间。
    实测动机：NVIDIA 的 glm-5.3 每次挂死 60s，旧逻辑只给 30s 冷却，
    于是「撞 60s → 冷 30s → 再撞 60s」无限循环，累计烧掉 60.9 小时。
    """

    consecutive_failures: int = 0
    quarantined_until: float = 0.0
    last_error_type: str = ""
    last_failure_at: float = 0.0

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "quarantined": self.quarantined_until > now,
            "quarantine_seconds_left": max(0.0, round(self.quarantined_until - now, 1)),
            "last_error_type": self.last_error_type,
        }


def is_quarantine_worthy(info: ErrorInfo) -> bool:
    """这次失败算不算「这个部署坏了」的证据。

    只认两类：
    * 反复的传输/上游故障（``transient`` / ``availability``）——单次是抖动，
      连续多次就是真坏了；
    * 「模型/端点在上游不存在或已下线」（404 / ``model_not_found``、
      410 / ``model_gone``）——这是持久的，再怎么重试也不会好。

    明确**不认**：
    * 429 限流（``throttle``）：偶发，且那是凭据层的处置范围；
    * 401/403（``credential``）：换把 Key 可能就好了，隔离整个部署是误伤；
    * 400/413/422（``client``）：调用方自己的错，不该赖部署。

    ``model_gone``（410）为什么算：上游明确宣告模型已 EOL 下线，语义与 404
    一样持久。2026-10-15 实测 NVIDIA 的 moonshotai/kimi-k3 报 410
    （end of life），若不计入隔离，每次请求都会白赔一次 30s+ 的上游往返。
    """
    if info.http_status in (404, 410) or info.error_type in {"model_not_found", "model_gone"}:
        return True
    return info.error_class in {ErrorClass.TRANSIENT, ErrorClass.AVAILABILITY}


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
        max_input_tokens: int = 0,
        max_request_seconds: float = 300.0,
    ) -> None:
        self.router = router
        self.pool = pool
        self.policy = policy or RetryPolicy()
        self.classifier = classifier or ErrorClassifier()
        self._sleep: Sleeper = sleeper or asyncio.sleep
        self.request_timeout = request_timeout
        #: 估算 input token 的硬上限，0 = 不限制。语义见
        #: ``RequestService._guard_input_size``（按 token 而非字节判定的那道闸）。
        self.max_input_tokens = max(0, max_input_tokens)
        #: 单次客户端请求的**墙钟**预算（秒），0 = 不限制。
        #:
        #: 为什么必须有：``max_total_attempts`` 是**次数**上限，不是时间上限。
        #: 20 次尝试 × 60 秒（nvidia 的 read timeout）= 最坏 20 分钟。2026-09-28
        #: 实测一次 zk-auto 首条消息：商汤 429（与消耗器共抢同一把 Key）→
        #: 逐个部署故障转移到 nvidia → glm-5.3 连续 3 次 60 秒超时，客户端
        #: **十几分钟收不到任何字节**，连一个错误都没有——因为每次尝试都「还没超」
        #: 所以调度器认为还有预算。次数闸看不见这种「每次都刚好不超、合计一小时」。
        #:
        #: 默认 300s = 现役最大上游超时（商汤 300s）再留一点余量：一次**成功**的
        #: 慢生成不会被误杀，只有「反复失败累计」会被截断。截断时抛
        #: ``GatewayTimeoutError``，客户端终于能看到一个可操作的错误。
        self.max_request_seconds = max(0.0, max_request_seconds)
        #: Deployment-level cooldowns applied by 529/503 style errors.
        self._deployment_cooldowns: dict[str, float] = {}
        #: 跨请求的连续失败记账与自动隔离，见 :class:`DeploymentHealth`。
        self._deployment_health: dict[str, DeploymentHealth] = {}
        #: 会话级**缓存粘性**：session_key → (deployment_id, 过期时间)。
        #:
        #: 为什么必须有（2026-10-24 实测）：上游前缀缓存按部署隔离。`ds` 别名
        #: round_robin [deepseek-flash, ds4.1]，ds4.1 再 round_robin [AMD, 魔搭]，
        #: 于是同一场对话每轮打到 3 个不同上游——每轮都撞没暖的缓存。对比
        #: harness 直连 99% 命中，网关只有 3%（继续对话也才 12%），用户实付
        #: 近全价。粘性让「上轮成功的部署」优先，缓存滚雪球；失败照常故障转移。
        self._deployment_affinity: OrderedDict[str, tuple[str, float]] = OrderedDict()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def deployment_cooling_down(self, deployment_id: str, *, now: float | None = None) -> bool:
        """短冷却或**自动隔离**中 → True（该部署暂时不进路由计划）。"""
        moment = now if now is not None else time.time()
        until = self._deployment_cooldowns.get(deployment_id)
        if until and until > moment:
            return True
        health = self._deployment_health.get(deployment_id)
        return bool(health and health.quarantined_until > moment)

    # ------------------------------------------------------------------ #
    # 部署健康：连续失败 → 自动隔离（跨请求）
    # ------------------------------------------------------------------ #
    def _note_deployment_failure(self, deployment_id: str, info: ErrorInfo) -> None:
        """记一次失败；连续到阈值就把该部署隔离一段时间。

        只有 :func:`is_quarantine_worthy` 认可的失败才计数——网络抖动、限流、
        调用方的错都不会让一个健康渠道被屏蔽。
        """
        if not is_quarantine_worthy(info):
            return
        now = time.time()
        health = self._deployment_health.setdefault(deployment_id, DeploymentHealth())
        # 陈旧计数衰减：距上次失败很久了，说明中间是好的，不该累计。
        if health.last_failure_at and now - health.last_failure_at > QUARANTINE_DECAY_SECONDS:
            health.consecutive_failures = 0
        health.consecutive_failures += 1
        health.last_error_type = info.error_type
        health.last_failure_at = now
        if health.consecutive_failures < QUARANTINE_THRESHOLD:
            return
        step = min(
            health.consecutive_failures - QUARANTINE_THRESHOLD,
            len(QUARANTINE_LADDER) - 1,
        )
        seconds = QUARANTINE_LADDER[step]
        health.quarantined_until = now + seconds
        logger.warning(
            "deployment %s 连续 %d 次失败（%s），自动隔离 %.0fs——"
            "修好后任意一次成功即解除",
            deployment_id,
            health.consecutive_failures,
            info.error_type,
            seconds,
        )

    def _note_deployment_success(self, deployment_id: str) -> None:
        """任意一次成功 → 清零计数并解除隔离（半开恢复的收口）。"""
        health = self._deployment_health.get(deployment_id)
        if health is None:
            return
        if health.consecutive_failures or health.quarantined_until:
            logger.info(
                "deployment %s 恢复（此前连续失败 %d 次%s）",
                deployment_id,
                health.consecutive_failures,
                "，已解除隔离" if health.quarantined_until else "",
            )
        health.consecutive_failures = 0
        health.quarantined_until = 0.0
        health.last_error_type = ""

    # ------------------------------------------------------------------ #
    # 会话级缓存粘性（deployment affinity）
    # ------------------------------------------------------------------ #
    def _alias_pins_front(self, alias_name: str | None) -> bool:
        """该别名是否 pin_first（运营者热切换链首）——粘性必须让位。"""
        if not alias_name:
            return False
        alias = self.router.config.aliases.get(alias_name)
        return bool(alias and alias.pin_first)

    def _note_deployment_affinity(self, session_key: str | None, deployment_id: str) -> None:
        """成功之后把会话钉到该部署——下一轮优先回来，让前缀缓存滚起来。

        只是**偏好**不是锁：该部署挂了/冷却了照常故障转移到计划里的下一个，
        粘性从来不会把请求拖死。
        """
        if not session_key or not deployment_id:
            return
        self._deployment_affinity.pop(session_key, None)  # refresh LRU position
        # TTL 与容量**现场读**池子（credential_affinity 段），不做构造期快照：
        # 快照会在热重载后变陈旧，而 getattr 猜属性名的旧写法更是把容量钉死
        # 在魔数 64——运营者改 max_sessions 怎么都不生效（2026-10-24 实锤）。
        self._deployment_affinity[session_key] = (
            deployment_id,
            time.time() + self.pool.affinity_ttl,
        )
        while len(self._deployment_affinity) > self.pool.affinity_max_sessions:
            self._deployment_affinity.popitem(last=False)

    def _preferred_deployment(self, session_key: str | None, *, now: float | None = None) -> str | None:
        """该会话上次成功的部署（过期/不存在则 None）。"""
        if not session_key:
            return None
        entry = self._deployment_affinity.get(session_key)
        if entry is None:
            return None
        deployment_id, expires_at = entry
        moment = now if now is not None else time.time()
        if expires_at <= moment:
            self._deployment_affinity.pop(session_key, None)
            return None
        return deployment_id

    def _apply_deployment_affinity(
        self, candidates: list[Any], session_key: str | None, *, preserve_front: bool = False
    ) -> str | None:
        """把粘性部署提到候选最前；返回被提升的部署 ID（供路由 reason 记录）。

        只在候选里**已存在且 eligible** 时生效——不改计划内容只调顺序，
        故障转移链原样保留。

        ``preserve_front=True``（alias.pin_first）时**不许动 index 0**：
        运营者热切换链首是显式指令，必须立刻生效；粘性是偏好，不能盖指令。
        （2026-10-24：热切换测试被粘性打红，暴露了这个优先级。）
        """
        preferred = self._preferred_deployment(session_key)
        if not preferred:
            return None
        start = 1 if preserve_front else 0
        for index, candidate in enumerate(candidates):
            if (
                index > start
                and candidate.eligible
                and candidate.deployment.id == preferred
                and not self.deployment_cooling_down(preferred)
            ):
                candidates.insert(start, candidates.pop(index))
                return preferred
        return None

    def quarantined_deployments(self, *, now: float | None = None) -> dict[str, dict[str, Any]]:
        """当前被隔离/有失败记录的部署，供 /health 与控制台展示。"""
        moment = now if now is not None else time.time()
        return {
            deployment_id: health.as_dict(moment)
            for deployment_id, health in self._deployment_health.items()
            if health.consecutive_failures or health.quarantined_until
        }

    def unavailable_deployments(self) -> set[str]:
        """调度器现在**不会调用**的部署 id（短冷却 + 自动隔离）。

        交给 Router 当纯数据用（``plan(..., unavailable=...)``），让前端模型不会被
        提到一个马上会被跳过的位置上。健康判断只有这一处，`Router` 依旧不做 I/O。
        """
        return {
            deployment_id
            for deployment_id in (*self._deployment_cooldowns, *self._deployment_health)
            if self.deployment_cooling_down(deployment_id)
        }

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
        return credentials[: self.policy.max_credentials_per_deployment]

    def _admit(self, provider: ProviderConfig, credential: CredentialRuntime) -> bool:
        """Reserve one request in the credential's quota windows, if any are set."""
        limiter = self.pool.rate_limiter
        if limiter is None:
            return True
        return limiter.admit(provider.id, credential.id, credential.tags)

    def _note_tokens(
        self, provider: ProviderConfig, credential: CredentialRuntime, usage: Usage
    ) -> None:
        """Feed a completed request's token total into any token-capped windows."""
        limiter = self.pool.rate_limiter
        if limiter is None:
            return
        limiter.note_tokens(provider.id, credential.id, usage.total_tokens, credential.tags)

    @staticmethod
    def _log_usage(
        candidate: RoutingCandidate,
        credential: CredentialRuntime,
        usage: Usage,
        *,
        estimated: bool = False,
    ) -> None:
        """缓存命中数落日志（2026-10-24 缓存分析 §4-E）。

        ``usage.cached_tokens`` 全链已打通却没有任何地方写进日志，
        ``grep cached data/gateway.log`` 恒为 0——没有这个数字，
        命中率优化就只能靠对账猜。两条成功路径（非流式 / 流式）都调它。
        ``estimated=True`` 时必须标注：网关自己估的 token 数没有 cached 信息，
        不标的话运营者会把「上游没报 usage」误读成「命中率为 0」。
        """
        share = (
            usage.cached_tokens / usage.prompt_tokens * 100.0
            if usage.prompt_tokens
            else 0.0
        )
        logger.info(
            "usage %s/%s key=%s: prompt=%d cached=%d (%.0f%%) completion=%d%s",
            candidate.deployment.provider_id,
            candidate.deployment.model,
            credential.id,
            usage.prompt_tokens,
            usage.cached_tokens,
            share,
            usage.completion_tokens,
            " (estimated)" if estimated else "",
        )

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

    def _normalize_throttle(
        self, info: ErrorInfo, credential: CredentialRuntime | None
    ) -> ErrorInfo:
        """Rewrite a shared-bucket 429 into "fail over", not "sweep sibling keys".

        The classifier always reports a 429 as ``switch_credential`` because that
        is right for a limit the provider counts per key. But when the cooldown
        policy says the bucket is shared across the provider's accounts, rotating
        keys is exactly the failure mode this gateway was fixed for: seven
        SenseNova accounts hit the same tpm bucket, six of the seven attempts are
        guaranteed 429s, and each one escalates the ladder for nothing.

        So for anything wider than one key the decision becomes DEPLOYMENT: the
        pool has already parked the sibling keys (see
        ``CredentialPool._park_throttled_siblings``), and the next candidate in
        the plan is a genuinely different bucket.

        The scope comes from ``CredentialPool.effective_throttle_scope`` - the
        same call the pool uses to decide what to park. Sharing it is the whole
        point: if the two ever disagreed, a request could fail over while the
        pool decided the bucket was per-key (or the reverse), and neither log
        line would explain the behaviour.

        The object is mutated in place on purpose - it is the same instance the
        pool just used for reporting.
        """
        if info.error_class is not ErrorClass.THROTTLE or credential is None:
            return info
        scope = self.pool.effective_throttle_scope(credential.id, info.throttle_kind.value)
        if scope == "credential":
            return info
        info.switch_credential = False
        info.switch_provider = True
        logger.info(
            "429 (%s) has %s-wide cooldown -> failing over instead of rotating keys",
            info.throttle_kind.value,
            scope,
        )
        return info

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
            resolved_model=candidate.model.id,
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
        decision = self.router.plan(request, unavailable=self.unavailable_deployments())
        session_key = self._session_key(request)
        # 缓存粘性：把该会话上次成功的部署提到最前（调顺序不改内容，
        # 故障转移链保留）。不这么做的话 round_robin 会让同一场对话在
        # 多个上游之间来回跳，每轮都撞没暖的前缀缓存（2026-10-24 实测）。
        # pin_first 别名（热切换）优先级更高——运营者指令压过偏好。
        sticky = self._apply_deployment_affinity(
            decision.candidates,
            session_key,
            preserve_front=self._alias_pins_front(decision.alias),
        )
        if sticky:
            logger.info("session %s sticky deployment: %s", (session_key or "")[:20], sticky)
        attempts: list[AttemptOutcome] = []
        errors: list[ErrorInfo] = []
        attempt_number = 0
        deployments_tried = 0
        # Wall-clock origin for the whole request. The loop below rebinds
        # ``started`` to a perf_counter for per-attempt latency, so the budget
        # check needs its own epoch-based value.
        wall_start = time.time()

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
            # 整池都在冷却时先短暂排队（见 _wait_for_credential）：排队期间不消耗
            # 任何上游配额，比立刻 503 对客户端友好得多。等到了就重取一次，仍然
            # 没有才按下面的旧路径记录空尝试并换下一个部署。
            if not credentials and await self._wait_for_credential(candidate, wall_start):
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
                # Admit at the actual dispatch point (not at selection): failover
                # keys that never get tried must not burn their quota.
                if not self._admit(candidate.provider, credential):
                    continue
                retries_done = 0
                # Which loop should we leave when this credential gives up?
                next_step = _Next.RETRY
                while True:
                    if attempt_number >= self.policy.max_total_attempts:
                        raise self._exhausted(decision, attempts, errors)
                    self._check_wall_clock(wall_start)
                    attempt_number += 1
                    provider_var.set(candidate.deployment.provider_id)
                    model_var.set(candidate.deployment.model)
                    credential_var.set(credential.id)
                    attempt_var.set(attempt_number)

                    started = time.perf_counter()
                    started_wall = time.time()
                    ctx = self._context(
                        request,
                        candidate,
                        credential,
                        attempt_number,
                        request_id,
                        attempt_timeout=self._attempt_timeout(
                            wall_start,
                            len(decision.candidates) - deployments_tried + 1,
                        ),
                    )
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
                        info = self._normalize_throttle(info, credential)
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
                        self._note_deployment_failure(candidate.deployment.id, info)

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
                    self._note_deployment_affinity(session_key, candidate.deployment.id)
                    usage = response.usage or Usage()
                    self._note_deployment_success(candidate.deployment.id)
                    self._note_tokens(candidate.provider, credential, usage)
                    self._log_usage(
                        candidate, credential, usage, estimated=response.usage is None
                    )
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
        decision = self.router.plan(request, unavailable=self.unavailable_deployments())
        session_key = self._session_key(request)
        sticky = self._apply_deployment_affinity(
            decision.candidates,
            session_key,
            preserve_front=self._alias_pins_front(decision.alias),
        )
        if sticky:
            logger.info("session %s sticky deployment (stream): %s", (session_key or "")[:20], sticky)
        attempts: list[AttemptOutcome] = []
        errors: list[ErrorInfo] = []
        attempt_number = 0
        deployments_tried = 0
        stream_open_retries = 0
        # Wall-clock origin for the whole stream, shared by every attempt. The
        # non-streaming path gets this from its caller; here it has to be taken
        # explicitly or the budget check has nothing to measure against.
        wall_start = time.time()

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
            if not credentials and await self._wait_for_credential(candidate, wall_start):
                # 与非流式路径同样的排队：整池冷却时等一小会儿再重取，而不是
                # 立刻把一次「上游一次都没发」的空尝试写进审计。
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
                if not self._admit(candidate.provider, credential):
                    continue
                next_step = _Next.RETRY
                while True:
                    if attempt_number >= self.policy.max_total_attempts:
                        raise self._exhausted(decision, attempts, errors)
                    self._check_wall_clock(wall_start)
                    attempt_number += 1
                    provider_var.set(candidate.deployment.provider_id)
                    model_var.set(candidate.deployment.model)
                    credential_var.set(credential.id)
                    attempt_var.set(attempt_number)

                    ctx = self._context(
                        request,
                        candidate,
                        credential,
                        attempt_number,
                        request_id,
                        attempt_timeout=self._attempt_timeout(
                            wall_start,
                            len(decision.candidates) - deployments_tried + 1,
                        ),
                    )
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
                        info = self._normalize_throttle(info, credential)
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
                        self._note_deployment_failure(candidate.deployment.id, info)
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
                    self._note_deployment_success(candidate.deployment.id)
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
                        self._note_deployment_affinity(session_key, candidate.deployment.id)

                    # 估算前先记住「上游没报 usage」：估出来的数字没有 cached 信息，
                    # 落日志时必须带 (estimated) 标记，不许冒充实报命中率。
                    usage_estimated = usage.total_tokens == 0
                    if usage_estimated:
                        # Providers that omit usage during streaming: estimate.
                        usage = Usage.build(
                            request.estimated_input_tokens(),
                            max(0, streamed_chars // 4),
                        )
                    if not cancelled and not stream_error:
                        self._note_tokens(candidate.provider, credential, usage)
                        self._log_usage(
                            candidate, credential, usage, estimated=usage_estimated
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
        *,
        attempt_timeout: float = 0.0,
    ) -> ProviderContext:
        return ProviderContext(
            request_id=request_id,
            deployment=candidate.deployment,
            model=candidate.model,
            credential=credential,
            timeout=self.request_timeout,
            stream=bool(request.stream),
            attempt=attempt_number,
            attempt_timeout=attempt_timeout,
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

    def _attempt_timeout(self, wall_start: float, deployments_left: int) -> float:
        """切给**这一次尝试**的秒数（0 = 不设限，沿用旧行为）。

        ``max_request_seconds`` 管的是整个请求，``_check_wall_clock`` 只在两次
        尝试**之间**检查——它刻意不打断在飞的请求（截断会毁掉客户端还能读的
        响应）。副作用是单条慢腿能把整个预算吃光，剩下的供应商一次都没轮到。

        2026-10-25 实测：一次 AMD 尝试跑了 701 秒，而请求预算只有 300 秒；
        154 条失败请求里 48 条（31%）就是这么死的，另外两家供应商全程没被试。

        所以把「还没用完的墙钟」按**还没试过的部署数**均分。每换一条腿，
        剩余预算重新均分，慢腿拿到的份额自然越来越小，但总能剩下一些给
        后面的腿。不低于 ``attempt_timeout_floor``：给一个 100k prefill 切
        15 秒是必超时，那等于「每条腿都失败」，比现在的病更糟。
        """
        floor = self.policy.attempt_timeout_floor
        if floor <= 0 or self.max_request_seconds <= 0:
            return 0.0
        remaining = self.max_request_seconds - (time.time() - wall_start)
        if remaining <= 0:
            return 0.0
        legs = max(1, deployments_left)
        margin = self.policy.attempt_timeout_margin
        return max(floor, remaining / legs - margin)

    async def _wait_for_credential(
        self, candidate: RoutingCandidate, wall_start: float
    ) -> bool:
        """整池都在冷却时**短暂排队**，而不是立刻 503。

        现在一个请求最多扫 ``max_credentials_per_deployment`` 把 Key，每把 429
        都会给它挂上 60 秒冷却。扫完就把整池打光，紧接着的那批请求在
        ``next_available_in`` 返回前会拿到瞬时的 ``no_available_credential``
        ——实测 0.01 秒返回、一次上游都没发（2026-10-24 起 154 条失败里
        55 条 25%~36% 是这个形状）。

        客户端的 10 req/min 与商汤 27 req/min 的容量本来是够的，缺的只是
        让渡。所以这里等一小会儿（上限 ``_CREDENTIAL_WAIT_SECONDS``，且不越过
        墙钟预算），把「拒绝」换成「稍等」。等不到才如实报 503 + Retry-After。
        """
        wait = self.pool.next_available_in(candidate.deployment.provider_id)
        if wait is None or wait <= 0:
            return False
        budget_left = self.max_request_seconds - (time.time() - wall_start)
        pause = min(wait, _CREDENTIAL_WAIT_SECONDS)
        if self.max_request_seconds > 0:
            # 留一点给真正的那次尝试，别把预算全睡掉。
            pause = min(pause, max(0.0, budget_left - self.policy.attempt_timeout_floor))
        if pause < _CREDENTIAL_WAIT_MIN_SECONDS:
            return False
        logger.info(
            "all keys of %s are cooling down; waiting %.1fs instead of failing fast",
            candidate.deployment.provider_id,
            pause,
        )
        await self._sleep(pause)
        return True

    def _check_wall_clock(self, started: float) -> None:
        """Abort a request that has spent its whole wall-clock budget failing.

        Guarded on ``max_request_seconds`` (0 = disabled). Raising *before* the
        next attempt is dispatched is what makes it a budget rather than a
        timeout: the client gets an actionable 504 listing how long each failing
        attempt took, instead of silence for however long the attempt ceiling
        happens to allow.

        It deliberately does **not** interrupt an attempt already in flight -
        aborting mid-stream would truncate a response the client can still read
        (and would leave the upstream connection to be reaped). The check only
        runs between attempts.
        """
        if self.max_request_seconds <= 0:
            return
        elapsed = time.time() - started
        if elapsed < self.max_request_seconds:
            return
        logger.warning(
            "请求已达墙钟预算 %.0fs（已尝试若干次，最后一次失败后不再重试）",
            elapsed,
        )
        raise GatewayTimeoutError(
            f"网关在 {elapsed:.0f} 秒内未能从任何渠道拿到响应"
            f"（上限 {self.max_request_seconds:.0f} 秒，由 ZKAI_MAX_REQUEST_SECONDS 控制）。"
            "通常是所有渠道同时不可用：可稍后重试、换个别名，或把上限调大。"
        )

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
