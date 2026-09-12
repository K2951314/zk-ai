"""Error taxonomy, classification and secret-redaction guarantees."""

from __future__ import annotations

import logging

import httpx
import pytest

from app.core.errors import (
    AllAttemptsFailedError,
    AuthenticationError,
    ConnectionFailureError,
    ContextLengthExceededError,
    OverloadedError,
    PermissionDeniedError,
    RateLimitError,
    RequestTimeoutError,
    UpstreamServerError,
    ZKAIError,
)
from app.core.logging import ContextFilter, get_logger
from app.core.security import (
    mask_secret,
    redact_headers,
    redact_mapping,
    redact_text,
    resolve_env_reference,
    secret_fingerprint,
)
from app.retry.classifier import ErrorClass, ErrorClassifier, parse_retry_after

# --------------------------------------------------------------------------- #
# Status code mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "error_type", "error_class", "retryable", "switch_credential", "switch_provider"),
    [
        (400, "invalid_request_error", ErrorClass.CLIENT, False, False, False),
        (401, "authentication_error", ErrorClass.CREDENTIAL, False, True, False),
        (403, "permission_denied", ErrorClass.CREDENTIAL, False, True, False),
        (404, "model_not_found", ErrorClass.CLIENT, False, False, True),
        (408, "timeout", ErrorClass.TRANSIENT, True, False, False),
        (409, "conflict_error", ErrorClass.CLIENT, False, False, False),
        (413, "context_length_exceeded", ErrorClass.CLIENT, False, False, False),
        (429, "rate_limit_error", ErrorClass.THROTTLE, False, True, False),
            (500, "upstream_error", ErrorClass.TRANSIENT, True, True, True),
            (502, "upstream_error", ErrorClass.TRANSIENT, True, True, True),
        (503, "upstream_error", ErrorClass.AVAILABILITY, True, False, True),
        (504, "timeout", ErrorClass.TRANSIENT, True, False, True),
        (529, "overloaded", ErrorClass.AVAILABILITY, True, False, True),
    ],
)
def test_status_classification(
    status: int,
    error_type: str,
    error_class: ErrorClass,
    retryable: bool,
    switch_credential: bool,
    switch_provider: bool,
) -> None:
    """Every documented status maps onto the intended retry/failover behaviour."""
    info = ErrorClassifier().classify_status(status, body={"error": {"message": "boom"}})
    assert info.error_type == error_type
    assert info.error_class is error_class
    assert info.retryable is retryable
    assert info.switch_credential is switch_credential
    assert info.switch_provider is switch_provider


def test_client_errors_never_rotate_credentials() -> None:
    """400 and 413 must never switch a key nor trigger a failover."""
    classifier = ErrorClassifier()
    for status in (400, 413, 409, 422):
        info = classifier.classify_status(status)
        assert info.is_request_error()
        assert info.switch_credential is False
        assert info.switch_provider is False
        assert info.retryable is False


def test_429_sets_credential_cooldown_and_switches_key() -> None:
    info = ErrorClassifier().classify_status(429, headers={"retry-after": "12"})
    assert info.switch_credential is True
    assert info.cooldown_scope == "credential"
    assert info.cooldown_seconds == 12.0
    assert info.retry_after == 12.0


def test_529_cools_down_the_deployment_and_fails_over() -> None:
    info = ErrorClassifier().classify_status(529)
    assert info.error_type == "overloaded"
    assert info.switch_provider is True
    assert info.cooldown_scope == "deployment"
    assert info.cooldown_seconds > 0


def test_404_fails_over_without_touching_credentials() -> None:
    """A wrong model name is not a credential problem - but another model may work."""
    info = ErrorClassifier().classify_status(404)
    assert info.switch_credential is False
    assert info.switch_provider is True


def test_default_429_cooldown_is_used_without_retry_after() -> None:
    classifier = ErrorClassifier(rate_limit_cooldown=42.0)
    info = classifier.classify_status(429)
    assert info.cooldown_seconds == 42.0


def test_quota_exhaustion_429_is_flagged_apart_from_tpm() -> None:
    """"token plan entitlement exhausted" must not ride the exponential ladder."""
    quota = ErrorClassifier().classify_status(
        429, body={"error": {"message": "token plan entitlement exhausted"}}
    )
    assert quota.error_type == "rate_limit_error"
    assert quota.quota_exhausted is True

    tpm = ErrorClassifier().classify_status(
        429, body={"error": {"message": "inference exceeds tpm/rpm limit"}}
    )
    assert tpm.quota_exhausted is False


# --------------------------------------------------------------------------- #
# Exception classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("exc", "error_type"),
    [
        (httpx.ConnectTimeout("timed out"), "timeout"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused"), "connection_error"),
        (httpx.RemoteProtocolError("closed"), "connection_error"),
        (TimeoutError(), "timeout"),
        (ValueError("weird"), "unknown_error"),
    ],
)
def test_exception_classification(exc: BaseException, error_type: str) -> None:
    assert ErrorClassifier().classify(exc).error_type == error_type


def test_typed_errors_round_trip_through_the_classifier() -> None:
    classifier = ErrorClassifier()
    mapping = {
        AuthenticationError("bad key"): "authentication_error",
        PermissionDeniedError("nope"): "permission_denied",
        RateLimitError("slow down"): "rate_limit_error",
        RequestTimeoutError("timeout"): "timeout",
        ConnectionFailureError("down"): "connection_error",
        OverloadedError("busy"): "overloaded",
        ContextLengthExceededError("too big"): "context_length_exceeded",
        UpstreamServerError("500"): "upstream_error",
    }
    for error, expected in mapping.items():
        assert classifier.classify(error).error_type == expected


def test_unknown_error_is_recorded_but_not_retried_infinitely() -> None:
    info = ErrorClassifier().classify(RuntimeError("boom"))
    assert info.error_type == "unknown_error"
    assert info.retryable is False
    assert info.switch_provider is True  # one failover is allowed, then the plan ends
    assert info.message.startswith("RuntimeError")


def test_error_to_error_preserves_client_status() -> None:
    info = ErrorClassifier().classify_status(429, headers={"retry-after": "5"})
    error = info.to_error(provider="fake", model="m")
    assert isinstance(error, RateLimitError)
    assert error.http_status == 429
    assert error.provider == "fake"
    assert error.retry_after == 5.0


# --------------------------------------------------------------------------- #
# Retry-After parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, None), ({"retry-after": "7"}, 7.0), ({"retry-after": "abc"}, None),
     ({"retry-after": "-3"}, 0.0)],
)
def test_parse_retry_after_seconds(header: dict[str, str] | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


def test_parse_retry_after_accepts_http_dates() -> None:
    from email.utils import formatdate

    future = formatdate(usegmt=True)  # now -> ~0 seconds
    value = parse_retry_after({"retry-after": future})
    assert value is not None and 0 <= value <= 5


# --------------------------------------------------------------------------- #
# Exception hierarchy
# --------------------------------------------------------------------------- #
def test_exception_hierarchy_and_status() -> None:
    assert issubclass(AuthenticationError, ZKAIError)
    assert AuthenticationError("x").http_status == 401
    assert PermissionDeniedError("x").http_status == 403
    assert ContextLengthExceededError("x").http_status == 413
    assert OverloadedError("x").http_status == 529


def test_all_attempts_failed_carries_attempt_details() -> None:
    error = AllAttemptsFailedError("everything failed", attempts=[{"attempt_number": 1}])
    payload = error.to_dict()
    assert payload["error"]["attempts"] == [{"attempt_number": 1}]
    assert error.http_status == 503


def test_error_payload_never_contains_secret_material() -> None:
    error = ZKAIError("boom", provider="openai", model="gpt-5")
    payload = error.to_dict()
    text = str(payload)
    assert "sk-" not in text
    assert "authorization" not in text.lower()


# --------------------------------------------------------------------------- #
# Secret handling
# --------------------------------------------------------------------------- #
def test_resolve_env_reference_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZKAI_TEST_KEY", "sk-env-value")
    assert resolve_env_reference("${ZKAI_TEST_KEY}") == ("sk-env-value", "env")
    assert resolve_env_reference("${ZKAI_TEST_MISSING:-fallback}") == ("fallback", "default")
    assert resolve_env_reference("${ZKAI_TEST_MISSING}") == (None, "missing")
    assert resolve_env_reference("plain-value") == ("plain-value", "literal")


def test_mask_and_fingerprint_do_not_leak_the_secret() -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    masked = mask_secret(secret)
    assert masked.startswith("sk-p")
    assert "abcdefghij" not in masked
    assert secret_fingerprint(secret) != secret_fingerprint(secret + "x")
    assert len(secret_fingerprint(secret)) == 12


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer sk-abcdefghijklmnop",
        "api_key=AIzaSyABCDEFGHIJKLMNOP",
        "x-api-key: sk-ant-api03-abcdefghijklmnop",
        "token sk-proj-1234567890abcdef",
    ],
)
def test_redact_text_removes_credentials(raw: str) -> None:
    redacted = redact_text(raw)
    assert "***REDACTED***" in redacted
    assert "sk-abcdefghij" not in redacted
    assert "AIzaSyABCDEFGHIJKLMNOP" not in redacted


def test_redact_headers_and_mappings() -> None:
    headers = {
        "Authorization": "Bearer sk-secretsecret",
        "Cookie": "session=abc",
        "content-type": "application/json",
    }
    safe = redact_headers(headers)
    assert safe["Authorization"] == "***REDACTED***"
    assert safe["Cookie"] == "***REDACTED***"
    assert safe["content-type"] == "application/json"

    payload = {"api_key": "sk-verysecret", "nested": {"authorization": "Bearer sk-x"}}
    scrubbed = redact_mapping(payload)
    assert scrubbed["api_key"] == "***REDACTED***"
    assert scrubbed["nested"]["authorization"] == "***REDACTED***"


def test_logging_filter_scrubs_secrets_from_records() -> None:
    """A key pasted into a log message must never reach a handler."""
    filter_ = ContextFilter()
    record = logging.LogRecord(
        name="zkai.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="calling with key sk-proj-abcdefghijklmnop",
        args=(),
        exc_info=None,
    )
    assert filter_.filter(record) is True
    assert "sk-proj-abcdefghijklmnop" not in record.getMessage()
    assert "***REDACTED***" in record.getMessage()


def test_logger_helpers_are_namespaced() -> None:
    assert get_logger("pool").name == "zkai.pool"
    assert get_logger("zkai.pool").name == "zkai.pool"
