"""Exception hierarchy for ZK-AI.

Every error that leaves the gateway is a subclass of :class:`ZKAIError` so that the
API layer can always translate it into a stable JSON payload without leaking
upstream details (and never leaking credentials).
"""

from __future__ import annotations

from typing import Any


class ZKAIError(Exception):
    """Base class for all ZK-AI errors."""

    #: Stable machine readable identifier surfaced through the API.
    error_type: str = "zkai_error"
    #: Default HTTP status code used by the API layer.
    http_status: int = 500
    #: Whether the error is safe to retry against the *same* credential.
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        http_status: int | None = None,
        raw: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model = model
        self.raw = raw or {}
        self.retry_after = retry_after
        if http_status is not None:
            self.http_status = http_status

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        """Serialise the error. ``raw`` is opt-in because it may contain upstream text."""
        payload: dict[str, Any] = {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.http_status,
            }
        }
        if self.provider:
            payload["error"]["provider"] = self.provider
        if self.model:
            payload["error"]["model"] = self.model
        if self.retry_after is not None:
            payload["error"]["retry_after"] = self.retry_after
        if include_raw and self.raw:
            payload["error"]["raw"] = self.raw
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"{type(self).__name__}(message={self.message!r}, provider={self.provider!r})"


# --------------------------------------------------------------------------- #
# Configuration / routing errors
# --------------------------------------------------------------------------- #
class ConfigError(ZKAIError):
    error_type = "config_error"
    http_status = 500


class ProviderNotFoundError(ZKAIError):
    error_type = "provider_not_found"
    http_status = 503


class ModelNotFoundError(ZKAIError):
    error_type = "model_not_found"
    http_status = 404


class AliasNotFoundError(ModelNotFoundError):
    error_type = "alias_not_found"


# --------------------------------------------------------------------------- #
# Request errors (client's fault -> never rotate credentials)
# --------------------------------------------------------------------------- #
class InvalidRequestError(ZKAIError):
    error_type = "invalid_request_error"
    http_status = 400


class UnsupportedParameterError(InvalidRequestError):
    error_type = "unsupported_parameter"


class ContextLengthExceededError(InvalidRequestError):
    error_type = "context_length_exceeded"
    http_status = 413


class ContentFilterError(InvalidRequestError):
    error_type = "content_filter"


class ConflictError(ZKAIError):
    error_type = "conflict_error"
    http_status = 409


# --------------------------------------------------------------------------- #
# Credential errors
# --------------------------------------------------------------------------- #
class CredentialError(ZKAIError):
    error_type = "credential_error"
    http_status = 401


class AuthenticationError(CredentialError):
    """401 - the credential itself is bad."""

    error_type = "authentication_error"


class PermissionDeniedError(CredentialError):
    """403 - the credential is valid but lacks permission / quota."""

    error_type = "permission_denied"
    http_status = 403


class NoAvailableCredentialError(ZKAIError):
    """The pool has no usable credential for this provider."""

    error_type = "no_available_credential"
    http_status = 503


# --------------------------------------------------------------------------- #
# Throttling / upstream availability errors
# --------------------------------------------------------------------------- #
class RateLimitError(ZKAIError):
    """429 - throttle the current credential and move on."""

    error_type = "rate_limit_error"
    http_status = 429
    retryable = False


class RequestTimeoutError(ZKAIError):
    """408 / client-side timeout."""

    error_type = "timeout"
    http_status = 408
    retryable = True


class ConnectionFailureError(ZKAIError):
    """Transport level failure (DNS, reset, TLS...)."""

    error_type = "connection_error"
    http_status = 502
    retryable = True


class UpstreamServerError(ZKAIError):
    error_type = "upstream_error"
    http_status = 500
    retryable = True


class BadGatewayError(UpstreamServerError):
    http_status = 502


class ServiceUnavailableError(UpstreamServerError):
    http_status = 503


class GatewayTimeoutError(RequestTimeoutError):
    http_status = 504


class OverloadedError(UpstreamServerError):
    """529 - provider explicitly reports being overloaded (Anthropic style)."""

    error_type = "overloaded"
    http_status = 529


class UpstreamUnknownError(ZKAIError):
    error_type = "unknown_error"
    http_status = 502


# --------------------------------------------------------------------------- #
# Aggregated failures
# --------------------------------------------------------------------------- #
class NoAvailableDeploymentError(ZKAIError):
    error_type = "no_available_deployment"
    http_status = 503


class AllAttemptsFailedError(ZKAIError):
    """Raised when every deployment/credential combination was exhausted."""

    error_type = "all_attempts_failed"
    http_status = 503

    def __init__(self, message: str, *, attempts: list[Any] | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        payload = super().to_dict(include_raw=include_raw)
        payload["error"]["attempts"] = [
            a.to_dict() if hasattr(a, "to_dict") else dict(a) for a in self.attempts
        ]
        return payload


class ClientDisconnected(ZKAIError):
    """Raised internally when the downstream client goes away."""

    error_type = "client_disconnected"
    http_status = 499
