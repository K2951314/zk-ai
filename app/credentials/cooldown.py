"""Cooldown policy: how long a credential or deployment rests after a failure.

Cooldowns grow with repeated throttling (exponential, capped) so a persistently
rate-limited key backs off instead of hammering the provider.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(slots=True)
class CooldownPolicy:
    """Tunables for credential / deployment cooldowns (seconds)."""

    rate_limit_base: float = 60.0
    rate_limit_max: float = 900.0
    rate_limit_multiplier: float = 2.0
    #: Decay window: a 429 arriving longer than this after the previous one is
    #: a *new* burst, not a continuation, so the ladder restarts at the base.
    #: Set ~ 2x rate_limit_max: anything quieter has effectively recovered.
    rate_limit_decay: float = 1800.0
    #: "Quota exhausted" 429s reset on an hours/week cycle, not a per-minute one.
    #: Parking the key for a fixed, long stretch stops every request from
    #: re-sweeping the sibling keys and escalating their counters for nothing.
    quota_cooldown: float = 1800.0
    quota_cooldown_max: float = 14400.0
    auth_cooldown: float = 0.0
    server_error_cooldown: float = 15.0
    deployment_cooldown: float = 30.0
    #: Add +/- this ratio of jitter so sibling keys do not retry in lockstep.
    jitter: float = 0.15
    #: Treat a credential with no cooldown set as healthy after this many seconds.
    auto_recover_after: float = 300.0

    @classmethod
    def from_mapping(cls, data: dict | None) -> CooldownPolicy:
        """Build from the ``cooldown:`` section of ``config.yaml`` (unknown keys ignored)."""
        if not data:
            return cls()
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def _apply_jitter(self, seconds: float) -> float:
        """Spread recovery timestamps so sibling keys do not wake in lockstep."""
        if self.jitter > 0 and seconds > 1:
            seconds *= 1 + random.uniform(-self.jitter, self.jitter)  # noqa: S311 - not security
        return round(max(1.0, seconds), 2)

    def rate_limit_cooldown(self, consecutive_rate_limits: int, retry_after: float | None) -> float:
        """Cooldown for a 429. ``Retry-After`` wins when the provider sends one."""
        if retry_after is not None and retry_after > 0:
            return min(max(retry_after, 1.0), self.rate_limit_max)
        exponent = max(0, consecutive_rate_limits - 1)
        base = self.rate_limit_base * (self.rate_limit_multiplier**exponent)
        base = min(base, self.rate_limit_max)
        if self.jitter > 0:
            base *= 1 + random.uniform(-self.jitter, self.jitter)  # noqa: S311 - not security
        return round(max(1.0, base), 2)

    def cooldown_for(
        self,
        *,
        error_type: str,
        scope: str,
        retry_after: float | None = None,
        consecutive_rate_limits: int = 0,
        configured: float | None = None,
        quota_exhausted: bool = False,
    ) -> float:
        """Resolve the cooldown duration for a classified error."""
        if configured is not None and configured > 0:
            return configured
        if scope == "deployment":
            return self.deployment_cooldown
        if error_type in {"rate_limit_error", "overloaded"}:
            if quota_exhausted:
                # The plan/credit quota will not return in 60s; back it off hard.
                if retry_after is not None and retry_after > 0:
                    return self._apply_jitter(
                        min(max(retry_after, self.quota_cooldown), self.quota_cooldown_max)
                    )
                return self._apply_jitter(self.quota_cooldown)
            return self.rate_limit_cooldown(consecutive_rate_limits, retry_after)
        if error_type in {"authentication_error", "permission_denied"}:
            # Bad key: no timer will help, the credential is parked until an
            # operator (or a health check) puts it back.
            return self.auth_cooldown
        if error_type in {"upstream_error", "timeout", "connection_error"}:
            return self.server_error_cooldown
        return 0.0
