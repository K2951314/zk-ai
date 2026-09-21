"""Alias registry + capability router + selection strategies."""

from __future__ import annotations

import pytest

from app.core.config import AppConfig
from app.core.errors import AliasNotFoundError, ModelNotFoundError
from app.models.provider import AliasStrategy
from app.models.request import ChatCompletionRequest, ChatMessage
from app.routing.aliases import BUILTIN_ALIASES, AliasRegistry
from app.routing.capability import infer_requirement, score_candidate
from app.routing.router import Router
from app.routing.strategy import (
    CapabilitySelection,
    CostSelection,
    PrioritySelection,
    SpeedSelection,
    available_strategies,
    get_strategy,
)
from tests.conftest import (
    make_alias,
    make_config,
    make_model,
    make_provider,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def build_router() -> Router:
    """A router with one provider and four differently-shaped models."""
    config = make_config(
        providers=[make_provider("fake", key_ids=("k1",))],
        models=[
            make_model("coder", priority=100, capabilities={"coding": 9.8, "reasoning": 6.0,
                                                             "speed": 5.0, "cost": 4.0}),
            make_model("thinker", priority=95, capabilities={"coding": 7.0, "reasoning": 9.8,
                                                             "long_context": 9.5, "cost": 3.0}),
            make_model("eye", priority=90, capabilities={"vision": 9.5, "coding": 6.0,
                                                         "reasoning": 6.0, "cost": 5.0}),
            make_model("sprinter", priority=85, capabilities={"speed": 9.8, "cost": 9.5,
                                                              "coding": 5.0, "reasoning": 4.0}),
        ],
        aliases=[
            make_alias("zk-all", ["coder", "thinker", "eye", "sprinter"]),
            make_alias("zk-code", ["coder", "thinker"], weights={"coding": 8.0}),
            make_alias("zk-vision", ["eye", "coder"], requires={"vision": 1}),
            make_alias("zk-cheapx", ["sprinter", "eye"], strategy=AliasStrategy.COST),
            make_alias("zk-fastx", ["sprinter", "coder"], strategy=AliasStrategy.SPEED),
            make_alias("zk-static", ["thinker", "coder"], strategy=AliasStrategy.PRIORITY),
        ],
    )
    return Router(config)


def request_for(model: str, **kwargs) -> ChatCompletionRequest:
    payload = {"model": model, "messages": [ChatMessage(role="user", content="hello")]}
    payload.update(kwargs)
    return ChatCompletionRequest(**payload)


# --------------------------------------------------------------------------- #
# Alias registry
# --------------------------------------------------------------------------- #
def test_alias_registry_resolves_and_expands() -> None:
    registry = AliasRegistry(
        [make_alias("a", ["m1", "m2"]), make_alias("b", ["a", "m3"])]
    )
    assert registry.is_alias("a")
    assert registry.expand("a", model_ids={"m1", "m2", "m3"}) == ["m1", "m2"]
    assert registry.expand("b", model_ids={"m1", "m2", "m3"}) == ["m1", "m2", "m3"]
    assert registry.expand("missing", model_ids={"m1"}) == []


def test_alias_registry_upsert_takes_effect_immediately() -> None:
    registry = AliasRegistry([make_alias("a", ["m1"])])
    registry.upsert(make_alias("a", ["m2"]))
    assert registry.get("a").targets == ["m2"]
    registry.enable("a", False)
    assert registry.get("a") is None  # disabled aliases are invisible to clients
    assert registry.get_raw("a") is not None


def test_alias_registry_remove_and_validate() -> None:
    registry = AliasRegistry([make_alias("a", ["m1", "ghost"])])
    problems = registry.validate(model_ids={"m1"})
    assert any("ghost" in problem for problem in problems)
    assert registry.remove("a") is True
    assert registry.remove("a") is False


def test_alias_registry_describe_payload() -> None:
    registry = AliasRegistry([make_alias("a", ["m1"], weights={"coding": 3.0})])
    described = registry.describe()["a"]
    assert described["targets"] == ["m1"]
    assert described["weights"] == {"coding": 3.0}


def test_default_alias_set_covers_the_documented_names() -> None:
    registry = AliasRegistry.default(model_ids=["m1", "m2"])
    names = set(registry.names())
    assert set(BUILTIN_ALIASES).issubset(names)
    assert registry.get("zk-coding").strategy is AliasStrategy.CAPABILITY


# --------------------------------------------------------------------------- #
# Router resolution
# --------------------------------------------------------------------------- #
def test_router_resolves_a_plain_model() -> None:
    router = build_router()
    decision = router.plan(request_for("coder"))
    assert decision.alias is None
    assert decision.resolved_models == ["coder"]
    assert decision.candidates[0].model.id == "coder"


def test_router_resolves_an_alias_into_its_targets() -> None:
    router = build_router()
    decision = router.plan(request_for("zk-code"))
    assert decision.alias == "zk-code"
    assert decision.resolved_models == ["coder", "thinker"]


def test_unknown_model_raises_model_not_found() -> None:
    router = build_router()
    with pytest.raises(ModelNotFoundError) as excinfo:
        router.plan(request_for("nope"))
    assert excinfo.value.http_status == 404


def test_alias_with_no_usable_target_raises_alias_not_found() -> None:
    config = make_config(
        providers=[make_provider("fake", key_ids=("k1",))],
        models=[make_model("m1")],
        aliases=[make_alias("zk-broken", ["ghost"])],
    )
    router = Router(config)
    with pytest.raises(AliasNotFoundError):
        router.plan(request_for("zk-broken"))


def test_model_with_no_deployments_is_rejected() -> None:
    model = make_model("empty")
    model.deployments = []
    config = make_config(models=[model])
    router = Router(config)
    with pytest.raises(ModelNotFoundError):
        router.plan(request_for("empty"))


def test_alias_can_be_rewired_at_runtime_without_client_changes() -> None:
    router = build_router()
    assert router.plan(request_for("zk-code")).candidates[0].model.id == "coder"
    router.aliases.upsert(make_alias("zk-code", ["thinker"]))
    assert router.plan(request_for("zk-code")).candidates[0].model.id == "thinker"


# --------------------------------------------------------------------------- #
# Capability router
# --------------------------------------------------------------------------- #
def test_code_prompt_prefers_the_coding_model() -> None:
    router = build_router()
    request = request_for(
        "zk-all",
        messages=[ChatMessage(role="user", content="```python\ndef f():\n    return 1\n```")],
    )
    decision = router.plan(request)
    assert decision.eligible_candidates()[0].model.id == "coder"
    assert "code-like content" in decision.reason


def test_reasoning_prompt_prefers_the_reasoning_model() -> None:
    router = build_router()
    request = request_for(
        "zk-all",
        messages=[
            ChatMessage(role="user", content="Prove step by step why this algorithm is correct")
        ],
    )
    decision = router.plan(request)
    assert decision.eligible_candidates()[0].model.id == "thinker"


def test_image_request_requires_vision_and_gates_out_blind_models() -> None:
    router = build_router()
    request = request_for(
        "zk-all",
        messages=[
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "what is in this screenshot?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            )
        ],
    )
    decision = router.plan(request)
    eligible = [c.model.id for c in decision.eligible_candidates()]
    assert eligible == ["eye"]
    blocked = [c for c in decision.candidates if not c.eligible]
    assert any("vision" in gate for candidate in blocked for gate in candidate.gates_failed)


def test_tools_force_the_tool_use_capability() -> None:
    # A tool-calling request needs models that actually declare ``tool_use``; the
    # default fixture gives every model 5.0, so this test configures models that
    # genuinely lack the capability.
    config = make_config(
        providers=[make_provider("fake", key_ids=("k1",))],
        models=[
            make_model("coder", capabilities={"coding": 9.8, "tool_use": 0.0}),
            make_model("thinker", capabilities={"reasoning": 9.8, "tool_use": 0.0}),
        ],
        aliases=[make_alias("zk-code", ["coder", "thinker"], weights={"coding": 8.0})],
    )
    router = Router(config)
    request = request_for(
        "zk-code",
        tools=[
            {
                "type": "function",
                "function": {"name": "f", "description": "d", "parameters": {}},
            }
        ],
    )
    decision = router.plan(request)
    assert decision.requirement.minimums.get("tool_use") == 1.0
    # Neither model declares tool_use, so both are gated out but still listed.
    assert all(not candidate.eligible for candidate in decision.candidates)
    assert decision.eligible_candidates() == []
    assert any("tool_use" in gate for c in decision.candidates for gate in c.gates_failed)


def test_json_mode_requires_structured_output() -> None:
    requirement = infer_requirement(
        request_for("zk-code", response_format={"type": "json_object"})
    )
    assert requirement.minimums["structured_output"] == 1.0
    assert requirement.weights["structured_output"] >= 4.0


def test_alias_requires_and_weights_override_inference() -> None:
    router = build_router()
    decision = router.plan(request_for("zk-vision"))
    # ``requires`` is a hard gate...
    assert decision.requirement.minimums["vision"] == 1.0
    # ...and a gate also dominates the score, so the vision model wins outright.
    assert decision.requirement.weights["vision"] == 8.0
    assert decision.candidates[0].model.id == "eye"
    assert [c.model.id for c in decision.eligible_candidates()] == ["eye"]


def test_long_context_requirement_raises_the_weight() -> None:
    router = build_router()
    long_prompt = "x " * 60_000
    request = request_for("zk-all", messages=[ChatMessage(role="user", content=long_prompt)])
    decision = router.plan(request)
    assert decision.requirement.estimated_tokens > 24_000
    assert decision.requirement.weights["long_context"] == 5.0


def test_context_window_gate_marks_a_candidate_ineligible() -> None:
    small = make_model("small", context_window=1_000)
    router = Router(
        make_config(
            models=[small],
            aliases=[make_alias("zk-small", ["small"])],
        )
    )
    request = request_for(
        "zk-small", messages=[ChatMessage(role="user", content="word " * 20_000)]
    )
    decision = router.plan(request)
    assert decision.candidates[0].eligible is False
    assert any("context window" in gate for gate in decision.candidates[0].gates_failed)


def test_disabled_deployment_is_gated_out() -> None:
    model = make_model("m1")
    model.deployments[0].enabled = False
    router = Router(make_config(models=[model]))
    decision = router.plan(request_for("m1"))
    assert decision.candidates[0].eligible is False
    assert "deployment disabled" in decision.candidates[0].gates_failed


def test_score_candidate_is_explainable() -> None:
    model = make_model("m1", capabilities={"coding": 8.0})
    requirement = infer_requirement(request_for("m1"))
    card = score_candidate(model=model, deployment=model.deployments[0], requirement=requirement)
    assert card.eligible is True
    assert card.total > 0
    assert "coding" in card.breakdown
    assert card.describe().startswith("score=")


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
def test_capability_strategy_orders_by_score() -> None:
    router = build_router()
    decision = router.plan(request_for("zk-all"))
    scores = [candidate.score for candidate in decision.eligible_candidates()]
    assert scores == sorted(scores, reverse=True)
    assert decision.strategy == "capability"

def test_capability_strategy_pin_first_overrides_the_score_ranking() -> None:
    """Regression: the console's model hot-swap must actually take effect.

    ``zk-auto`` uses ``strategy=capability``, so without a pin the highest-scoring
    model always won and promoting another model to ``targets[0]`` did nothing.
    ``pin_first=True`` makes the operator's choice lead the attempt order while the
    rest of the chain stays capability-ranked for failover.
    """
    router = build_router()
    # sprinter has the lowest capability score -> it cannot win by ranking.
    plain = router.plan(request_for("zk-all"))
    order_plain = [c.model.id for c in plain.eligible_candidates()]
    assert order_plain[0] != "sprinter"
    assert order_plain.index("sprinter") > 0

    router.aliases.upsert(
        make_alias("zk-all", ["sprinter", "coder", "thinker", "eye"], pin_first=True)
    )
    pinned = router.plan(request_for("zk-all"))
    order_pinned = [c.model.id for c in pinned.eligible_candidates()]
    assert order_pinned[0] == "sprinter"        # the pin leads
    assert sorted(order_pinned[1:]) == sorted([m for m in order_plain if m != "sprinter"])


def test_capability_strategy_without_pin_ignores_the_alias_order() -> None:
    """Default behaviour is unchanged: capability score decides, targets order does not."""
    router = build_router()
    # Same targets, reversed: without a pin the ranking is identical.
    router.aliases.upsert(make_alias("zk-all", ["sprinter", "coder", "thinker", "eye"]))
    reversed_order = [c.model.id for c in router.plan(request_for("zk-all")).eligible_candidates()]
    router.aliases.upsert(make_alias("zk-all", ["coder", "thinker", "eye", "sprinter"]))
    original_order = [c.model.id for c in router.plan(request_for("zk-all")).eligible_candidates()]
    assert reversed_order == original_order


def test_priority_strategy_keeps_the_alias_order() -> None:
    router = build_router()
    decision = router.plan(request_for("zk-static"))
    assert [c.model.id for c in decision.candidates] == ["thinker", "coder"]


def test_cost_strategy_prefers_the_cheapest_capable_model() -> None:
    router = build_router()
    decision = router.plan(request_for("zk-cheapx"))
    assert decision.candidates[0].model.id == "sprinter"


def test_speed_strategy_prefers_the_fastest_model() -> None:
    router = build_router()
    decision = router.plan(request_for("zk-fastx"))
    assert decision.candidates[0].model.id == "sprinter"


def test_strategy_registry_and_fallback() -> None:
    assert "capability" in available_strategies()
    assert isinstance(get_strategy("priority"), PrioritySelection)
    assert isinstance(get_strategy("cost"), CostSelection)
    assert isinstance(get_strategy("speed"), SpeedSelection)
    assert isinstance(get_strategy(None), CapabilitySelection)  # default
    assert isinstance(get_strategy("nonsense"), CapabilitySelection)


def test_unknown_alias_strategy_falls_back_to_capability() -> None:
    alias = make_alias("zk-weird", ["coder", "thinker"])
    object.__setattr__(alias, "strategy", AliasStrategy.CAPABILITY)
    registry = AliasRegistry([alias])
    assert registry.get("zk-weird").strategy is AliasStrategy.CAPABILITY


# --------------------------------------------------------------------------- #
# Preview + description
# --------------------------------------------------------------------------- #
def test_preview_reports_plan_scores_and_reasons() -> None:
    router = build_router()
    preview = router.preview(
        request_for("zk-code", messages=[ChatMessage(role="user", content="def f(): pass")])
    )
    assert preview["alias"] == "zk-code"
    assert preview["plan"][0]["model"] == "coder"
    assert preview["scores"]
    assert preview["requirement"]


def test_preview_handles_unknown_models_gracefully() -> None:
    router = build_router()
    preview = router.preview(request_for("does-not-exist"))
    assert preview["error"]["type"] == "model_not_found"
    assert "coder" in preview["known_models"]


def test_router_describe_exposes_topology() -> None:
    described = build_router().describe()
    assert "coder" in described["models"]
    assert "zk-code" in described["aliases"]
    assert described["providers"]["fake"] == "openai"


def test_decision_reason_mentions_strategy_and_order() -> None:
    decision = build_router().plan(request_for("zk-code"))
    assert "strategy=" in decision.reason
    assert "requested=zk-code" in decision.reason


def test_config_resolve_model_ids_helpers() -> None:
    config: AppConfig = make_config(
        models=[make_model("m1"), make_model("m2")],
        aliases=[make_alias("zk-a", ["m1"]), make_alias("zk-nested", ["zk-a", "m2"])],
    )
    assert config.resolve_model_ids("zk-nested") == ["m1", "m2"]
    assert config.resolve_model_ids("m1") == ["m1"]
    assert config.resolve_model_ids("ghost") == []
    assert config.is_alias("zk-a") is True
    assert config.known_names() == ["m1", "m2", "zk-a", "zk-nested"]
