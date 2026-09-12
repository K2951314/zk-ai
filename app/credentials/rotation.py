"""Credential selection strategies (rotation).

Strategies only *order* the candidates returned by :class:`CredentialPool`; the
pool owns the state machine, rotation owns fairness.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.models.credential import CredentialRuntime

STRATEGIES: dict[str, type[CredentialRotation]] = {}


class CredentialRotation(ABC):
    """Order a list of usable credentials."""

    name: str = "base"

    def __init__(self, rng: random.Random | None = None) -> None:
        # Non-cryptographic on purpose: this only shuffles key rotation order.
        self.rng = rng or random.Random()  # noqa: S311

    @abstractmethod
    def order(self, credentials: Sequence[CredentialRuntime]) -> list[CredentialRuntime]:
        """Return credentials in the order they should be attempted."""

    # ------------------------------------------------------------------ #
    @staticmethod
    def _priority_key(credential: CredentialRuntime, now: float) -> tuple:
        """Default ordering: priority, then least-recently-used, then fewest failures."""
        return (
            -credential.priority,
            credential.consecutive_failures,
            credential.last_used_at or 0.0,
            credential.id,
        )

    def __init_subclass__(cls, **kwargs) -> None:  # pragma: no cover - registry sugar
        super().__init_subclass__(**kwargs)
        if getattr(cls, "name", "base") != "base":
            STRATEGIES[cls.name] = cls


class PriorityRotation(CredentialRotation):
    """Highest priority first, LRU as the tie-breaker (deterministic)."""

    name = "priority"

    def order(self, credentials: Sequence[CredentialRuntime]) -> list[CredentialRuntime]:
        import time

        now = time.time()
        return sorted(credentials, key=lambda c: self._priority_key(c, now))


class RoundRobinRotation(CredentialRotation):
    """Cycle through credentials with equal priority."""

    name = "round_robin"

    def __init__(self, rng: random.Random | None = None) -> None:
        super().__init__(rng)
        self._cursor: dict[str, int] = {}

    def order(self, credentials: Sequence[CredentialRuntime]) -> list[CredentialRuntime]:
        if not credentials:
            return []
        grouped: dict[int, list[CredentialRuntime]] = {}
        for credential in credentials:
            grouped.setdefault(credential.priority, []).append(credential)
        ordered: list[CredentialRuntime] = []
        for priority in sorted(grouped, reverse=True):
            bucket = sorted(grouped[priority], key=lambda c: c.id)
            start = self._cursor.get(str(priority), 0) % len(bucket)
            ordered.extend(bucket[start:] + bucket[:start])
            self._cursor[str(priority)] = (start + 1) % len(bucket)
        return ordered


class WeightedRotation(CredentialRotation):
    """Weighted random selection within a priority band."""

    name = "weighted"

    def order(self, credentials: Sequence[CredentialRuntime]) -> list[CredentialRuntime]:
        grouped: dict[int, list[CredentialRuntime]] = {}
        for credential in credentials:
            grouped.setdefault(credential.priority, []).append(credential)
        ordered: list[CredentialRuntime] = []
        for priority in sorted(grouped, reverse=True):
            bucket = list(grouped[priority])
            while bucket:
                weights = [max(0.001, c.weight) for c in bucket]
                chosen = self.rng.choices(bucket, weights=weights, k=1)[0]
                ordered.append(chosen)
                bucket.remove(chosen)
        return ordered


class LeastFailuresRotation(CredentialRotation):
    """Prefer the healthiest credential (fewest failures, best success rate)."""

    name = "least_failures"

    def order(self, credentials: Sequence[CredentialRuntime]) -> list[CredentialRuntime]:
        return sorted(
            credentials,
            key=lambda c: (
                c.consecutive_failures,
                -c.success_rate(),
                -c.priority,
                c.last_used_at or 0.0,
                c.id,
            ),
        )


class FastestRotation(CredentialRotation):
    """Prefer the credential with the best observed latency."""

    name = "fastest"

    def order(self, credentials: Sequence[CredentialRuntime]) -> list[CredentialRuntime]:
        return sorted(
            credentials,
            key=lambda c: (
                c.latency_ema_ms if c.latency_ema_ms > 0 else float("inf"),
                -c.priority,
                c.id,
            ),
        )


DEFAULT_ROTATION = "priority"


def get_rotation(name: str | None, *, rng: random.Random | None = None) -> CredentialRotation:
    """Resolve a rotation strategy by name (falls back to priority)."""
    key = (name or DEFAULT_ROTATION).strip().lower()
    cls = STRATEGIES.get(key, PriorityRotation)
    return cls(rng)


def available_rotations() -> list[str]:
    return sorted(STRATEGIES)
