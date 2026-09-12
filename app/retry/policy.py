"""Retry policy: hard limits on how far the scheduler is allowed to go."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.retry.backoff import BackoffCalculator
from app.retry.classifier import ErrorInfo


@dataclass(slots=True)
class RetryPolicy:
    """Attempt budget for one client request.

    Attributes
    ----------
    max_retries_per_credential:
        Retries against the *same* credential before moving on.
    max_credentials_per_deployment:
        How many sibling keys may be tried for one deployment.
    max_deployments:
        Failover depth (how many models/providers to try).
    max_total_attempts:
        Absolute ceiling across the whole request - the anti-infinite-loop guard.
    stream_open_retries:
        Retries allowed while *opening* a stream (before the first chunk).
    """

    max_retries_per_credential: int = 2
    max_credentials_per_deployment: int = 3
    max_deployments: int = 3
    max_total_attempts: int = 8
    stream_open_retries: int = 1
    base_delay: float = 0.5
    max_delay: float = 8.0
    multiplier: float = 2.0
    jitter: float = 0.3
    jitter_mode: str = "full"
    #: Retry client errors (400/413) - always False, kept explicit for clarity.
    retry_client_errors: bool = False
    #: Honour provider ``Retry-After`` headers within the delay cap.
    respect_retry_after: bool = True
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.max_total_attempts = max(1, self.max_total_attempts)
        self.max_retries_per_credential = max(0, self.max_retries_per_credential)
        self.max_credentials_per_deployment = max(1, self.max_credentials_per_deployment)
        self.max_deployments = max(1, self.max_deployments)
        self.backoff = BackoffCalculator(
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            multiplier=self.multiplier,
            jitter=self.jitter,
            jitter_mode=self.jitter_mode,
        )

    # ``backoff`` is attached in __post_init__ (slots friendly).
    backoff: BackoffCalculator = field(init=False, repr=False, compare=False)

    def should_retry(self, info: ErrorInfo, *, retries_done: int) -> bool:
        """May we retry the same credential after *info*?"""
        if info.is_request_error() and not self.retry_client_errors:
            return False
        if not info.retryable and info.cooldown_scope == "none":
            return False
        return retries_done < self.max_retries_per_credential

    def delay_for(self, attempt: int, info: ErrorInfo | None = None) -> float:
        """Backoff delay for the given 1-based attempt."""
        retry_after = info.retry_after if (info and self.respect_retry_after) else None
        return self.backoff.delay_for_error(attempt, retry_after=retry_after)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> RetryPolicy:
        """Build from the ``retry:`` section of ``config.yaml``."""
        if not data:
            return cls()
        allowed = set(cls.__dataclass_fields__) - {"backoff", "options"}
        kwargs = {k: v for k, v in data.items() if k in allowed}
        policy = cls(**kwargs)
        policy.options = {k: v for k, v in data.items() if k not in allowed}
        return policy
