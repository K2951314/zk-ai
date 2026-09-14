"""Credential runtime state (the explicit state machine used by the Key Pool).

States
------
``HEALTHY``   selectable, no active cooldown.
``COOLDOWN``  temporarily skipped (429 / 529 / overload). Auto-recovers.
``UNHEALTHY`` failed repeatedly or rejected with 401/403. Skipped until a health
              check or a manual enable puts it back.
``DISABLED``  administratively disabled (config ``enabled: false`` or admin API).

All transitions happen inside :mod:`app.credentials.pool` while holding the pool
lock, so the state machine is race free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.security import mask_secret, secret_fingerprint
from app.models.provider import CredentialConfig


class CredentialStatus(str, Enum):
    HEALTHY = "healthy"
    COOLDOWN = "cooldown"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


@dataclass
class CredentialRuntime:
    """Mutable credential state. Never log :attr:`secret`."""

    id: str
    provider_id: str
    enabled: bool = True
    priority: int = 100
    weight: float = 1.0
    status: CredentialStatus = CredentialStatus.HEALTHY
    max_consecutive_failures: int = 3
    tags: list[str] = field(default_factory=list)

    #: Resolved secret. Kept in memory only.
    secret: str | None = None
    #: Where the secret came from: env / default / literal / missing.
    secret_source: str = "missing"  # noqa: S105 - provenance label, not a password
    secret_ref: str | None = None

    last_used_at: float | None = None
    last_success_at: float | None = None
    last_error_at: float | None = None
    cooldown_until: float | None = None

    success_count: int = 0
    failure_count: int = 0
    rate_limit_count: int = 0
    consecutive_failures: int = 0
    consecutive_rate_limits: int = 0

    last_error_type: str | None = None
    last_error_detail: str | None = None
    disabled_reason: str | None = None
    #: Timestamp of the most recent *served* 429. Drives the decay window:
    #: a 429 that arrives long after the previous one starts a fresh ladder.
    last_rate_limit_at: float | None = None

    in_flight: int = 0
    latency_ema_ms: float = 0.0

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    def is_configured(self) -> bool:
        """True when a secret was resolved (Ollama may legitimately have none)."""
        return bool(self.secret)

    def is_usable(self, *, allow_cooldown: bool = False, now: float | None = None) -> bool:
        """Can this credential serve a request right now?"""
        moment = now if now is not None else time.time()
        if self.status is CredentialStatus.DISABLED:
            return False
        if self.status is CredentialStatus.UNHEALTHY:
            return False
        if self.status is CredentialStatus.COOLDOWN:
            if self.cooldown_until is None or self.cooldown_until <= moment:
                return True  # cooldown expired, lazily recover
            return allow_cooldown
        return True

    def cooldown_remaining(self, now: float | None = None) -> float:
        moment = now if now is not None else time.time()
        if self.cooldown_until is None:
            return 0.0
        return max(0.0, self.cooldown_until - moment)

    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        if total == 0:
            return 1.0
        return self.success_count / total

    def fingerprint(self) -> str:
        """Log-safe identifier of the underlying key material."""
        return secret_fingerprint(self.secret)

    def masked(self) -> str:
        return mask_secret(self.secret)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Serialisable view for the admin API - contains **no** secret material."""
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "status": self.status.value,
            "enabled": self.enabled,
            "priority": self.priority,
            "weight": self.weight,
            "configured": self.is_configured(),
            "secret_source": self.secret_source,
            "secret_ref": self.secret_ref,
            "secret_fingerprint": self.fingerprint(),
            "secret_masked": self.masked(),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "rate_limit_count": self.rate_limit_count,
            "consecutive_failures": self.consecutive_failures,
            "success_rate": round(self.success_rate(), 4),
            "latency_ema_ms": round(self.latency_ema_ms, 2),
            "last_used_at": self.last_used_at,
            "last_success_at": self.last_success_at,
            "last_error_at": self.last_error_at,
            "cooldown_until": self.cooldown_until,
            "cooldown_remaining": round(self.cooldown_remaining(now), 2),
            "last_error_type": self.last_error_type,
            "last_error_detail": self.last_error_detail,
            "disabled_reason": self.disabled_reason,
            "tags": list(self.tags),
        }

    @classmethod
    def from_config(cls, config: CredentialConfig, *, secret: str | None, source: str) -> CredentialRuntime:
        """Build runtime state from a YAML credential definition."""
        return cls(
            id=config.id,
            provider_id=config.provider_id or "",
            enabled=config.enabled,
            priority=config.priority,
            weight=config.weight,
            status=CredentialStatus.HEALTHY if config.enabled else CredentialStatus.DISABLED,
            max_consecutive_failures=config.max_consecutive_failures,
            tags=list(config.tags),
            secret=secret,
            secret_source=source,
            secret_ref=config.env_reference(),
            disabled_reason=None if config.enabled else "disabled in config",
        )


def status_from_str(value: str) -> CredentialStatus:
    """Parse a status string, defaulting to HEALTHY for unknown input."""
    try:
        return CredentialStatus(value)
    except ValueError:
        return CredentialStatus.HEALTHY
