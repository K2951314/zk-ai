"""Proactive request quotas: keep credentials under the provider's own limits.

Unlike cooldowns (reactive, after a 429), these sliding-window counters stop the
scheduler from *sending* a request that would trip the upstream limiter in the
first place:

- NVIDIA NIM: 40 requests/minute per account, shared across all models.
- SenseNova: per-account points pools with a 5-hour rolling window and a weekly
  window (see 使用手册.md) — approximated here in request counts, because the
  points API exposes no per-request cost ahead of time.

Limits are attached to provider configs as ``options.rate_limits``:

.. code-block:: yaml

    - id: nvidia
      options:
        rate_limits:
          - {scope: provider, window_seconds: 60, max_requests: 40}

    - id: sensenova
      credentials:
        - id: sensenova-01
          tags: ["account-a"]          # account tag -> shared quota bucket
      options:
        rate_limits:
          - {scope: account, window_seconds: 18000, max_requests: 300}
          - {scope: account, window_seconds: 604800, max_requests: 6000}

``scope`` is one of ``credential`` (per key), ``account`` (keys sharing the
first ``account-*`` tag share one bucket), or ``provider`` (whole provider).
State persists to ``data/rate_limits.json`` (best effort) so a restart does not
reset a half-spent window.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.models.provider import ProviderConfig

logger = get_logger("routing.limits")

_FLUSH_INTERVAL = 30.0


@dataclass(slots=True)
class RateLimitRule:
    """One sliding-window quota."""

    window_seconds: float
    max_requests: int
    scope: str = "credential"  # credential | account | provider

    @classmethod
    def from_mapping(cls, data: dict) -> RateLimitRule | None:
        try:
            window = float(data["window_seconds"])
            limit = int(data["max_requests"])
        except (KeyError, TypeError, ValueError):
            logger.warning("ignoring malformed rate_limits entry: %r", data)
            return None
        scope = str(data.get("scope", "credential"))
        if window <= 0 or limit <= 0 or scope not in {"credential", "account", "provider"}:
            logger.warning("ignoring malformed rate_limits entry: %r", data)
            return None
        return cls(window_seconds=window, max_requests=limit, scope=scope)


@dataclass
class _Bucket:
    """Timestamps of admitted requests for one (rule, bucket-key)."""

    hits: list[float] = field(default_factory=list)


class RateLimiter:
    """Process-wide sliding-window counters, keyed by (rule, bucket)."""

    def __init__(self, state_file: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._rules: dict[str, list[RateLimitRule]] = {}  # provider_id -> rules
        self._buckets: dict[str, _Bucket] = {}  # f"{provider}|{scope}|{window}|{key}"
        self._state_file = state_file
        self._dirty = False
        self._last_flush = 0.0

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #
    def register_provider(self, provider: ProviderConfig) -> None:
        rules: list[RateLimitRule] = []
        for entry in (provider.options or {}).get("rate_limits") or []:
            rule = RateLimitRule.from_mapping(entry)
            if rule is not None:
                rules.append(rule)
        with self._lock:
            if rules:
                self._rules[provider.id] = rules
            else:
                self._rules.pop(provider.id, None)

    def configure(self, providers: dict[str, ProviderConfig]) -> None:
        """Rebuild the rule table (config reload). Keeps existing counters."""
        with self._lock:
            configured = set(self._rules)
        for provider in providers.values():
            self.register_provider(provider)
        with self._lock:
            for stale in configured - set(providers):
                self._rules.pop(stale, None)

    # ------------------------------------------------------------------ #
    # Bucket keys
    # ------------------------------------------------------------------ #
    @staticmethod
    def _account_of(tags: list[str] | None) -> str | None:
        for tag in tags or ():
            if isinstance(tag, str) and tag.startswith("account-"):
                return tag
        return None

    def _bucket_key(
        self, rule: RateLimitRule, provider_id: str, credential_id: str, account: str | None
    ) -> str:
        if rule.scope == "provider":
            key = "*"
        elif rule.scope == "account":
            key = account or credential_id  # untagged keys fall back to per-key
        else:
            key = credential_id
        return f"{provider_id}|{rule.scope}|{int(rule.window_seconds)}|{key}"

    # ------------------------------------------------------------------ #
    # Accounting
    # ------------------------------------------------------------------ #
    def _prune(self, bucket: _Bucket, window: float, moment: float) -> None:
        cutoff = moment - window
        hits = bucket.hits
        # timestamps are appended in order, so drop the expired prefix
        first_live = 0
        while first_live < len(hits) and hits[first_live] <= cutoff:
            first_live += 1
        if first_live:
            del hits[:first_live]

    def remaining(
        self,
        provider_id: str,
        credential_id: str,
        tags: list[str] | None = None,
        *,
        now: float | None = None,
    ) -> float:
        """Requests still available under the tightest rule (inf = unlimited)."""
        with self._lock:
            rules = self._rules.get(provider_id)
            if not rules:
                return float("inf")
            moment = now if now is not None else time.time()
            account = self._account_of(tags)
            headroom = float("inf")
            for rule in rules:
                key = self._bucket_key(rule, provider_id, credential_id, account)
                bucket = self._buckets.get(key)
                used = 0
                if bucket is not None:
                    self._prune(bucket, rule.window_seconds, moment)
                    used = len(bucket.hits)
                headroom = min(headroom, rule.max_requests - used)
            return max(0.0, headroom)

    def admit(
        self,
        provider_id: str,
        credential_id: str,
        tags: list[str] | None = None,
        *,
        now: float | None = None,
    ) -> bool:
        """Record one admitted request; returns False when a quota is exhausted."""
        with self._lock:
            rules = self._rules.get(provider_id)
            if not rules:
                return True
            moment = now if now is not None else time.time()
            account = self._account_of(tags)
            keys = [self._bucket_key(rule, provider_id, credential_id, account) for rule in rules]
            for rule, key in zip(rules, keys, strict=True):
                bucket = self._buckets.setdefault(key, _Bucket())
                self._prune(bucket, rule.window_seconds, moment)
                if len(bucket.hits) >= rule.max_requests:
                    return False
            for key in keys:
                self._buckets[key].hits.append(moment)
            self._dirty = True
            return True

    def refund(
        self,
        provider_id: str,
        credential_id: str,
        tags: list[str] | None = None,
    ) -> None:
        """Take back one admission (request failed before reaching the provider)."""
        with self._lock:
            rules = self._rules.get(provider_id)
            if not rules:
                return
            account = self._account_of(tags)
            for rule in rules:
                key = self._bucket_key(rule, provider_id, credential_id, account)
                bucket = self._buckets.get(key)
                if bucket and bucket.hits:
                    bucket.hits.pop()
            self._dirty = True

    def usage(
        self, provider_id: str, credential_id: str, tags: list[str] | None = None
    ) -> list[dict]:
        """Per-rule usage for the admin console."""
        with self._lock:
            rules = self._rules.get(provider_id) or []
            moment = time.time()
            account = self._account_of(tags)
            out: list[dict] = []
            for rule in rules:
                key = self._bucket_key(rule, provider_id, credential_id, account)
                bucket = self._buckets.get(key)
                used = 0
                if bucket is not None:
                    self._prune(bucket, rule.window_seconds, moment)
                    used = len(bucket.hits)
                out.append(
                    {
                        "scope": rule.scope,
                        "window_seconds": int(rule.window_seconds),
                        "max_requests": rule.max_requests,
                        "used": used,
                        "bucket": key.rsplit("|", 1)[-1],
                    }
                )
            return out

    def describe(self) -> dict[str, list[dict]]:
        """Configured rules per provider (for admin/debug)."""
        with self._lock:
            return {
                pid: [
                    {
                        "scope": r.scope,
                        "window_seconds": int(r.window_seconds),
                        "max_requests": r.max_requests,
                    }
                    for r in rules
                ]
                for pid, rules in self._rules.items()
            }

    # ------------------------------------------------------------------ #
    # Persistence (best effort: losing a minute of counters just means the
    # limiter re-admits a little early after a crash — never blocks traffic)
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if self._state_file is None or not self._state_file.exists():
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("rate limit state unreadable (%s); starting fresh", exc)
            return
        moment = time.time()
        loaded = 0
        with self._lock:
            for key, hits in (raw.get("buckets") or {}).items():
                if not isinstance(hits, list):
                    continue
                # Drop entries older than the largest window; nothing can still count.
                live = [float(t) for t in hits if isinstance(t, (int, float)) and moment - t < 604800]
                if live:
                    self._buckets[key] = _Bucket(hits=live)
                    loaded += len(live)
        if loaded:
            logger.info("rate limiter restored %d hit(s) from %s", loaded, self._state_file)

    def flush(self, *, force: bool = False) -> None:
        if self._state_file is None:
            return
        with self._lock:
            if not force and (not self._dirty or time.time() - self._last_flush < _FLUSH_INTERVAL):
                return
            data = {"buckets": {key: bucket.hits[-5000:] for key, bucket in self._buckets.items()}}
            self._dirty = False
            self._last_flush = time.time()
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._state_file)
        except OSError as exc:
            logger.warning("rate limit state save failed: %s", exc)


__all__ = ["RateLimitRule", "RateLimiter"]
