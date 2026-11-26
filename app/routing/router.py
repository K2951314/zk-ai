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
from dataclasses import dataclass, field, replace
from typing import Any

from app.core.config import AppConfig
from app.core.errors import AliasNotFoundError, ModelNotFoundError, ProviderNotFoundError
from app.core.logging import get_logger
from app.models.provider import (
    DeploymentConfig,
    ModelAliasConfig,
    ModelConfig,
    ProviderConfig,
)
from app.models.request import ChatCompletionRequest
from app.providers.base import ProviderAdapter
from app.providers.factory import create_adapter
from app.routing.agent_auto import rewrite_model
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


@dataclass(slots=True, frozen=True)
class FrontModelOutcome:
    """What the front-line promotion actually did.

    ``applied`` is the model id that now leads the chain; ``note`` explains why
    nothing was promoted (e.g. "隔离/冷却中，未提升"). Splitting the two keeps
    the routing reason honest: it must never name a model the scheduler is going
    to skip.
    """

    applied: str | None
    note: str | None


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

    def _servable_aliases(self) -> set[str]:
        """别名里**现在真能产出候选**的那些（agent_auto 改写目标的可服务判据）。

        判据刻意与 :meth:`resolve_model_ids` 的第一步**逐字一致**（同一个
        ``expand(name, model_ids=set(self.config.models))``）：那里返回空就会抛
        ``AliasNotFoundError``，所以"expand 非空"就是"plan() 走得通"。
        自己另发明一套更严或更松的标准，就会重新造出"检查说行、实际 404"的裂缝。
        """
        model_ids = set(self.config.models)
        return {
            name
            for name in self.aliases.names()
            if self.aliases.expand(name, model_ids=model_ids)
        }

    # ------------------------------------------------------------------ #
    # Planning
    # ------------------------------------------------------------------ #
    def plan(
        self,
        request: ChatCompletionRequest,
        *,
        unavailable: set[str] | None = None,
    ) -> RoutingDecision:
        """Build the ordered attempt plan for *request*.

        *unavailable* is the set of deployment ids the scheduler currently refuses
        to call (short cooldown or automatic quarantine). It is passed **as data**,
        never fetched: the router does no I/O, and the only component that knows
        the health state is the scheduler. Without it the front-line model could
        be promoted to first place while being quarantined - the scheduler would
        then skip it anyway, and the routing reason would claim an order that
        never happened. That lie is expensive: it is what makes "my front model
        is set but nothing answers" look like a routing bug.
        """
        requested = request.model
        # agent-auto: rewrite the requested alias by request *shape* before the
        # scorer sees it. This is what makes "reasoning must stay on K3" hard:
        # the rewrite happens before candidate expansion, so the scorer only
        # ever ranks models inside the chain the shape picked. A mechanical
        # request sent to zk-long has no K3 in its candidate list at all, so
        # no weight vector can resurrect it.
        # 传「现在真能服务的别名」，不是「配置里写过的别名」。AliasRegistry.names()
        # 包含 enabled=false 的条目，而 expand() 对它们返回空列表——按名字判断会让
        # 改写目标看似可用，实际每个命中的请求都以 404 结束且一句 WARNING 都没有
        # （2026-09-29 第三轮审查抓到：控制台把 zk-long 停用 = 所有带图/工具轮/
        # 长上下文请求全灭）。这与「把 /v1/models 的目录当成能调用」是同一个错，
        # 只是这次犯在别名层。
        rewrite = rewrite_model(request, known_aliases=self._servable_aliases())
        if rewrite.changed:
            logger.info(
                "agent-auto: %s -> %s (%s)",
                rewrite.original,
                rewrite.rewritten,
                rewrite.reason,
            )
        model_ids, alias_name = self.resolve_model_ids(rewrite.rewritten)
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
                candidate = RoutingCandidate.from_scorecard(
                    model=model,
                    deployment=deployment,
                    provider=provider,
                    card=card,
                    target_index=target_index,
                )
                # 2026-11-26：冷却/隔离的腿降权到队尾（不删除），让健康的供应商
                # 先接活。以前 unavailable 只用于 front_model 提升，排序看不到它，
                # 额度耗尽 9.7h 的腿仍排队首被白跳一次。
                candidate.penalized = deployment.id in (unavailable or set())
                candidates.append(candidate)

        if not candidates:
            raise ModelNotFoundError(
                f"模型 '{requested}' 下没有任何可用的部署", model=requested
            )

        # 别名策略 > 模型策略 > 默认 capability（决定**跨模型**顺序）
        strategy_name = alias.strategy if alias else (primary_model.strategy if primary_model else None)
        strategy = get_strategy(strategy_name)
        ordered = strategy.order(candidates, requirement, pin_first=bool(alias and alias.pin_first))

        # 2026-11-26：模型内多部署是**独立的分担池**。别名策略（如 ds 的 priority）
        # 决定模型间的先后，但不该盖掉模型自己配的 round_robin/weighted——否则
        # 同一模型加的 3 家供应商永远只有 label 最小的那家接活。
        # 只在模型占用的槽位内重排，不改变跨模型顺序，也不动 capability 全局语义。
        ordered = self._rotate_model_local_pools(ordered, requirement)

        front = self._promote_front_model(ordered, alias, unavailable or set())

        # Safety valve: per-deployment ``request_requires`` gates can leave a
        # request with zero eligible deployments (e.g. every deployment on the
        # list demands reasoning, but this request is a plain "hi"). That is an
        # operator misconfiguration, not a request error, so we fall back to
        # considering the gated-out deployments rather than returning nothing.
        # The gates still order (gated candidates sort after eligible ones), so
        # this only ever *widens* the fallback chain.
        if ordered and not any(c.eligible for c in ordered):
            # minimums 也放宽：请求带图但所有部署都是纯文本模型时，
            # vision 闸门会把 order 清空、0 尝试直接拒——宁可发出去让上游
            # 自己报错，也不要本地就掐死。
            relaxed_req = replace(requirement, minimums={})
            relaxed: list[RoutingCandidate] = []
            for candidate in candidates:
                card = score_candidate(
                    model=candidate.model,
                    deployment=_without_request_gate(candidate.deployment),
                    requirement=relaxed_req,
                )
                relaxed_candidate = RoutingCandidate.from_scorecard(
                    model=candidate.model,
                    deployment=candidate.deployment,
                    provider=candidate.provider,
                    card=card,
                    target_index=candidate.target_index,
                )
                # 兜底也不能把冷却中的腿提到队首（与首次打分同一判据）。
                relaxed_candidate.penalized = candidate.deployment.id in (unavailable or set())
                relaxed.append(relaxed_candidate)
            ordered = strategy.order(
                relaxed, relaxed_req, pin_first=bool(alias and alias.pin_first)
            )
            # The front-line model must survive the safety valve too: if it were
            # dropped here, a request whose gates all fail would silently lose the
            # only model that answers, which is exactly the case where the
            # operator needs it most.
            self._promote_front_model(ordered, alias, unavailable or set())
            logger.warning(
                "alias '%s' left no eligible deployment (all request_requires gates failed); "
                "falling back to the ungated ranking - this usually means the deployment "
                "gates are over-tight",
                alias_name,
            )

        reason_parts = [
            f"requested={requested}",
            f"alias={alias_name or '-'}",
            f"strategy={strategy.name}",
            f"order={[c.deployment.id for c in ordered if c.eligible][:4]}",
        ]
        if rewrite.changed:
            reason_parts.append(f"agent_auto={rewrite.rewritten}({rewrite.reason})")
        if front.applied:
            reason_parts.append(f"front_model={front.applied}")
        elif front.note:
            reason_parts.append(f"front_model={front.note}")
        if requirement.notes:
            reason_parts.append("hints: " + "; ".join(requirement.notes))
        blocked = [c.deployment.id for c in ordered if not c.eligible]
        if blocked:
            reason_parts.append(f"gated_out={blocked[:3]}")
        parked = [c.deployment.id for c in ordered if c.penalized]
        if parked:
            reason_parts.append(f"parked={parked[:3]}")

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

    def _rotate_model_local_pools(
        self,
        ordered: list[RoutingCandidate],
        requirement: CapabilityRequirement,
    ) -> list[RoutingCandidate]:
        """模型内多部署是独立分担池，按模型自己的策略轮转（2026-11-26）。

        背景：`ds` 别名是 ``priority``，而 ``deepseek-flash`` 模型挂了 3 家并配了
        ``round_robin``。旧逻辑「别名策略 > 模型策略」让模型轮转被完全盖掉，
        3 家永远按 label 字母序、`a-ds` 一家独大——用户看到的"加了供应商没用"。

        做法：只在**同一模型占用的槽位内**重排，跨模型顺序由外层别名策略决定，
        保持不动。因此 capability 的全局打分排序语义不受影响（多是单部署模型），
        priority 别名仍能钉住 targets 顺序。只有模型显式配了 round_robin/weighted
        且部署数 ≥2 时才轮转，其余模型零改动。

        ⚠ 轮转只作用于**健康腿**：冷却/隔离的候选已被降权到队尾，这里只在它们
        之外的槽位转，避免把停掉的腿又转回首跳。
        """
        # 按模型分组，记住每组在 ordered 中的位置（可能不连续）。
        slots: dict[str, list[int]] = {}
        for index, candidate in enumerate(ordered):
            slots.setdefault(candidate.model.id, []).append(index)

        result = list(ordered)
        for model_id, positions in slots.items():
            if len(positions) < 2:
                continue
            model = self.config.models.get(model_id)
            if model is None or model.strategy not in ("round_robin", "weighted"):
                continue
            group = [result[position] for position in positions]
            # 健康腿先轮转，停掉的腿留在原位（它们本就在队尾）。
            healthy = [c for c in group if not c.penalized]
            if len(healthy) < 2:
                continue
            rotated = get_strategy(model.strategy).order(healthy, requirement)
            healthy_slots = [
                position for position in positions if not result[position].penalized
            ]
            for position, candidate in zip(healthy_slots, rotated, strict=True):
                result[position] = candidate
        return result

    def _promote_front_model(
        self,
        ordered: list[RoutingCandidate],
        alias: ModelAliasConfig | None,
        unavailable: set[str] | None = None,
    ) -> FrontModelOutcome:
        """Float *alias.front_model* to the head of the attempt order.

        Returns what actually happened, so the routing reason cannot claim an
        order the scheduler will not follow.

        Deliberately conservative, because the whole point is *reliability*:

        * only an **eligible** candidate is promoted. A model gated out by
          ``request_requires`` (or with no deployment at all) must not be forced
          to the front - that would turn a working request into a guaranteed
          failure, which is the opposite of what a front-line model is for;
        * only a deployment the scheduler will actually **call** is promoted. One
          that is cooling down or quarantined gets skipped at execution time
          (scheduler.py ``deployment_cooling_down``), so promoting it would buy
          nothing and cost the truth - the log would name a model that never
          received the request. This is the gap found in the 2026-09-29 review:
          the operator's front model was 403-dead and still reported as first;
        * the promotion is a reorder, not a filter: everything the capability
          router ranked stays in the chain behind it, so content-based selection
          still decides the *backup* and the failover order;
        * when the front model is missing, ineligible, or unavailable the chain is
          returned untouched, so a typo in ``front_model`` degrades to the old
          behaviour instead of breaking routing.

        Callers hold no locks; ``ordered`` is mutated in place because it is a
        freshly built list owned by ``plan()``.
        """
        if alias is None or not alias.front_model:
            return FrontModelOutcome(None, None)
        wanted = alias.front_model
        parked: list[str] = []
        for index, candidate in enumerate(ordered):
            if candidate.model.id != wanted or not candidate.eligible:
                continue
            if unavailable and candidate.deployment.id in unavailable:
                parked.append(candidate.deployment.id)
                continue
            if index == 0:
                return FrontModelOutcome(wanted, None)  # already leading
            ordered.insert(0, ordered.pop(index))
            return FrontModelOutcome(wanted, None)
        if parked:
            logger.debug(
                "alias '%s' front_model '%s' is parked (cooldown/quarantine: %s); "
                "keeping the routed order",
                alias.name,
                wanted,
                "、".join(parked),
            )
            return FrontModelOutcome(None, f"{wanted}(隔离/冷却中，未提升)")
        logger.debug(
            "alias '%s' front_model '%s' is not an eligible candidate; "
            "keeping the routed order",
            alias.name,
            wanted,
        )
        return FrontModelOutcome(None, None)

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


def _without_request_gate(deployment: DeploymentConfig) -> DeploymentConfig:
    """Copy of *deployment* with the per-deployment request gate removed.

    Used only by :meth:`Router.plan`'s safety valve, so the rest of the gate
    stays authoritative. Cheaper than ``model_copy(update=...)`` because the
    field set is tiny and we only need the gate cleared.
    """
    return deployment.model_copy(update={"request_requires": {}})


def _card_from_candidate(candidate: RoutingCandidate):
    """Rebuild a minimal ScoreCard from a candidate (for preview output)."""
    from app.routing.capability import ScoreCard

    return ScoreCard(
        total=candidate.score,
        breakdown=candidate.breakdown,
        gates_failed=candidate.gates_failed,
    )
