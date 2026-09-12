"""Secret handling helpers.

Rules enforced here (see README "Security"):
  * an API key may only live in an environment variable, or be given inline in a
    local-only config file for development;
  * nothing that logs may ever contain a full key, an ``Authorization`` header,
    a cookie or any other credential-bearing header.
"""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping
from typing import Any

# ``${VAR}`` or ``${VAR:-fallback}``
_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}$")

# Known key shapes used for redaction. Ordered longest-prefix first.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{10,}"),
    re.compile(r"gsk_[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\bx-api-key\b\s*[:=]\s*[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\bapi[_-]?key\b\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{8,}"),
)

_REDACTED = "***REDACTED***"

#: Headers that must never be logged or stored.
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "cookie",
        "set-cookie",
        "x-admin-token",
        "openai-api-key",
    }
)


def is_env_reference(value: str | None) -> bool:
    """Return True when *value* looks like ``${VAR}`` / ``${VAR:-default}``."""
    return bool(value) and bool(_ENV_PATTERN.match(value or ""))


def resolve_env_reference(value: str) -> tuple[str | None, str]:
    """Resolve ``${VAR}`` style references.

    Returns a ``(resolved, source)`` tuple where *source* is one of
    ``"env"``, ``"default"``, ``"literal"``, ``"missing"``.
    """
    match = _ENV_PATTERN.match(value)
    if not match:
        stripped = value.strip()
        return (stripped or None, "literal")
    var_name, default = match.group(1), match.group(2)
    raw = os.environ.get(var_name)
    if raw:
        return raw.strip(), "env"
    if default is not None and default != "":
        return default.strip(), "default"
    return None, "missing"


def mask_secret(secret: str | None, *, keep: int = 4) -> str:
    """Return a log-safe fingerprint such as ``sk-1***cdef``."""
    if not secret:
        return "<empty>"
    if len(secret) <= keep * 2:
        return "***"
    return f"{secret[:keep]}***{secret[-keep:]}"


def secret_fingerprint(secret: str | None) -> str:
    """Stable short hash of a secret, safe to log and good for de-duplication."""
    if not secret:
        return "none"
    import hashlib

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def redact_text(text: str) -> str:
    """Remove anything that looks like a credential from *text*."""
    if not text:
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def redact_headers(headers: Mapping[str, Any]) -> dict[str, Any]:
    """Drop sensitive headers, keep everything else for diagnostics."""
    safe: dict[str, Any] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            safe[key] = _REDACTED
        else:
            safe[key] = value
    return safe


def redact_mapping(payload: Any, *, depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure (depth limited)."""
    if depth > 6:
        return "<max-depth>"
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        also_sensitive = {"api_key", "apikey", "token", "secret"}
        for key, value in payload.items():
            lowered = key.lower() if isinstance(key, str) else None
            if lowered is not None and (lowered in SENSITIVE_HEADERS or lowered in also_sensitive):
                out[key] = _REDACTED
            else:
                out[key] = redact_mapping(value, depth=depth + 1)
        return out
    if isinstance(payload, list):
        return [redact_mapping(item, depth=depth + 1) for item in payload[:50]]
    if isinstance(payload, str):
        return redact_text(payload)
    return payload


def constant_time_equals(left: str | None, right: str | None) -> bool:
    """Timing-safe comparison used by the admin token check."""
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
