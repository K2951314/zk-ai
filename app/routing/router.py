"""The Router: resolve a requested model name into an ordered attempt plan.

Pipeline::

    "zk-coding"
      -> AliasRegistry            (is it an alias?)
      -> candidate model ids      (nested aliases expanded)
      -> deployment expansion     (each model -> its provider deployments)
      -> capability requirement   (what does this request need?)
      -> capability scoring       (explainable weighted score + hard gates)
      -> selection strategy       (which order do we try them in?)
      -> RoutePlan                (ordered candidates + reason)

The router performs **no** I/O: it never calls a provider. Execution, retry and
failover are the scheduler's job, which keeps routing unit-testable.
"""

from __future__ import annotations

import asyncio
import difflib
import threading
from dataclasses import dataclass, field
from typing import Any

from app.core.config import AppConfig
from app.core.errors import AliasNotFoundError, ModelNotFoundError, ProviderNotFoundError
from app.core.logging import get_logger
from app.models.provider import ModelAliasConfig, ModelConfig, ProviderConfig
from app.models.request import ChatCompletionRequest
from app.providers.base import ProviderAdapter
from app.providers.factory import create_adapter
from app.routing.aliases import AliasRegistry
from app.routing.capability import (
    CapabilityRequirement,
    explain_scores,
    infer_requirement,
    score_candidate,
)
from app.routing.strategy import RoutingCandidate, get_strategy

logger = get_logger("routing.router")

# Strong refs to in-flight adapter close tasks (see _close_in_background).
_PENDING_CLOSES: set[asyncio.Task[None]] = set()


def _unknown_model_message(
    requested: str, models: dict[str, ModelConfig] | Any, alias_names: list[str]
) -> str:
    """404 文案带上相近的可用名——客户端把 model 写错时能自己改对。

    换机后桌面版报 ``model 'zk' is not configured`` 就是这么发现的：
    config.toml 里的 model 不是网关有的别名/模型。前缀匹配优先
    （``zk`` → zk-auto/zk-k3/zk-vision），再退 difflib 模糊匹配。
    """
    base = f"模型 '{requested}' 不存在（网关里没有这个模型或别名）"
    known = sorted(set(models) | set(alias_names))
    close = [name for name in known if requested and name.startswith(requested)]
    if not close:
        close = difflib.get_close_matches(requested, known, n=5, cutoff=0.6)
    if close:
        base += "。网关里有这些相近的：" + "、".join(close[:6])
    return base


@dataclass(slots=True)
class RoutingDecision:
    """Why this request went where it went (recorded on every response)."""

    requested_model: str
    alias: str | None
    resolved_models: list[str]
    strategy: str
    reason: str
    requirement: CapabilityRequirement
    candidates: list[RoutingCandidate] = field(default_factory=list)

    def eligible_candidates(self) -> list[RoutingCandidate]:
        """Candidates that passed every hard gate, in attempt order."""
        return [candidate for candidate in self.candidates if candidate.eligible]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_model": self.requested_model,
            "alias": self.alias,
            "resolved_models": self.resolved_models,
            "strategy": self.strategy,
            "reason": self.reason,
            "requirement": {
                "weights": {k: v for k, v in self.requirement.weights.items() if v},
                "minimums": self.requirement.minimums,
                "estimated_tokens": self.requirement.estimated_tokens,
                "notes": self.requirement.notes,
            },
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class Router:
    """Model resolution + candidate planning."""

    def __init__(self, config: AppConfig, *, alias_registry: AliasRegistry | None = None) -> None:
        self._lock = threading.RLock()
        self.config = config
        self.aliases = alias_registry or AliasRegistry(config.aliases.values())
        self._adapters: dict[str, ProviderAdapter] = {}
        self._build_adapters()

    # ------------------------------------------------------------------ #
    # Adapters
    # ------------------------------------------------------------------ #
    def _build_adapters(self) -> None:
        self._adapters = {
            provider.id: create_adapter(provider)
            for provider in self.config.providers.values()
            if provider.enabled
        }

    def adapter(self, provider_id: str) -> ProviderAdapter:
        """Return the adapter for *provider_id* (raises when unknown/disabled)."""
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise ProviderNotFoundError(
                f"provider '{provider_id}' is unknown or disabled", provider=provider_id
            )
        return adapter

    def adapters(self) -> dict[str, ProviderAdapter]:
        return dict(self._adapters)

    async def aclose(self) -> None:
        """Close every adapter's HTTP client."""
        for adapter in self._adapters.values():
            await adapter.aclose()

    def register_adapter(self, provider_id: str, adapter: ProviderAdapter) -> None:
        """Inject an adapter (tests / hot-plugging a custom provider)."""
        with self._lock:
            self._adapters[provider_id] = adapter

    def upsert_adapter(self, provider: ProviderConfig) -> None:
        """Build and install an adapter for *provider*, closing any displaced one.

        Skips the install (and removes any existing entry) when the provider is
        disabled, so a runtime ``enabled=false`` edit takes effect immediately
        instead of leaving a zombie adapter that ``adapter()`` would still hand out.
        """
        displaced: ProviderAdapter | None = None
        with self._lock:
            displaced = self._adapters.pop(provider.id, None)
            if provider.enabled:
                self._adapters[provider.id] = create_adapter(provider)
        if displaced is not None:
            self._close_in_background([displaced])

    def remove_adapter(self, provider_id: str) -> None:
        """Drop the adapter for *provider_id* and close its HTTP client."""
        with self._lock:
            removed = self._adapters.pop(provider_id, None)
        if removed is not None:
            self._close_in_background([removed])

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #
    def resolve_model_ids(self, requested: str) -> tuple[list[str], str | None]:
        """Expand *requested* into concrete model ids. Returns (ids, alias_name)."""
        if requested in self.config.models:
            return [requested], None
        if self.aliases.is_alias(requested):
            model_ids = self.aliases.expand(requested, model_ids=set(self.config.models))
            if not model_ids:
                raise AliasNotFoundError(
                    f"alias '{requested}' resolves to no configured model", model=requested
                )
            return model_ids, requested
        raise ModelNotFoundError(
            _unknown_model_message(requested, self.config.models, self.aliases.names()),
            model=requested,
        )

    def requirement_for(
        self,
        request: ChatCompletionRequest,
        *,
        alias: ModelAliasConfig | None = None,
        model: ModelConfig | None = None,
    ) -> CapabilityRequirement:
        return infer_requirement(request, alias=alias, model=model)

    # ------------------------------------------------------------------ #
    # Planning
    # ------------------------------------------------------------------ #
    def plan(self, request: ChatCompletionRequest) -> RoutingDecision:
        """Build the ordered attempt plan for *request*."""
        requested = request.model
        model_ids, alias_name = self.resolve_model_ids(requested)
        alias = self.aliases.get(alias_name) if alias_name else None

        primary_model = self.config.models.get(model_ids[0])
        requirement = self.requirement_for(request, alias=alias, model=primary_model)

        candidates: list[RoutingCandidate] = []
        for target_index, model_id in enumerate(model_ids):
            model = self.config.models.get(model_id)
            if model is None or not model.enabled:
                continue
            for deployment in model.deployments:
                provider = self.config.providers.get(deployment.provider_id)
                if provider is None or not provider.enabled:
                    logger.debug(
                        "skipping deployment %s: provider %s unavailable",
                        deployment.id,
                        deployment.provider_id,
                    )
                    continue
                card = score_candidate(model=model, deployment=deployment, requirement=requirement)
                candidates.append(
                    RoutingCandidate.from_scorecard(
                        model=model,
                        deployment=deployment,
                        provider=provider,
                        card=card,
                        target_index=target_index,
                    )
                )

        if not candidates:
            raise ModelNotFoundError(
                f"模型 '{requested}' 下没有任何可用的部署", model=requested
            )

        strategy_name = alias.strategy if alias else None
        strategy = get_strategy(strategy_name)
        ordered = strategy.order(candidates, requirement, pin_first=bool(alias and alias.pin_first))

        reason_parts = [
            f"requested={requested}",
            f"alias={alias_name or '-'}",
            f"strategy={strategy.name}",
            f"order={[c.deployment.id for c in ordered if c.eligible][:4]}",
        ]
        if requirement.notes:
            reason_parts.append("hints: " + "; ".join(requirement.notes))
        blocked = [c.deployment.id for c in ordered if not c.eligible]
        if blocked:
            reason_parts.append(f"gated_out={blocked[:3]}")

        decision = RoutingDecision(
            requested_model=requested,
            alias=alias_name,
            resolved_models=model_ids,
            strategy=strategy.name,
            reason=" | ".join(reason_parts),
            requirement=requirement,
            candidates=ordered,
        )
        logger.info(
            "routing %s -> %s (%s)",
            requested,
            ordered[0].label if ordered else "none",
            decision.reason,
        )
        return decision

    def eligible_candidates(self, decision: RoutingDecision) -> list[RoutingCandidate]:
        """Eligible candidates in attempt order."""
        eligible = [candidate for candidate in decision.candidates if candidate.eligible]
        return eligible

    # ------------------------------------------------------------------ #
    # Admin preview
    # ------------------------------------------------------------------ #
    def preview(self, request: ChatCompletionRequest) -> dict[str, Any]:
        """Explain routing without executing anything (``/admin/router/preview``)."""
        try:
            decision = self.plan(request)
        except (ModelNotFoundError, AliasNotFoundError) as exc:
            return {
                "requested_model": request.model,
                "error": {"type": exc.error_type, "message": exc.message},
                "known_models": self.config.public_model_ids(),
                "known_aliases": self.aliases.names(),
            }
        cards = [
            (candidate.deployment.id, _card_from_candidate(candidate))
            for candidate in decision.candidates
        ]
        return {
            "requested_model": request.model,
            "alias": decision.alias,
            "strategy": decision.strategy,
            "reason": decision.reason,
            "estimated_input_tokens": decision.requirement.estimated_tokens,
            "requirement": decision.requirement.describe(),
            "weights": {
                k: v for k, v in decision.requirement.normalized_weights().items() if v > 0
            },
            "minimums": decision.requirement.minimums,
            "plan": [candidate.to_dict() for candidate in decision.candidates],
            "scores": explain_scores(cards, limit=8),
        }

    # ------------------------------------------------------------------ #
    # Reconfiguration
    # ------------------------------------------------------------------ #
    def reload(self, config: AppConfig, *, alias_registry: AliasRegistry | None = None) -> None:
        """Hot swap configuration: aliases take effect immediately, adapters are rebuilt."""
        with self._lock:
            old_adapters = list(self._adapters.values())
            self.config = config
            if alias_registry is not None:
                self.aliases = alias_registry
            else:
                self.aliases.replace_all(config.aliases.values())
            self._build_adapters()
        if old_adapters:
            self._close_in_background(old_adapters)
        logger.info("router configuration reloaded (models=%d)", len(config.models))

    @staticmethod
    def _close_in_background(adapters: list[ProviderAdapter]) -> None:
        """Schedule ``aclose()`` on retired adapters without blocking the caller.

        Keep-alive connections would otherwise accumulate one set per hot-edit
        until process exit. Outside a running loop (unit tests, CLI) we skip:
        those callers own the adapter lifecycle themselves.
        """
        if not adapters:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(Router._close_retired(adapters))
        # RUF006: keep a strong ref until the close finishes, else GC may drop it.
        _PENDING_CLOSES.add(task)
        task.add_done_callback(_PENDING_CLOSES.discard)

    @staticmethod
    async def _close_retired(adapters: list[ProviderAdapter]) -> None:
        for adapter in adapters:
            try:
                await adapter.aclose()
            except Exception:
                logger.debug("failed to close retired adapter", exc_info=True)

    def describe(self) -> dict[str, Any]:
        """Routing topology dump for the admin API."""
        return {
            "models": {
                model.id: {
                    "enabled": model.enabled,
                    "capabilities": model.capabilities.as_dict(),
                    "context_window": model.context_window,
                    "deployment_ids": [d.id for d in model.deployments],
                    "providers": sorted({d.provider_id for d in model.deployments}),
                }
                for model in self.config.models.values()
            },
            "aliases": self.aliases.describe(),
            "providers": {provider.id: provider.type.value for provider in self.config.providers.values()},
            "adapters": sorted(self._adapters),
        }


def _card_from_candidate(candidate: RoutingCandidate):
    """Rebuild a minimal ScoreCard from a candidate (for preview output)."""
    from app.routing.capability import ScoreCard

    return ScoreCard(
        total=candidate.score,
        breakdown=candidate.breakdown,
        gates_failed=candidate.gates_failed,
    )
