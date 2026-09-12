"""Credential health state machine.

This module owns *all* status transitions. Keeping them in one place makes the
"why did this key get parked" question answerable from a single log line, and
keeps :class:`~app.credentials.pool.CredentialPool` free of business rules.

Transition table
----------------
============================  ==========================================
failure class                 transition
============================  ==========================================
401 authentication            -> UNHEALTHY (revive via health check/admin)
403 permission                -> UNHEALTHY (reason: permission)
429 / 529 overload            -> COOLDOWN (Retry-After aware, exponential)
5xx / timeout / connection    -> counters only; COOLDOWN after N consecutive
400 / 413 / 409 / 422         -> **no transition** (caller's fault)
success                       -> HEALTHY, counters reset, latency EMA updated
cooldown expiry               -> HEALTHY (lazy recovery on next selection)
============================  ==========================================
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.logging import get_logger
from app.credentials.cooldown import CooldownPolicy
from app.models.credential import CredentialRuntime, CredentialStatus
from app.retry.classifier import ErrorClass, ErrorInfo

logger = get_logger("credentials.health")

#: Errors that prove the credential itself is unusable.
CREDENTIAL_FATAL = frozenset({"authentication_error", "permission_denied"})
#: Errors that park the *credential* for a while. Note that 529 (overloaded) is
#: deliberately absent: it is a provider/model level problem handled by the
#: scheduler's deployment cooldown, so the key stays usable for other models.
THROTTLE_ERRORS = frozenset({"rate_limit_error"})


@dataclass(slots=True)
class Transition:
    """Record of a status change (or a no-op) for logging / statistics."""

    credential_id: str
    provider_id: str
    previous: CredentialStatus
    current: CredentialStatus
    reason: str
    cooldown_until: float | None = None

    @property
    def changed(self) -> bool:
        return self.previous is not self.current


class CredentialHealthTracker:
    """Apply success/failure events to :class:`CredentialRuntime` objects."""

    def __init__(self, policy: CooldownPolicy | None = None) -> None:
        self.policy = policy or CooldownPolicy()

    # ------------------------------------------------------------------ #
    # Success
    # ------------------------------------------------------------------ #
    def on_success(
        self, credential: CredentialRuntime, *, latency_ms: float = 0.0, now: float | None = None
    ) -> Transition:
        moment = now if now is not None else time.time()
        previous = credential.status
        credential.success_count += 1
        credential.consecutive_failures = 0
        credential.consecutive_rate_limits = 0
        credential.last_success_at = moment
        credential.status = CredentialStatus.HEALTHY
        credential.cooldown_until = None
        credential.last_error_type = None
        credential.last_error_detail = None
        if credential.disabled_reason == "auto-recovered":
            credential.disabled_reason = None
        if latency_ms > 0:
            alpha = 0.3
            credential.latency_ema_ms = (
                latency_ms
                if credential.latency_ema_ms <= 0
                else (1 - alpha) * credential.latency_ema_ms + alpha * latency_ms
            )
        return Transition(
            credential_id=credential.id,
            provider_id=credential.provider_id,
            previous=previous,
            current=credential.status,
            reason="success",
        )

    # ------------------------------------------------------------------ #
    # Failure
    # ------------------------------------------------------------------ #
    def on_failure(
        self, credential: CredentialRuntime, info: ErrorInfo, *, now: float | None = None
    ) -> Transition:
        moment = now if now is not None else time.time()
        previous = credential.status

        # Client mistakes must not penalise the credential (no rotation for
        # 400/404/413/409/422). We still record *what* happened for the operator,
        # but the failure counters stay untouched: otherwise a malformed request
        # would corrupt ``success_rate``, the ``least_failures`` rotation order
        # and the error rates reported by ``/admin/stats``.
        if info.error_class is ErrorClass.CLIENT:
            credential.last_error_at = moment
            credential.last_error_type = info.error_type
            credential.last_error_detail = (info.message or "")[:300]
            return Transition(
                credential_id=credential.id,
                provider_id=credential.provider_id,
                previous=previous,
                current=credential.status,
                reason=f"client-error:{info.error_type}",
            )

        if info.error_class is ErrorClass.INTERNAL:
            # A gateway-side failure (e.g. an upstream payload we could not
            # parse). Nothing here is evidence that the *key* is bad - and
            # parking healthy keys for our own bug is how one parse error used
            # to black out the whole pool. Record, do not penalise.
            credential.last_error_at = moment
            credential.last_error_type = info.error_type
            credential.last_error_detail = (info.message or "")[:300]
            return Transition(
                credential_id=credential.id,
                provider_id=credential.provider_id,
                previous=previous,
                current=credential.status,
                reason=f"internal:{info.error_type}",
            )

        credential.failure_count += 1
        credential.last_error_at = moment
        credential.last_error_type = info.error_type
        credential.last_error_detail = (info.message or "")[:300]

        if info.error_type in CREDENTIAL_FATAL:
            credential.consecutive_failures += 1
            credential.status = CredentialStatus.UNHEALTHY
            credential.cooldown_until = None
            credential.disabled_reason = (
                "Key 无效（401 认证失败）" if info.error_type == "authentication_error"
                else "无权限（403）"
            )
            logger.warning(
                "credential %s marked UNHEALTHY (%s): %s",
                credential.id,
                info.error_type,
                credential.disabled_reason,
            )
            return Transition(
                credential_id=credential.id,
                provider_id=credential.provider_id,
                previous=previous,
                current=credential.status,
                reason=credential.disabled_reason or info.error_type,
            )

        if info.error_type in THROTTLE_ERRORS:
            credential.rate_limit_count += 1
            credential.consecutive_rate_limits += 1
            credential.consecutive_failures += 1
            # The cooldown policy owns the duration so that repeated throttling
            # backs off exponentially; a provider supplied Retry-After wins.
            # Plan-quota exhaustion skips the ladder entirely (long flat rest).
            duration = self.policy.cooldown_for(
                error_type=info.error_type,
                scope=info.cooldown_scope or "credential",
                retry_after=info.retry_after,
                consecutive_rate_limits=credential.consecutive_rate_limits,
                quota_exhausted=info.quota_exhausted,
            )
            credential.status = CredentialStatus.COOLDOWN
            credential.cooldown_until = moment + duration if duration > 0 else None
            credential.disabled_reason = (
                "套餐额度耗尽长休" if info.quota_exhausted else "限流冷却中"
            )
            logger.info(
                "credential %s -> COOLDOWN %.1fs (%s, consecutive=%d)",
                credential.id,
                duration,
                info.error_type,
                credential.consecutive_rate_limits,
            )
            return Transition(
                credential_id=credential.id,
                provider_id=credential.provider_id,
                previous=previous,
                current=credential.status,
                reason=f"{info.error_type} cooldown",
                cooldown_until=credential.cooldown_until,
            )

        # Transient / availability failures: count, and only park the key after
        # repeated failures (the provider is usually at fault, not the key).
        credential.consecutive_failures += 1
        if credential.consecutive_failures >= credential.max_consecutive_failures:
            duration = info.cooldown_seconds or self.policy.server_error_cooldown
            credential.status = CredentialStatus.COOLDOWN
            credential.cooldown_until = moment + duration
            credential.disabled_reason = f"连续失败冷却（{info.error_type}）"
            logger.warning(
                "credential %s -> COOLDOWN %.1fs after %d consecutive failures (%s)",
                credential.id,
                duration,
                credential.consecutive_failures,
                info.error_type,
            )
            return Transition(
                credential_id=credential.id,
                provider_id=credential.provider_id,
                previous=previous,
                current=credential.status,
                reason=f"{info.error_type} repeated",
                cooldown_until=credential.cooldown_until,
            )
        return Transition(
            credential_id=credential.id,
            provider_id=credential.provider_id,
            previous=previous,
            current=credential.status,
            reason=f"transient:{info.error_type}",
        )

    # ------------------------------------------------------------------ #
    # Administrative actions
    # ------------------------------------------------------------------ #
    def mark_disabled(
        self, credential: CredentialRuntime, reason: str = "disabled by operator"
    ) -> Transition:
        previous = credential.status
        credential.status = CredentialStatus.DISABLED
        credential.enabled = False
        credential.disabled_reason = reason
        return Transition(
            credential_id=credential.id,
            provider_id=credential.provider_id,
            previous=previous,
            current=credential.status,
            reason=reason,
        )

    def mark_enabled(
        self, credential: CredentialRuntime, *, now: float | None = None
    ) -> Transition:
        # ``now`` is accepted for signature symmetry with the sibling transitions;
        # enabling records no timestamp of its own.
        _ = now
        previous = credential.status
        credential.status = CredentialStatus.HEALTHY
        credential.enabled = True
        credential.disabled_reason = None
        credential.cooldown_until = None
        credential.consecutive_failures = 0
        credential.consecutive_rate_limits = 0
        return Transition(
            credential_id=credential.id,
            provider_id=credential.provider_id,
            previous=previous,
            current=credential.status,
            reason="enabled",
        )

    def mark_from_health_check(
        self,
        credential: CredentialRuntime,
        *,
        healthy: bool,
        detail: str | None = None,
        error_type: str | None = None,
        now: float | None = None,
    ) -> Transition:
        """Apply the outcome of a probe (startup / scheduled / manual).

        Only credential-class failures (401/403) are fatal: a timeout or a
        transient 429 on the probe endpoint is *not* evidence of a bad key, and
        parking the key until an operator intervenes is how one flaky network
        permanently kills a healthy pool.
        """
        if healthy:
            if credential.status in {CredentialStatus.UNHEALTHY, CredentialStatus.COOLDOWN}:
                transition = self.mark_enabled(credential, now=now)
                transition.reason = "health check passed"
                return transition
            return Transition(
                credential_id=credential.id,
                provider_id=credential.provider_id,
                previous=credential.status,
                current=credential.status,
                reason="health check passed",
            )
        moment = now if now is not None else time.time()
        if error_type and error_type not in CREDENTIAL_FATAL:
            credential.last_error_at = moment
            credential.last_error_type = error_type
            credential.last_error_detail = (detail or "")[:300]
            return Transition(
                credential_id=credential.id,
                provider_id=credential.provider_id,
                previous=credential.status,
                current=credential.status,
                reason=f"probe failed (transient): {error_type}",
            )
        return self.on_failure(
            credential,
            ErrorInfo(
                error_type=error_type or "authentication_error",
                error_class=ErrorClass.CREDENTIAL,
                http_status=403 if error_type == "permission_denied" else 401,
                message=detail or "health check failed",
            ),
            now=now,
        )

    # ------------------------------------------------------------------ #
    # Lazy recovery
    # ------------------------------------------------------------------ #
    def refresh(
        self, credential: CredentialRuntime, *, now: float | None = None
    ) -> Transition | None:
        """Recover a credential whose cooldown expired. Returns None when unchanged."""
        moment = now if now is not None else time.time()
        previous: CredentialStatus = credential.status
        if credential.status is CredentialStatus.UNHEALTHY:
            # A credential parked by 401/403 may just have hit a transient upstream
            # glitch; ``auto_recover_after`` gives it one free retry instead of
            # parking it until an operator notices. A genuinely revoked key is
            # re-parked on the next failure, costing one probe request per window.
            last_error = credential.last_error_at
            if (
                last_error is not None
                and moment - last_error >= self.policy.auto_recover_after
            ):
                credential.status = CredentialStatus.HEALTHY
                credential.consecutive_failures = 0
                credential.disabled_reason = "auto-recovered"
                logger.info("credential %s auto-recovered after UNHEALTHY rest", credential.id)
                return Transition(
                    credential_id=credential.id,
                    provider_id=credential.provider_id,
                    previous=previous,
                    current=credential.status,
                    reason="auto-recovered after rest",
                )
            return None
        if credential.status is not CredentialStatus.COOLDOWN:
            return None
        moment = now if now is not None else time.time()
        if credential.cooldown_until is not None and credential.cooldown_until > moment:
            return None
        credential.status = CredentialStatus.HEALTHY
        credential.cooldown_until = None
        credential.consecutive_failures = 0
        credential.disabled_reason = "auto-recovered"
        # The rate-limit ladder must survive a *short* rest (cooldown 60s ->
        # immediately throttled again -> 120s), otherwise the exponential
        # backoff never engages. But once the key has quietly rested through a
        # full *max* cooldown, the history is stale: without this reset a
        # burst of throttling permanently pins every key at the 900s ceiling
        # and the user's wait buys no recovery (observed live: zk-k3 503
        # storms for 15+ minutes on SenseNova's rolling 5h quota windows).
        if (
            credential.last_error_at is not None
            and moment - credential.last_error_at >= self.policy.rate_limit_max
        ):
            credential.consecutive_rate_limits = 0
        logger.info("credential %s cooldown expired -> HEALTHY", credential.id)
        return Transition(
            credential_id=credential.id,
            provider_id=credential.provider_id,
            previous=previous,
            current=credential.status,
            reason="cooldown expired",
        )
