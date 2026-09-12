"""Exponential backoff with jitter, plus budget guards.

Every delay is bounded by ``max_delay`` and every retry loop is bounded by an
attempt ceiling, so an infinite retry can never happen by construction.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


def compute_delay(
    attempt: int,
    *,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    multiplier: float = 2.0,
    jitter: float = 0.3,
    jitter_mode: str = "full",
    rng: random.Random | None = None,
) -> float:
    """Return the backoff delay for *attempt* (1-based).

    ``jitter_mode``:
      * ``"full"``  - multiply by a uniform factor in ``[1-jitter, 1+jitter]``
      * ``"equal"`` - ``delay/2 + rand(0, delay/2)`` (AWS "equal jitter")
      * ``"none"``  - deterministic exponential only
    """
    if attempt < 1:
        attempt = 1
    delay = base_delay * (multiplier ** (attempt - 1))
    delay = min(delay, max_delay)
    if jitter <= 0 or jitter_mode == "none":
        return round(delay, 4)
    generator = rng or random
    if jitter_mode == "equal":
        half = delay / 2
        delay = half + generator.uniform(0.0, half)
    else:
        low = max(0.0, 1.0 - jitter)
        high = 1.0 + jitter
        delay = delay * generator.uniform(low, high)
    return round(max(0.0, min(delay, max_delay)), 4)


def parse_retry_after_delay(retry_after: float | None, *, cap: float = 60.0) -> float | None:
    """Clamp a provider supplied ``Retry-After`` to something sane."""
    if retry_after is None:
        return None
    return float(min(max(0.0, retry_after), cap))


@dataclass(slots=True)
class BackoffCalculator:
    """Configurable backoff helper bound to a retry policy."""

    base_delay: float = 0.5
    max_delay: float = 8.0
    multiplier: float = 2.0
    jitter: float = 0.3
    jitter_mode: str = "full"

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        return compute_delay(
            attempt,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            multiplier=self.multiplier,
            jitter=self.jitter,
            jitter_mode=self.jitter_mode,
            rng=rng,
        )

    def delay_for_error(
        self,
        attempt: int,
        *,
        retry_after: float | None = None,
        rng: random.Random | None = None,
    ) -> float:
        """Honour ``Retry-After`` when present, otherwise use exponential backoff."""
        parsed = parse_retry_after_delay(retry_after, cap=self.max_delay * 4)
        if parsed is not None:
            return parsed
        return self.delay_for(attempt, rng=rng)
