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
from typing import Any, ClassVar

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
    #: 2026-11-26：调度器当前拒绝调用的部署（短冷却 / 自动隔离）。
    #: 只降权、不过滤——保留完整故障转移链，避免"全冷却时零候选"。
    #: 值来自 Router.plan(unavailable=...)，Router 依旧不做 I/O。
    penalized: bool = False
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
            "penalized": self.penalized,
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
        pin_first: bool = False,
        advance: bool = True,
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
        # 降权在**唯一出口**统一做：六个策略都以 `_number` 收尾，在这里分区
        # 才能保证「路由计划 = 调度器将执行的顺序」这条不变量对任何策略成立。
        # 2026-11-26 审查：此前只有 priority/capability 显式调用
        # `_demote_penalized`，round_robin/weighted/cost/speed 的冷却腿仍可能
        # 排队首——线上 zk-auto 恰好是 round_robin，降权在主链路上形同虚设。
        ordered = SelectionStrategy._demote_penalized(candidates)
        for index, candidate in enumerate(ordered):
            candidate.order = index
        return ordered

    @staticmethod
    def _demote_penalized(
        candidates: list[RoutingCandidate],
    ) -> list[RoutingCandidate]:
        """冷却/隔离的候选排到队尾，**不删除**。

        2026-11-26：以前 `unavailable` 只用于 front_model 提升，`strategy.order()`
        根本看不到它——额度耗尽 9.7 小时的腿仍排队首，被调度器跳过后白费一次
        首跳判断。现在所有策略共用这一个降权步骤：健康腿在前、被停的腿在后，
        但整条链仍在（全冷却时不会零候选）。稳定分区，不改变组内相对顺序。
        由 `_number` 统一调用，各策略不要再自己调一次（幂等，但两处写必然漂移）。
        """
        healthy = [c for c in candidates if not c.penalized]
        parked = [c for c in candidates if c.penalized]
        return healthy + parked


class PrioritySelection(SelectionStrategy):
    """Static order: alias target position first, then deployment priority.

    ``targets: [a, b]`` in an alias is an explicit operator decision, so it wins;
    deployment priority breaks ties between several deployments of one model.
    """

    name = AliasStrategy.PRIORITY.value

    def order(
        self,
        candidates: list[RoutingCandidate],
        requirement: CapabilityRequirement | None = None,
        pin_first: bool = False,
        advance: bool = True,
    ) -> list[RoutingCandidate]:
        eligible, blocked = self._partition(candidates)
        eligible.sort(key=lambda c: (c.target_index, -c.deployment.priority, c.label))
        blocked.sort(key=lambda c: (c.target_index, -c.deployment.priority, c.label))
        return self._number(eligible + blocked)


class CapabilitySelection(SelectionStrategy):
    """Highest capability score first - the default for ``zk-auto`` / ``zk-coding``."""

    name = AliasStrategy.CAPABILITY.value

    def order(
        self,
        candidates: list[RoutingCandidate],
        requirement: CapabilityRequirement | None = None,
        pin_first: bool = False,
        advance: bool = True,
    ) -> list[RoutingCandidate]:
        eligible, blocked = self._partition(candidates)
        if pin_first:
            # An explicitly pinned head (console hot-swap) leads the attempt order.
            # Everything after it stays capability-ranked, so the failover chain
            # remains as smart as it is without the pin.
            head = [c for c in eligible if c.target_index == 0]
            rest = [c for c in eligible if c.target_index > 0]
            rest.sort(key=lambda c: (-c.score, -c.deployment.priority, c.label))
            head.sort(key=lambda c: (-c.score, -c.deployment.priority, c.label))
            eligible = head + rest
        else:
            eligible.sort(key=lambda c: (-c.score, -c.deployment.priority, c.label))
        blocked.sort(key=lambda c: c.label)
        return self._number(eligible + blocked)


class CostSelection(SelectionStrategy):
    """Cheapest capable model first."""

    name = AliasStrategy.COST.value

    def order(
        self,
        candidates: list[RoutingCandidate],
        requirement: CapabilityRequirement | None = None,
        pin_first: bool = False,
        advance: bool = True,
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
        self,
        candidates: list[RoutingCandidate],
        requirement: CapabilityRequirement | None = None,
        pin_first: bool = False,
        advance: bool = True,
    ) -> list[RoutingCandidate]:
        eligible, blocked = self._partition(candidates)
        eligible.sort(key=lambda c: (-(c.breakdown.get("speed", 0.0)), -c.score, c.label))
        blocked.sort(key=lambda c: c.label)
        return self._number(eligible + blocked)


class RoundRobinSelection(SelectionStrategy):
    """Rotate between equally ranked candidates.

    Cursor is **class-level** because :func:`get_strategy` returns a fresh
    instance per call — an instance-level cursor would reset every request
    and round-robin would degenerate to "always the same candidate".
    """

    name = AliasStrategy.ROUND_ROBIN.value

    _cursor: ClassVar[dict[str, int]] = {}

    def __init__(self, rng: random.Random | None = None) -> None:
        super().__init__(rng)

    def order(
        self,
        candidates: list[RoutingCandidate],
        requirement: CapabilityRequirement | None = None,
        pin_first: bool = False,
        advance: bool = True,
    ) -> list[RoutingCandidate]:
        eligible, blocked = self._partition(candidates)
        eligible.sort(key=lambda c: (-c.deployment.priority, -c.score, c.label))
        if eligible:
            key = eligible[0].model.id
            offset = RoundRobinSelection._cursor.get(key, 0) % len(eligible)
            eligible = eligible[offset:] + eligible[:offset]
            if advance:
                RoundRobinSelection._cursor[key] = (offset + 1) % len(eligible)
        blocked.sort(key=lambda c: c.label)
        return self._number(eligible + blocked)


class WeightedSelection(SelectionStrategy):
    """Weighted random within priority bands (deterministic seeding in tests)."""

    name = AliasStrategy.WEIGHTED.value

    def order(
        self,
        candidates: list[RoutingCandidate],
        requirement: CapabilityRequirement | None = None,
        pin_first: bool = False,
        advance: bool = True,
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
                rng = self.rng
                if not advance:
                    rng = random.Random()  # noqa: S311
                    rng.setstate(self.rng.getstate())
                chosen = rng.choices(bucket, weights=weights, k=1)[0]
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
