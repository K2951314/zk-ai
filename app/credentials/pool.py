"""The Key Pool.

Responsibilities
----------------
* Own one :class:`CredentialRuntime` per configured credential (resolving the
  secret from the environment exactly once, at load time).
* Expose a *deterministic* candidate list per provider, honouring the explicit
  credential state machine (HEALTHY / COOLDOWN / UNHEALTHY / DISABLED).
* Apply health transitions on success/failure through
  :class:`~app.credentials.health.CredentialHealthTracker`.
* Never leak secret material into logs, snapshots or the database.

Concurrency
-----------
The pool is guarded by a :class:`threading.RLock`. All mutating operations are
synchronous and short, so they are safe from both the asyncio event loop and
worker threads (scripts, startup health checks). Selection is therefore atomic -
two concurrent requests can never be handed the same "current" key by accident,
and the in-flight counter makes load visible.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable

from app.core.logging import get_logger
from app.core.security import resolve_env_reference
from app.credentials.cooldown import CooldownPolicy
from app.credentials.health import CredentialHealthTracker, Transition
from app.credentials.rotation import get_rotation
from app.models.credential import CredentialRuntime, CredentialStatus
from app.models.provider import CredentialConfig, ProviderConfig
from app.retry.classifier import ErrorInfo

logger = get_logger("credentials.pool")


class CredentialPool:
    """A state-machine driven pool of API credentials."""

    def __init__(
        self,
        *,
        policy: CooldownPolicy | None = None,
        rotation: str = "priority",
        allow_inline_secrets: bool = True,
        affinity_enabled: bool = True,
        affinity_ttl: float = 1800.0,
        affinity_max_sessions: int = 4096,
    ) -> None:
        self._lock = threading.RLock()
        self._credentials: dict[str, CredentialRuntime] = {}
        self._by_provider: dict[str, list[str]] = {}
        self.policy = policy or CooldownPolicy()
        self.tracker = CredentialHealthTracker(self.policy)
        self.rotation_name = rotation
        self._rotation = get_rotation(rotation)
        self.allow_inline_secrets = allow_inline_secrets
        #: Conversation -> (credential_id, expiry). Providers' prompt caches are
        #: account-scoped, so every failover bounce throws away a warm cache and
        #: re-bills the full prefill. Pinning a conversation to the key that
        #: last *succeeded* keeps the cache where the conversation history lives,
        #: exactly like swapping API keys under CC Switch without re-thinking.
        #: The binding is a preference, never a latch: an unusable key falls
        #: back to normal rotation, and the next success re-anchors it.
        self.affinity_enabled = affinity_enabled
        self.affinity_ttl = max(30.0, float(affinity_ttl))
        self._affinity_max_sessions = max(16, int(affinity_max_sessions))
        self._affinity: OrderedDict[str, tuple[str, float]] = OrderedDict()

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def reconcile(self, providers: dict[str, ProviderConfig]) -> int:
        """Drop pool entries whose credential/provider left the config.

        ``register_provider`` only *adds*; without this, a key deleted from
        ``providers.yaml`` survives every hot-reload and keeps serving traffic
        until the process restarts. Returns the number of credentials removed.
        """
        with self._lock:
            keep_creds = {c.id for p in providers.values() for c in p.credentials}
            keep_providers = set(providers)
            removed = 0
            for credential_id in list(self._credentials):
                credential = self._credentials[credential_id]
                if credential.provider_id not in keep_providers or credential_id not in keep_creds:
                    del self._credentials[credential_id]
                    removed += 1
            for provider_id in list(self._by_provider):
                if provider_id not in keep_providers:
                    del self._by_provider[provider_id]
                else:
                    self._by_provider[provider_id] = [
                        cid for cid in self._by_provider[provider_id] if cid in self._credentials
                    ]
            return removed

    def register_provider(self, provider: ProviderConfig) -> list[CredentialRuntime]:
        """Load every credential of *provider* into the pool."""
        created: list[CredentialRuntime] = []
        with self._lock:
            # Register the provider up-front so availability probes work even when
            # it has zero (or unresolved) credentials.
            self._by_provider.setdefault(provider.id, [])
            for config in provider.credentials:
                runtime = self._build_runtime(config)
                self._credentials[runtime.id] = runtime
                if runtime.id not in self._by_provider[provider.id]:
                    self._by_provider[provider.id].append(runtime.id)
                created.append(runtime)

            if not provider.credentials and not provider.requires_credential:
                # Keyless provider (Ollama): a synthetic credential keeps a single
                # code path for accounting, health and statistics.
                synthetic = CredentialRuntime(
                    id=f"{provider.id}-local",
                    provider_id=provider.id,
                    priority=100,
                    secret=None,
                    secret_source="none",  # noqa: S106 - keyless marker, not a password
                    tags=["keyless"],
                )
                self._credentials[synthetic.id] = synthetic
                self._by_provider.setdefault(provider.id, []).append(synthetic.id)
                created.append(synthetic)
                logger.info("provider %s is keyless; registered synthetic credential", provider.id)

            if provider.requires_credential and not provider.credentials:
                logger.warning(
                    "provider %s has no credentials configured - requests will fail over",
                    provider.id,
                )
        return created

    def _build_runtime(self, config: CredentialConfig) -> CredentialRuntime:
        """Resolve the secret and initialise the state machine for one credential."""
        secret: str | None = None
        source = "missing"

        reference = config.env_reference()
        if reference:
            secret, source = resolve_env_reference(reference)
            if source == "missing":
                logger.warning(
                    "credential %s: environment variable %s is not set -> DISABLED",
                    config.id,
                    reference,
                )
        elif config.value:
            if self.allow_inline_secrets:
                secret, source = config.value, "literal"
                logger.warning(
                    "credential %s uses an inline secret - development only, "
                    "move it to an environment variable",
                    config.id,
                )
            else:
                logger.error(
                    "credential %s has an inline secret but inline secrets are disabled",
                    config.id,
                )
        elif not config.enabled:
            source = "missing"
        else:
            source = "none"

        runtime = CredentialRuntime.from_config(config, secret=secret, source=source)
        if source == "missing" and reference:
            runtime.status = CredentialStatus.DISABLED
            runtime.enabled = False
            runtime.disabled_reason = f"环境变量 {reference} 未设置"
        return runtime

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    def get(self, credential_id: str) -> CredentialRuntime | None:
        with self._lock:
            return self._credentials.get(credential_id)

    def for_provider(self, provider_id: str) -> list[CredentialRuntime]:
        with self._lock:
            return [
                self._credentials[cid]
                for cid in self._by_provider.get(provider_id, [])
                if cid in self._credentials
            ]

    def all(self) -> list[CredentialRuntime]:
        with self._lock:
            return list(self._credentials.values())

    def candidates(
        self,
        provider_id: str,
        *,
        exclude: Iterable[str] | None = None,
        return_cooldown: bool = False,
        session_key: str | None = None,
    ) -> list[CredentialRuntime]:
        """Usable credentials for *provider_id*, best first.

        ``return_cooldown`` includes credentials whose cooldown already expired
        (they recover lazily on selection). When ``session_key`` is given and
        affinity is enabled, the pinned credential is floated to the front of an
        otherwise-normal ordering (it must still be usable).
        """
        moment = time.time()
        excluded = set(exclude or ())
        with self._lock:
            usable: list[CredentialRuntime] = []
            for credential in self.for_provider(provider_id):
                if credential.id in excluded:
                    continue
                self.tracker.refresh(credential, now=moment)
                if credential.is_usable(now=moment, allow_cooldown=return_cooldown):
                    usable.append(credential)
            ordered = self._rotation.order(usable)
            self._apply_affinity(ordered, provider_id, session_key, moment)
            return ordered

    # ------------------------------------------------------------------ #
    # Conversation affinity (sticky sessions across failover)
    # ------------------------------------------------------------------ #
    def note_affinity(self, session_key: str | None, credential_id: str) -> None:
        """Bind *session_key* to the credential that just served it successfully.

        Sliding TTL: every success renews the pin, so an active conversation
        never gets bounced off its warm provider cache by an idle timeout.
        """
        if not self.affinity_enabled or not session_key or not credential_id:
            return
        with self._lock:
            self._affinity.pop(session_key, None)  # refresh LRU position
            self._affinity[session_key] = (credential_id, time.time() + self.affinity_ttl)
            while len(self._affinity) > self._affinity_max_sessions:
                self._affinity.popitem(last=False)

    def _apply_affinity(
        self,
        ordered: list[CredentialRuntime],
        provider_id: str,
        session_key: str | None,
        moment: float,
    ) -> None:
        """Move the pinned credential of *session_key* to the front, if present.

        Caller holds the lock. Expired bindings are dropped; a binding whose key
        is not in ``ordered`` (unusable / excluded / other provider) is a no-op -
        normal rotation already picked the best available alternative.
        """
        if not self.affinity_enabled or not session_key or not ordered:
            return
        binding = self._affinity.get(session_key)
        if binding is None:
            return
        credential_id, expires = binding
        if expires <= moment:
            self._affinity.pop(session_key, None)
            return
        for index, credential in enumerate(ordered):
            if credential.id == credential_id:
                if index:
                    ordered.insert(0, ordered.pop(index))
                return

    def pick(
        self,
        provider_id: str,
        *,
        exclude: Iterable[str] | None = None,
    ) -> CredentialRuntime | None:
        """Select and *mark in use* the next credential (atomic)."""
        moment = time.time()
        excluded = set(exclude or ())
        with self._lock:
            for credential in self.candidates(provider_id, exclude=excluded):
                credential.last_used_at = moment
                credential.in_flight += 1
                return credential
        return None

    def release(self, credential_id: str) -> None:
        """Mark a credential as no longer in flight."""
        with self._lock:
            credential = self._credentials.get(credential_id)
            if credential and credential.in_flight > 0:
                credential.in_flight -= 1

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def report_success(self, credential_id: str, *, latency_ms: float = 0.0) -> Transition | None:
        with self._lock:
            credential = self._credentials.get(credential_id)
            if credential is None:
                return None
            if credential.in_flight > 0:
                credential.in_flight -= 1
            return self.tracker.on_success(credential, latency_ms=latency_ms)

    def report_failure(self, credential_id: str, info: ErrorInfo) -> Transition | None:
        with self._lock:
            credential = self._credentials.get(credential_id)
            if credential is None:
                return None
            if credential.in_flight > 0:
                credential.in_flight -= 1
            return self.tracker.on_failure(credential, info)

    # ------------------------------------------------------------------ #
    # Administration
    # ------------------------------------------------------------------ #
    def enable(self, credential_id: str) -> Transition | None:
        with self._lock:
            credential = self._credentials.get(credential_id)
            if credential is None:
                return None
            transition = self.tracker.mark_enabled(credential)
            logger.info("credential %s enabled by operator", credential_id)
            return transition

    def disable(self, credential_id: str, reason: str = "disabled by operator") -> Transition | None:
        with self._lock:
            credential = self._credentials.get(credential_id)
            if credential is None:
                return None
            transition = self.tracker.mark_disabled(credential, reason)
            logger.warning("credential %s disabled (%s)", credential_id, reason)
            return transition

    def apply_health_check(
        self,
        credential_id: str,
        *,
        healthy: bool,
        detail: str | None = None,
        error_type: str | None = None,
    ) -> Transition | None:
        with self._lock:
            credential = self._credentials.get(credential_id)
            if credential is None:
                return None
            return self.tracker.mark_from_health_check(
                credential, healthy=healthy, detail=detail, error_type=error_type
            )

    def next_available_in(self, provider_id: str) -> float | None:
        """Seconds until the first parked key of *provider_id* is selectable.

        ``None`` means "do not wait": either something is usable right now, or
        only operator-disabled / unhealthy keys remain (cooldowns recover,
        those do not). Lets the gateway answer a blackout with an honest
        ``Retry-After`` instead of a bare 503 the client hammers every 2s.
        """
        moment = time.time()
        with self._lock:
            credentials = self.for_provider(provider_id)
            for credential in credentials:
                self.tracker.refresh(credential, now=moment)
            if any(credential.is_usable(now=moment) for credential in credentials):
                return None
            waiting = [
                credential.cooldown_remaining(moment)
                for credential in credentials
                if credential.status is CredentialStatus.COOLDOWN
            ]
            return min(waiting) if waiting else None

    def invalidate_cooldowns(self, provider_id: str | None = None) -> int:
        """Clear cooldowns (used when an operator wants an immediate retry)."""
        count = 0
        with self._lock:
            targets = self.all() if provider_id is None else self.for_provider(provider_id)
            for credential in targets:
                if credential.status is CredentialStatus.COOLDOWN:
                    self.tracker.mark_enabled(credential)
                    count += 1
        return count

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def snapshot(self, provider_id: str | None = None) -> list[dict]:
        """Admin-safe view of the pool (no secrets, masked fingerprints only)."""
        moment = time.time()
        with self._lock:
            credentials = self.all() if provider_id is None else self.for_provider(provider_id)
            return [credential.snapshot(moment) for credential in credentials]

    def stats(self, provider_id: str | None = None) -> dict:
        """Aggregate pool health, used by ``/health`` and ``/admin/stats``."""
        moment = time.time()
        with self._lock:
            credentials = self.all() if provider_id is None else self.for_provider(provider_id)
            by_status: dict[str, int] = {status.value: 0 for status in CredentialStatus}
            total_requests = 0
            total_success = 0
            total_failure = 0
            rate_limits = 0
            for credential in credentials:
                by_status[credential.status.value] += 1
                total_requests += credential.success_count + credential.failure_count
                total_success += credential.success_count
                total_failure += credential.failure_count
                rate_limits += credential.rate_limit_count
            usable = sum(
                1 for credential in credentials if credential.is_usable(now=moment)
            )
            return {
                "total": len(credentials),
                "usable": usable,
                "by_status": by_status,
                "requests": total_requests,
                "success": total_success,
                "failure": total_failure,
                "rate_limits": rate_limits,
                "success_rate": round(total_success / total_requests, 4) if total_requests else None,
                "rotation": self.rotation_name,
                "affinity_enabled": self.affinity_enabled,
                "affinity_sessions": self._active_affinity_count(moment),
            }

    def _active_affinity_count(self, moment: float) -> int:
        # Caller holds the lock. Live bindings only (expiry is lazy elsewhere).
        return sum(1 for _, expires in self._affinity.values() if expires > moment)

    def affinity_bindings_for_provider(self, provider_id: str) -> list[dict]:
        """Active conversation pins on this provider (session reduced to a hash).

        Session keys can be client-provided text (``session_id`` / ``user``), so
        they are never echoed - only a short digest, enough for an operator to
        recognise "these rows share one conversation" without leaking content.
        """
        moment = time.time()
        with self._lock:
            result: list[dict] = []
            for session_key, (credential_id, expires) in self._affinity.items():
                credential = self._credentials.get(credential_id)
                if credential is None or credential.provider_id != provider_id:
                    continue
                if expires <= moment:
                    continue
                digest = hashlib.sha256(session_key.encode("utf-8", "replace")).hexdigest()
                result.append(
                    {
                        "session": digest[:10],
                        "credential_id": credential_id,
                        "remaining": round(expires - moment, 1),
                    }
                )
            return result

    def provider_availability(self) -> dict[str, bool]:
        """Which providers currently have at least one usable credential."""
        return {
            provider_id: bool(self.candidates(provider_id))
            for provider_id in list(self._by_provider.keys())
        }

    def describe_selection(self, provider_id: str, limit: int = 5) -> list[dict]:
        """Explain *why* the candidate order is what it is (admin preview)."""
        moment = time.time()
        result: list[dict] = []
        for index, credential in enumerate(self.candidates(provider_id)[:limit]):
            result.append(
                {
                    "order": index + 1,
                    "id": credential.id,
                    "status": credential.status.value,
                    "priority": credential.priority,
                    "weight": credential.weight,
                    "consecutive_failures": credential.consecutive_failures,
                    "last_used_at": credential.last_used_at,
                    "cooldown_remaining": round(credential.cooldown_remaining(moment), 2),
                    "selected": index == 0,
                }
            )
        return result
