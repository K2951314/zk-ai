"""Candidate ordering strategies.

The router builds one :class:`RoutingCandidate` per deployment (with its
explainable capability score); the strategy decides the *order* in which they are
attempted, i.e. which model serves the request first and what the failover chain
looks like.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.models.provider import AliasStrategy, DeploymentConfig, ModelConfig, ProviderConfig
from app.routing.capability import CapabilityRequirement, ScoreCard


@dataclass(slots=True)
class RoutingCandidate:
    """One attemptable (model, deployment, provider) triple."""

    model: ModelConfig
    deployment: DeploymentConfig
    provider: ProviderConfig
    score: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)
    eligible: bool = True
    gates_failed: list[str] = field(default_factory=list)
    order: int = 0
    #: Position of the candidate's model inside the alias target list. Alias order
    #: is an explicit operator decision, so ``priority`` selection honours it first.
    target_index: int = 0

    @property
    def key(self) -> str:
        return f"{self.deployment.provider_id}/{self.deployment.model}"

    @property
    def label(self) -> str:
        return f"{self.model.id} -> {self.deployment.id} ({self.key})"

    def reason(self) -> str:
        if not self.eligible:
            return f"{self.deployment.id}: {', '.join(self.gates_failed)}"
        top = sorted(
            ((k, v) for k, v in self.breakdown.items() if v > 0),
            key=lambda kv: kv[1],
            reverse=True,
        )[:3]
        detail = ", ".join(f"{k}={v:.2f}" for k, v in top)
        return f"{self.deployment.id}: score {self.score:.3f} ({detail})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "model": self.model.id,
            "deployment": self.deployment.id,
            "provider": self.deployment.provider_id,
            "upstream_model": self.deployment.model,
            "priority": self.deployment.priority,
            "target_index": self.target_index,
            "score": self.score,
            "eligible": self.eligible,
            "gates_failed": self.gates_failed,
            "reason": self.reason(),
        }

    @classmethod
    def from_scorecard(
        cls,
        *,
        model: ModelConfig,
        deployment: DeploymentConfig,
        provider: ProviderConfig,
        card: ScoreCard,
        target_index: int = 0,
    ) -> RoutingCandidate:
        return cls(
            model=model,
            deployment=deployment,
            provider=provider,
            score=card.total,
            breakdown=card.breakdown,
            eligible=card.eligible,
            gates_failed=list(card.gates_failed),
            target_index=target_index,
        )


class SelectionStrategy(ABC):
    """Order candidates for a request."""

    name: str = "base"

    def __init__(self, rng: random.Random | None = None) -> None:
        # Non-cryptographic on purpose: this only shuffles candidate order.
        self.rng = rng or random.Random()  # noqa: S311

    @abstractmethod
    def order(
        self,
        candidates: list[RoutingCandidate],
        requirement: CapabilityRequirement | None = None,
    ) -> list[RoutingCandidate]:
        """Return eligible candidates first, in attempt order."""

    # ------------------------------------------------------------------ #
    @staticmethod
    def _partition(
        candidates: list[RoutingCandidate],
    ) -> tuple[list[RoutingCandidate], list[RoutingCandidate]]:
        """Split into (eligible, ineligible)."""
        eligible = [c for c in candidates if c.eligible]
        blocked = [c for c in candidates if not c.eligible]
        return eligible, blocked

    @staticmethod
    def _number(candidates: list[RoutingCandidate]) -> list[RoutingCandidate]:
        for index, candidate in enumerate(candidates):
            candidate.order = index
        return candidates


class PrioritySelection(SelectionStrategy):
    """Static order: alias target position first, then deployment priority.

    ``targets: [a, b]`` in an alias is an explicit operator decision, so it wins;
    deployment priority breaks ties between several deployments of one model.
    """

    name = AliasStrategy.PRIORITY.value

    def order(
        self, candidates: list[RoutingCandidate], requirement: CapabilityRequirement | None = None
    ) -> list[RoutingCandidate]:
        eligible, blocked = self._partition(candidates)
        eligible.sort(key=lambda c: (c.target_index, -c.deployment.priority, c.label))
        blocked.sort(key=lambda c: (c.target_index, -c.deployment.priority, c.label))
        return self._number(eligible + blocked)


class CapabilitySelection(SelectionStrategy):
    """Highest capability score first - the default for ``zk-auto`` / ``zk-coding``."""

    name = AliasStrategy.CAPABILITY.value

    def order(
        self, candidates: list[RoutingCandidate], requirement: CapabilityRequirement | None = None
    ) -> list[RoutingCandidate]:
        eligible, blocked = self._partition(candidates)
        eligible.sort(key=lambda c: (-c.score, -c.deployment.priority, c.label))
        blocked.sort(key=lambda c: c.label)
        return self._number(eligible + blocked)


class CostSelection(SelectionStrategy):
    """Cheapest capable model first."""

    name = AliasStrategy.COST.value

    def order(
        self, candidates: list[RoutingCandidate], requirement: CapabilityRequirement | None = None
    ) -> list[RoutingCandidate]:
        eligible, blocked = self._partition(candidates)
        eligible.sort(
            key=lambda c: (
                -(c.breakdown.get("cost", 0.0)),
                -c.score,
                c.label,
            )
        )
        blocked.sort(key=lambda c: c.label)
        return self._number(eligible + blocked)


class SpeedSelection(SelectionStrategy):
    """Fastest model first."""

    name = AliasStrategy.SPEED.value

    def order(
        self, candidates: list[RoutingCandidate], requirement: CapabilityRequirement | None = None
    ) -> list[RoutingCandidate]:
        eligible, blocked = self._partition(candidates)
        eligible.sort(key=lambda c: (-(c.breakdown.get("speed", 0.0)), -c.score, c.label))
        blocked.sort(key=lambda c: c.label)
        return self._number(eligible + blocked)


class RoundRobinSelection(SelectionStrategy):
    """Rotate between equally ranked candidates (state kept per strategy)."""

    name = AliasStrategy.ROUND_ROBIN.value

    def __init__(self, rng: random.Random | None = None) -> None:
        super().__init__(rng)
        self._cursor: dict[str, int] = {}

    def order(
        self, candidates: list[RoutingCandidate], requirement: CapabilityRequirement | None = None
    ) -> list[RoutingCandidate]:
        eligible, blocked = self._partition(candidates)
        eligible.sort(key=lambda c: (-c.deployment.priority, -c.score, c.label))
        if eligible:
            key = eligible[0].model.id
            offset = self._cursor.get(key, 0) % len(eligible)
            eligible = eligible[offset:] + eligible[:offset]
            self._cursor[key] = (offset + 1) % len(eligible)
        blocked.sort(key=lambda c: c.label)
        return self._number(eligible + blocked)


class WeightedSelection(SelectionStrategy):
    """Weighted random within priority bands (deterministic seeding in tests)."""

    name = AliasStrategy.WEIGHTED.value

    def order(
        self, candidates: list[RoutingCandidate], requirement: CapabilityRequirement | None = None
    ) -> list[RoutingCandidate]:
        eligible, blocked = self._partition(candidates)
        bands: dict[int, list[RoutingCandidate]] = {}
        for candidate in eligible:
            bands.setdefault(candidate.deployment.priority, []).append(candidate)
        ordered: list[RoutingCandidate] = []
        for priority in sorted(bands, reverse=True):
            bucket = list(bands[priority])
            while bucket:
                weights = [max(0.001, item.deployment.weight) for item in bucket]
                chosen = self.rng.choices(bucket, weights=weights, k=1)[0]
                ordered.append(chosen)
                bucket.remove(chosen)
        blocked.sort(key=lambda c: c.label)
        return self._number(ordered + blocked)


_STRATEGIES: dict[str, type[SelectionStrategy]] = {
    cls.name: cls
    for cls in (
        PrioritySelection,
        CapabilitySelection,
        CostSelection,
        SpeedSelection,
        RoundRobinSelection,
        WeightedSelection,
    )
}


def get_strategy(name: str | AliasStrategy | None, *, rng: random.Random | None = None) -> SelectionStrategy:
    """Resolve a strategy by name, defaulting to capability ordering."""
    key = name.value if isinstance(name, AliasStrategy) else (name or "")
    cls = _STRATEGIES.get(key.strip().lower(), CapabilitySelection)
    return cls(rng)


def available_strategies() -> list[str]:
    return sorted(_STRATEGIES)
