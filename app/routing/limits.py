"""Proactive request quotas: keep credentials under the provider's own limits.

Unlike cooldowns (reactive, after a 429), these sliding-window counters stop the
scheduler from *sending* a request that would trip the upstream limiter in the
first place:

- NVIDIA NIM: 40 requests/minute per account, shared across all models.
- SenseNova: per-account points pools with a 5-hour rolling window and a weekly
  window (see 使用手册.md) — counted in tokens, because that is what actually
  drains the pool (the burner's measured rate is ~120/360 points per Mtok).

Limits are attached to provider configs as ``options.rate_limits``:

.. code-block:: yaml

    - id: nvidia
      options:
        rate_limits:
          - {scope: credential, window_seconds: 60, max_requests: 40}

    - id: sensenova
      credentials:
        - id: sensenova-01
          tags: ["account-a"]          # account tag -> shared quota bucket
      options:
        rate_limits:
          - {scope: account, window_seconds: 18000, max_tokens: 300000000}
          - {scope: account, window_seconds: 604800, max_tokens: 3500000000}

A rule caps requests (``max_requests``), tokens (``max_tokens``), or both.
``scope`` is one of ``credential`` (per key), ``account`` (keys sharing the
first ``account-*`` tag share one bucket), or ``provider`` (whole provider).
Token accounting is post-request: the window's token total is fed by the usage
returned after each success, and the bucket is treated as spent once the sum
reaches the cap (exactly how a provider bills - it cannot know a request's
size before answering it). The console can edit these rules at runtime
(``PUT /admin/providers/{id}/limits``); overrides live in the database and
re-apply over YAML on reload and restart.
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
#: Longest window we persist across restarts (anything older can never count).
_MAX_PERSIST_WINDOW = 604800


@dataclass(slots=True)
class RateLimitRule:
    """One sliding-window quota. Caps at least one of requests / tokens."""

    window_seconds: float
    max_requests: int | None = None
    max_tokens: int | None = None
    scope: str = "credential"  # credential | account | provider

    @classmethod
    def from_mapping(cls, data: dict) -> RateLimitRule | None:
        try:
            window = float(data["window_seconds"])
        except (KeyError, TypeError, ValueError):
            logger.warning("ignoring malformed rate_limits entry: %r", data)
            return None
        max_requests = _positive_or_none(data.get("max_requests"))
        max_tokens = _positive_or_none(data.get("max_tokens"))
        if max_requests is None and max_tokens is None:
            logger.warning("rate_limits entry caps neither requests nor tokens: %r", data)
            return None
        scope = str(data.get("scope", "credential"))
        if window <= 0 or scope not in {"credential", "account", "provider"}:
            logger.warning("ignoring malformed rate_limits entry: %r", data)
            return None
        return cls(
            window_seconds=window,
            max_requests=max_requests,
            max_tokens=max_tokens,
            scope=scope,
        )

    def as_mapping(self) -> dict:
        out: dict = {
            "scope": self.scope,
            "window_seconds": int(self.window_seconds),
        }
        if self.max_requests is not None:
            out["max_requests"] = self.max_requests
        if self.max_tokens is not None:
            out["max_tokens"] = self.max_tokens
        return out


def _positive_or_none(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _valid_timestamps(raw: list, moment: float) -> list[float]:
    return [
        float(t)
        for t in raw
        if isinstance(t, (int, float)) and moment - float(t) < _MAX_PERSIST_WINDOW
    ]


def _valid_token_hits(raw: list, moment: float) -> list[tuple[float, int]]:
    return [
        (float(ts), int(n))
        for ts, n in raw
        if isinstance(ts, (int, float))
        and isinstance(n, (int, float))
        and moment - float(ts) < _MAX_PERSIST_WINDOW
    ]


@dataclass
class _Bucket:
    """Sliding-window counters for one (rule, bucket-key)."""

    #: Timestamps of admitted requests (only capped windows keep these).
    hits: list[float] = field(default_factory=list)
    #: (timestamp, token_count) pairs from completed requests.
    token_hits: list[tuple[float, int]] = field(default_factory=list)


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
    @staticmethod
    def _prune(bucket: _Bucket, window: float, moment: float) -> None:
        cutoff = moment - window
        hits = bucket.hits
        first_live = 0
        # timestamps are appended in order, so drop the expired prefix
        while first_live < len(hits) and hits[first_live] <= cutoff:
            first_live += 1
        if first_live:
            del hits[:first_live]
        tokens = bucket.token_hits
        first_live = 0
        while first_live < len(tokens) and tokens[first_live][0] <= cutoff:
            first_live += 1
        if first_live:
            del tokens[:first_live]

    def _headroom(self, rule: RateLimitRule, bucket: _Bucket | None) -> float:
        """Requests still available under one rule (inf = uncapped dimension)."""
        used_requests = 0 if bucket is None else len(bucket.hits)
        used_tokens = 0 if bucket is None else sum(n for _, n in bucket.token_hits)
        rooms = []
        if rule.max_requests is not None:
            rooms.append(rule.max_requests - used_requests)
        if rule.max_tokens is not None:
            rooms.append(rule.max_tokens - used_tokens)
        return float(min(rooms)) if rooms else float("inf")

    def remaining(
        self,
        provider_id: str,
        credential_id: str,
        tags: list[str] | None = None,
        *,
        now: float | None = None,
    ) -> float:
        """Headroom left under the tightest rule (inf = unlimited)."""
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
                if bucket is not None:
                    self._prune(bucket, rule.window_seconds, moment)
                headroom = min(headroom, self._headroom(rule, bucket))
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
                bucket = self._buckets.get(key)
                if bucket is not None:
                    self._prune(bucket, rule.window_seconds, moment)
                if self._headroom(rule, bucket) <= 0:
                    return False
            for rule, key in zip(rules, keys, strict=True):
                if rule.max_requests is None:
                    continue  # token-only windows don't need per-request marks
                bucket = self._buckets.setdefault(key, _Bucket())
                bucket.hits.append(moment)
            self._dirty = True
            return True

    def note_tokens(
        self,
        provider_id: str,
        credential_id: str,
        tokens: int,
        tags: list[str] | None = None,
        *,
        now: float | None = None,
    ) -> None:
        """Feed one completed request's token count into the capped windows."""
        if tokens <= 0:
            return
        with self._lock:
            rules = self._rules.get(provider_id)
            if not rules:
                return
            moment = now if now is not None else time.time()
            account = self._account_of(tags)
            touched = False
            for rule in rules:
                if rule.max_tokens is None:
                    continue
                key = self._bucket_key(rule, provider_id, credential_id, account)
                self._buckets.setdefault(key, _Bucket()).token_hits.append((moment, tokens))
                touched = True
            self._dirty = self._dirty or touched

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
                if bucket is not None:
                    self._prune(bucket, rule.window_seconds, moment)
                out.append(
                    {
                        "scope": rule.scope,
                        "window_seconds": int(rule.window_seconds),
                        "max_requests": rule.max_requests,
                        "max_tokens": rule.max_tokens,
                        "used_requests": 0 if bucket is None else len(bucket.hits),
                        "used_tokens": 0 if bucket is None else sum(n for _, n in bucket.token_hits),
                        "bucket": key.rsplit("|", 1)[-1],
                    }
                )
            return out

    def describe(self) -> dict[str, list[dict]]:
        """Configured rules per provider (for admin/debug)."""
        with self._lock:
            return {pid: [rule.as_mapping() for rule in rules] for pid, rules in self._rules.items()}

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
            for key, entry in (raw.get("buckets") or {}).items():
                bucket = _Bucket()
                if isinstance(entry, dict):
                    bucket.hits = _valid_timestamps(entry.get("hits") or [], moment)
                    bucket.token_hits = _valid_token_hits(entry.get("tokens") or [], moment)
                elif isinstance(entry, list):
                    # v1 state file: plain request timestamps
                    bucket.hits = _valid_timestamps(entry, moment)
                if bucket.hits or bucket.token_hits:
                    self._buckets[key] = bucket
                    loaded += len(bucket.hits) + len(bucket.token_hits)
        if loaded:
            logger.info("rate limiter restored %d counter(s) from %s", loaded, self._state_file)

    def flush(self, *, force: bool = False) -> None:
        if self._state_file is None:
            return
        with self._lock:
            # Never write when nothing changed: an un-capped test container must
            # not clobber the real state file with an empty snapshot.
            if not self._dirty:
                return
            if not force and time.time() - self._last_flush < _FLUSH_INTERVAL:
                return
            data = {
                "buckets": {
                    key: {
                        "hits": bucket.hits[-5000:],
                        "tokens": bucket.token_hits[-5000:],
                    }
                    for key, bucket in self._buckets.items()
                }
            }
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
