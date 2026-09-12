"""Error classification - the heart of the retry / failover decisions.

The classifier turns *any* exception into an :class:`ErrorInfo` that answers four
questions the scheduler needs:

1. ``retryable``        - may we retry the very same credential?
2. ``switch_credential``- should we put this credential aside and try a sibling?
3. ``switch_provider``  - should we fail over to the next deployment?
4. ``cooldown_seconds`` - for how long should the credential / deployment rest?

Client mistakes (400/413/409/404-parameter) never rotate credentials, which is
what the spec calls out explicitly.

Decision matrix (the single source of truth for retry / rotation / failover)::

    status / cause      retryable  switch_cred  switch_provider  cooldown
    ------------------  ---------  -----------  ---------------  --------------
    400  bad request        no          no             no         -
    401  bad key            no         YES             no         credential
    403  no permission      no         YES             no         credential
    404  wrong model        no          no            YES         -
    408  request timeout   YES          no             no         -
    409  conflict           no          no             no         -
    413  too large          no          no             no         -
    422  unprocessable      no          no             no         -
    429  rate limited       no         YES             no         credential
    500  upstream bug      YES         YES            YES         -
    502  bad gateway       YES         YES            YES         -
    503  unavailable       YES          no            YES         deployment
    504  gateway timeout   YES          no            YES         deployment
    529  overloaded        YES          no            YES         deployment
    timeout (transport)    YES          no            YES         deployment
    connection error       YES         YES            YES         credential
    unknown                no          no            YES         -

Note that ``switch_credential`` means "after the per-credential retry budget is
spent, try a sibling key of the *same* deployment before failing over".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from app.core.errors import (
    AllAttemptsFailedError,
    AuthenticationError,
    BadGatewayError,
    ClientDisconnected,
    ConfigError,
    ConflictError,
    ConnectionFailureError,
    ContentFilterError,
    ContextLengthExceededError,
    GatewayTimeoutError,
    InvalidRequestError,
    ModelNotFoundError,
    NoAvailableCredentialError,
    OverloadedError,
    PermissionDeniedError,
    ProviderNotFoundError,
    RateLimitError,
    RequestTimeoutError,
    ServiceUnavailableError,
    UnsupportedParameterError,
    UpstreamServerError,
    UpstreamUnknownError,
    ZKAIError,
)

#: Where a cooldown is applied.
CooldownScope = str  # "none" | "credential" | "deployment" | "provider"

#: 429 bodies that mean "the plan/credit quota is gone until it resets" rather
#: than "slow down for a minute". Matched case-insensitively against the
#: upstream message. SenseNova: "token plan entitlement exhausted".
QUOTA_EXHAUSTION_PATTERNS: tuple[str, ...] = (
    "entitlement exhausted",
    "quota exhausted",
    "quota exceeded",
    "out of quota",
    "insufficient balance",
    "insufficient credits",
    "余额不足",
    "额度已用尽",
    "额度用尽",
    "每日限额",
)


def looks_like_quota_exhaustion(message: str | None) -> bool:
    """True when a 429 message describes plan quota, not a per-minute window."""
    if not message:
        return False
    lowered = message.lower()
    return any(pattern.lower() in lowered for pattern in QUOTA_EXHAUSTION_PATTERNS)


class ErrorClass(str, Enum):
    """Coarse buckets surfaced in statistics."""

    CLIENT = "client"          # the caller must fix the request
    CREDENTIAL = "credential"  # this key is bad / lacks rights
    THROTTLE = "throttle"      # rate limited
    TRANSIENT = "transient"    # retry, likely to succeed
    AVAILABILITY = "availability"  # upstream degraded -> failover
    INTERNAL = "internal"      # gateway bug / unexpected


@dataclass(slots=True)
class ErrorInfo:
    """Normalised error description consumed by the scheduler."""

    error_type: str
    error_class: ErrorClass
    http_status: int
    message: str
    retryable: bool = False
    switch_credential: bool = False
    switch_provider: bool = False
    cooldown_scope: CooldownScope = "none"
    cooldown_seconds: float = 0.0
    #: HTTP status reported back to the client when everything failed.
    client_status: int = 502
    retry_after: float | None = None
    #: True when a 429 means "plan/credit quota exhausted" (hours to days until
    #: reset) rather than "you crossed a per-minute tpm/rpm window". Quota
    #: exhaustion gets a long flat cooldown instead of exponential back-off, so
    #: the scheduler stops sweeping every sibling key on every request.
    quota_exhausted: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def is_request_error(self) -> bool:
        """True when the failure is the caller's fault (no failover allowed)."""
        return self.error_class is ErrorClass.CLIENT

    def to_error(self, *, provider: str | None = None, model: str | None = None) -> ZKAIError:
        """Rehydrate a typed exception for the API layer."""
        mapping: dict[str, type[ZKAIError]] = {
            "invalid_request_error": InvalidRequestError,
            "unsupported_parameter": UnsupportedParameterError,
            "context_length_exceeded": ContextLengthExceededError,
            "content_filter": ContentFilterError,
            "conflict_error": ConflictError,
            "authentication_error": AuthenticationError,
            "permission_denied": PermissionDeniedError,
            "rate_limit_error": RateLimitError,
            "timeout": GatewayTimeoutError,
            "connection_error": ConnectionFailureError,
            "upstream_error": UpstreamServerError,
            "overloaded": OverloadedError,
            "model_not_found": ModelNotFoundError,
            "unknown_error": UpstreamUnknownError,
        }
        cls = mapping.get(self.error_type, UpstreamUnknownError)
        error = cls(
            self.message,
            provider=provider,
            model=model,
            http_status=self.http_status or None,
            raw=self.raw,
            retry_after=self.retry_after,
        )
        error.http_status = self.client_status
        return error


# --------------------------------------------------------------------------- #
# Retry-After parsing
# --------------------------------------------------------------------------- #
def parse_retry_after(headers: dict[str, str] | None) -> float | None:
    """Parse the ``Retry-After`` header (seconds or HTTP date)."""
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:  # HTTP-date form
        import datetime as _dt
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=_dt.UTC)
        delta = (target - _dt.datetime.now(_dt.UTC)).total_seconds()
        return max(0.0, delta)
    except Exception:  # pragma: no cover - malformed header
        return None


class ErrorClassifier:
    """Map HTTP status codes and transport exceptions onto :class:`ErrorInfo`."""

    def __init__(self, *, default_cooldown: float = 60.0, rate_limit_cooldown: float = 60.0,
                 deployment_cooldown: float = 30.0) -> None:
        self.default_cooldown = default_cooldown
        self.rate_limit_cooldown = rate_limit_cooldown
        self.deployment_cooldown = deployment_cooldown

    # ------------------------------------------------------------------ #
    # Entry points
    # ------------------------------------------------------------------ #
    def classify(self, exc: BaseException, *, retry_after: float | None = None) -> ErrorInfo:
        """Classify an exception or a :class:`ZKAIError`."""
        if isinstance(exc, AllAttemptsFailedError):
            return ErrorInfo(
                error_type=exc.error_type,
                error_class=ErrorClass.AVAILABILITY,
                http_status=exc.http_status,
                message=exc.message,
                client_status=exc.http_status,
            )
        if isinstance(exc, ClientDisconnected):
            return ErrorInfo(
                error_type="client_disconnected",
                error_class=ErrorClass.CLIENT,
                http_status=499,
                message=exc.message,
                client_status=499,
            )
        if isinstance(exc, ZKAIError):
            return self._from_zkai(exc, retry_after=retry_after)
        if isinstance(exc, httpx.TimeoutException):
            return self._timeout(str(exc) or "upstream timeout")
        if isinstance(exc, httpx.TransportError):
            return self._connection(str(exc) or "transport error")
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return self._timeout(str(exc) or "asyncio timeout")
        if isinstance(exc, (asyncio.CancelledError,)):
            raise exc  # never swallow cancellation
        return self._unknown(exc)

    def classify_status(
        self,
        status: int,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
        message: str | None = None,
    ) -> ErrorInfo:
        """Classify a raw upstream HTTP response."""
        retry_after = parse_retry_after(headers)
        detail = message or self._extract_message(body) or f"upstream returned HTTP {status}"
        return self._by_status(status, detail, retry_after=retry_after, body=body)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_message(body: Any) -> str | None:
        """Pull a human readable message out of the many provider error shapes."""
        if isinstance(body, str):
            return body[:500] or None
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        if isinstance(error, str):
            return error[:500]
        if isinstance(error, dict):
            for key in ("message", "detail", "msg"):
                value = error.get(key)
                if isinstance(value, str):
                    return value[:500]
        for key in ("message", "detail", "error_description", "msg"):
            value = body.get(key)
            if isinstance(value, str):
                return value[:500]
        return None

    def _by_status(
        self,
        status: int,
        detail: str,
        *,
        retry_after: float | None = None,
        body: Any = None,
    ) -> ErrorInfo:
        raw = {"body": body} if body is not None else {}
        if status == 400:
            # Never rotate a key for a malformed request.
            return ErrorInfo(
                "invalid_request_error", ErrorClass.CLIENT, 400, detail,
                retryable=False, switch_credential=False, switch_provider=False,
                client_status=400, raw=raw,
            )
        if status == 401:
            return ErrorInfo(
                "authentication_error", ErrorClass.CREDENTIAL, 401, detail,
                retryable=False, switch_credential=True, switch_provider=False,
                cooldown_scope="credential", cooldown_seconds=0.0,
                client_status=401, raw=raw,
            )
        if status == 403:
            return ErrorInfo(
                "permission_denied", ErrorClass.CREDENTIAL, 403, detail,
                retryable=False, switch_credential=True, switch_provider=False,
                cooldown_scope="credential", cooldown_seconds=0.0,
                client_status=403, raw=raw,
            )
        if status == 404:
            # Ambiguous: could be a wrong model/endpoint. Do not burn keys.
            return ErrorInfo(
                "model_not_found", ErrorClass.CLIENT, 404, detail,
                retryable=False, switch_credential=False, switch_provider=True,
                client_status=404, raw=raw,
            )
        if status == 408:
            return ErrorInfo(
                "timeout", ErrorClass.TRANSIENT, 408, detail,
                retryable=True, switch_credential=False, switch_provider=False,
                client_status=408, raw=raw,
            )
        if status == 409:
            return ErrorInfo(
                "conflict_error", ErrorClass.CLIENT, 409, detail,
                retryable=False, switch_credential=False, switch_provider=False,
                client_status=409, raw=raw,
            )
        if status == 413:
            # Context / payload too large: rotating keys cannot help.
            return ErrorInfo(
                "context_length_exceeded", ErrorClass.CLIENT, 413, detail,
                retryable=False, switch_credential=False, switch_provider=False,
                client_status=413, raw=raw,
            )
        if status == 422:
            return ErrorInfo(
                "invalid_request_error", ErrorClass.CLIENT, 422, detail,
                retryable=False, switch_credential=False, switch_provider=False,
                client_status=422, raw=raw,
            )
        if status == 429:
            quota = looks_like_quota_exhaustion(detail)
            cooldown = retry_after if retry_after else self.rate_limit_cooldown
            return ErrorInfo(
                "rate_limit_error", ErrorClass.THROTTLE, 429, detail,
                retryable=False, switch_credential=True, switch_provider=False,
                cooldown_scope="credential", cooldown_seconds=cooldown,
                client_status=429, retry_after=retry_after, raw=raw,
                quota_exhausted=quota,
            )
        if status == 500:
            # Internal upstream error: retry with backoff, then try a sibling key
            # (another key often lands on a different backend node), then fail over.
            return ErrorInfo(
                "upstream_error", ErrorClass.TRANSIENT, 500, detail,
                retryable=True, switch_credential=True, switch_provider=True,
                client_status=500, raw=raw,
            )
        if status == 502:
            return ErrorInfo(
                "upstream_error", ErrorClass.TRANSIENT, 502, detail,
                retryable=True, switch_credential=True, switch_provider=True,
                client_status=502, raw=raw,
            )
        if status == 503:
            return ErrorInfo(
                "upstream_error", ErrorClass.AVAILABILITY, 503, detail,
                retryable=True, switch_credential=False, switch_provider=True,
                cooldown_scope="deployment", cooldown_seconds=self.deployment_cooldown,
                client_status=503, raw=raw,
            )
        if status == 504:
            return ErrorInfo(
                "timeout", ErrorClass.TRANSIENT, 504, detail,
                retryable=True, switch_credential=False, switch_provider=True,
                cooldown_scope="deployment", cooldown_seconds=self.deployment_cooldown,
                client_status=504, raw=raw,
            )
        if status == 529:
            # Anthropic style explicit overload -> short deployment cooldown + failover.
            return ErrorInfo(
                "overloaded", ErrorClass.AVAILABILITY, 529, detail,
                retryable=True, switch_credential=False, switch_provider=True,
                cooldown_scope="deployment", cooldown_seconds=self.deployment_cooldown,
                client_status=529, raw=raw,
            )
        if 400 <= status < 500:
            return ErrorInfo(
                "invalid_request_error", ErrorClass.CLIENT, status, detail,
                retryable=False, switch_credential=False, switch_provider=False,
                client_status=status, raw=raw,
            )
        if status >= 500:
            return ErrorInfo(
                "upstream_error", ErrorClass.AVAILABILITY, status, detail,
                retryable=True, switch_credential=False, switch_provider=True,
                cooldown_scope="deployment", cooldown_seconds=self.deployment_cooldown,
                client_status=status, raw=raw,
            )
        return self._unknown(RuntimeError(detail))

    def _from_zkai(self, exc: ZKAIError, *, retry_after: float | None) -> ErrorInfo:
        status = getattr(exc, "http_status", 502)
        if exc.error_type == "timeout":
            return self._timeout(exc.message, retry_after=retry_after)
        if exc.error_type == "connection_error":
            return self._connection(exc.message)
        if exc.error_type in {"no_available_credential", "no_available_deployment"}:
            return ErrorInfo(
                exc.error_type, ErrorClass.AVAILABILITY, status or 503, exc.message,
                retryable=False, switch_credential=False, switch_provider=True,
                cooldown_scope="deployment", cooldown_seconds=self.deployment_cooldown,
                client_status=status or 503,
                retry_after=exc.retry_after,
            )
        if isinstance(exc, (ModelNotFoundError, ProviderNotFoundError, ConfigError)):
            # Wrong model/endpoint: failover once, never blame the credential.
            return ErrorInfo(
                exc.error_type, ErrorClass.CLIENT, status or 404, exc.message,
                retryable=False, switch_credential=False, switch_provider=True,
                client_status=status or 404,
            )
        if isinstance(exc, (PermissionDeniedError, AuthenticationError)):
            return self._by_status(status or 401, exc.message)
        if isinstance(exc, RateLimitError):
            return self._by_status(429, exc.message, retry_after=retry_after or exc.retry_after)
        if isinstance(exc, (UnsupportedParameterError, InvalidRequestError,
                            ContextLengthExceededError, ContentFilterError, ConflictError)):
            return self._by_status(status or 400, exc.message)
        if isinstance(exc, OverloadedError):
            return self._by_status(529, exc.message)
        if isinstance(exc, NoAvailableCredentialError):
            return ErrorInfo(
                exc.error_type, ErrorClass.AVAILABILITY, 503, exc.message,
                retryable=False, switch_credential=False, switch_provider=True,
                client_status=503,
            )
        if isinstance(exc, (BadGatewayError, ServiceUnavailableError)):
            return self._by_status(status or 503, exc.message)
        if isinstance(exc, (UpstreamServerError,)):
            return self._by_status(status or 500, exc.message)
        if isinstance(exc, (RequestTimeoutError, GatewayTimeoutError)):
            return self._timeout(exc.message)
        return ErrorInfo(
            exc.error_type, ErrorClass.INTERNAL, status or 502, exc.message,
            retryable=False, switch_credential=False, switch_provider=True,
            client_status=status or 502, raw=exc.raw,
        )

    def _timeout(self, detail: str, *, retry_after: float | None = None) -> ErrorInfo:
        return ErrorInfo(
            "timeout", ErrorClass.TRANSIENT, 408, detail or "upstream timeout",
            retryable=True, switch_credential=False, switch_provider=True,
            cooldown_scope="deployment", cooldown_seconds=self.deployment_cooldown,
            client_status=504, retry_after=retry_after,
        )

    def _connection(self, detail: str) -> ErrorInfo:
        return ErrorInfo(
            "connection_error", ErrorClass.TRANSIENT, 502, detail or "connection error",
            retryable=True, switch_credential=True, switch_provider=True,
            cooldown_scope="credential", cooldown_seconds=0.0,
            client_status=502,
        )

    def _unknown(self, exc: BaseException) -> ErrorInfo:
        return ErrorInfo(
            "unknown_error", ErrorClass.INTERNAL, 500,
            f"{type(exc).__name__}: {exc}"[:500],
            retryable=False, switch_credential=False, switch_provider=True,
            client_status=502,
        )
