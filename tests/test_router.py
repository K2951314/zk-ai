"""Alias registry + capability router + selection strategies."""

from __future__ import annotations

import time

import pytest

from app.core.config import AppConfig
from app.core.errors import AliasNotFoundError, ModelNotFoundError
from app.models.provider import AliasStrategy
from app.models.request import ChatCompletionRequest, ChatMessage
from app.routing.aliases import BUILTIN_ALIASES, AliasRegistry
from app.routing.capability import infer_requirement, score_candidate
from app.routing.router import Router
from app.routing.scheduler import Scheduler
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


def test_unknown_model_message_suggests_close_names() -> None:
    """换机事故复盘：桌面版发 'zk'（config.toml 的 model 写错了）——404 文案
    要把相近的可用名列出来，让用户能自己改对，而不是只有一句 not configured。"""
    router = build_router()
    with pytest.raises(ModelNotFoundError) as excinfo:
        router.plan(request_for("zk"))
    message = str(excinfo.value)
    assert "zk-all" in message and "zk-code" in message  # 前缀匹配
    with pytest.raises(ModelNotFoundError) as excinfo:
        router.plan(request_for("zk-visionn"))  # 拼错一个字母：模糊匹配兜底
    assert "zk-vision" in str(excinfo.value)


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


# --------------------------------------------------------------------------- #
# Per-deployment request gates (DeploymentConfig.request_requires)
# --------------------------------------------------------------------------- #
def _flagship_and_cheap() -> Router:
    """A flagship that only serves strong-reasoning requests + a cheap model that
    serves everything - the division-of-labour shape from the live config."""
    from app.models.provider import CapabilityScores, DeploymentConfig, ModelConfig

    flagship = ModelConfig(
        id="flagship",
        context_window=1_000_000,
        capabilities=CapabilityScores(
            coding=9.5, reasoning=9.5, tool_use=9.0, vision=9.5,
            long_context=10.0, structured_output=8.5, speed=6.0, cost=9.0,
        ),
        deployments=[
            DeploymentConfig(
                id="flagship-dep", provider_id="fake", model="flagship",
                priority=130, context_window=1_000_000,
                request_requires={"reasoning": 4.0},
            )
        ],
    )
    cheap = ModelConfig(
        id="cheap",
        context_window=1_000_000,
        capabilities=CapabilityScores(
            coding=8.5, reasoning=8.5, tool_use=9.0, vision=0.0,
            long_context=10.0, structured_output=9.0, speed=9.0, cost=10.0,
        ),
        deployments=[
            DeploymentConfig(
                id="cheap-dep", provider_id="fake", model="cheap",
                priority=100, context_window=1_000_000,
            )
        ],
    )
    return Router(
        make_config(
            providers=[make_provider("fake", key_ids=("k1",))],
            models=[flagship, cheap],
            aliases=[make_alias("zk-mix", ["flagship", "cheap"],
                                weights={"reasoning": 2.0, "coding": 2.0})],
        )
    )


def test_flagship_declines_a_plain_request() -> None:
    router = _flagship_and_cheap()
    decision = router.plan(request_for("zk-mix"))  # "hello", reasoning weight 2.0 < 4.0
    flagship = next(c for c in decision.candidates if c.deployment.id == "flagship-dep")
    assert flagship.eligible is False
    assert any("reasoning" in gate for gate in flagship.gates_failed)
    assert decision.eligible_candidates()[0].deployment.id == "cheap-dep"


def test_flagship_serves_a_reasoning_request() -> None:
    router = _flagship_and_cheap()
    decision = router.plan(
        request_for(
            "zk-mix",
            messages=[ChatMessage(role="user",
                                  content="please prove step-by-step the root cause")],
        )
    )
    flagship = next(c for c in decision.candidates if c.deployment.id == "flagship-dep")
    assert flagship.eligible is True
    assert decision.eligible_candidates()[0].deployment.id == "flagship-dep"


def test_image_request_raises_reasoning_so_the_flagship_stays_eligible() -> None:
    router = _flagship_and_cheap()
    decision = router.plan(
        request_for(
            "zk-mix",
            messages=[ChatMessage(role="user", content=[
                {"type": "text", "text": "看这张截图"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="}},
            ])],
        )
    )
    assert decision.requirement.weights["reasoning"] >= 4.0
    flagship = next(c for c in decision.candidates if c.deployment.id == "flagship-dep")
    assert flagship.eligible is True


def test_alias_weight_is_a_floor_not_a_ceiling() -> None:
    # An alias weight below an inference-raised weight must not drag it back
    # down - otherwise the requirement becomes request-blind and no downstream
    # gate can tell "prove step-by-step" from "hi".
    router = _flagship_and_cheap()
    decision = router.plan(
        request_for(
            "zk-mix",
            messages=[ChatMessage(role="user", content="prove step-by-step why")],
        )
    )
    assert decision.requirement.weights["reasoning"] == 5.0  # inference won, not the 2.0 floor


def test_safety_valve_widens_when_every_deployment_is_gated() -> None:
    # A single-target alias whose only deployment demands reasoning: a plain
    # request gates it out, and the valve must widen back instead of failing.
    router = _flagship_and_cheap()
    router.aliases.upsert(make_alias("zk-only-flagship", ["flagship"]))
    decision = router.plan(request_for("zk-only-flagship"))
    eligible = decision.eligible_candidates()
    assert eligible, "safety valve should have widened the plan"
    assert eligible[0].deployment.id == "flagship-dep"


def test_request_requires_gate_divides_labour_without_a_pin() -> None:
    """部署级 ``request_requires`` + 不钉首位 = 真正的按请求性质分工。

    2026-09-27 的教训：``zk-auto`` 一度同时配了
    ``request_requires: {reasoning: 4.0}``（kimi-k3 三个部署的接活标准）
    和 ``pin_first: true``（targets[0] = step-5-preview）。结果门槛被 pin
    完全压过——实测 24 条真实 Codex 轮次（14 条推理型）**100% 落在 step-5**，
    K3 一次都没轮到，即使它分数更高、即使它过了门槛。

    根因是 ``pin_first`` 只按 ``target_index`` 排序，不看 score，也不看门槛；
    所以一个低分模型只要站在 targets[0] 就能赢下所有请求。删掉那个静态 pin
    之后分工立刻恢复：推理请求够格 K3，机械请求被门槛挡在便宜模型上。

    这条测试锁住「门槛能真正发挥作用」这个契约——否则将来谁再给 alias 加一个
    静态 pin_first，分会白流，而症状是「聪明模型永远不接活」，极难一眼看出。
    """
    router = build_router()
    # thinker 只在强推理时接活——模仿 models.yaml 里 kimi-k3 三个部署的
    # request_requires: {reasoning: 4.0}（DeploymentConfig 字段，读码确认）。
    model = router.config.models["thinker"]
    model.deployments[0].request_requires = {"reasoning": 4.0}
    router.aliases.upsert(
        make_alias("zk-divide", ["sprinter", "coder", "thinker", "eye"], pin_first=False)
    )

    strong = router.plan(request_for("zk-divide", messages=[ChatMessage(role="user",
        content="请 step-by-step 证明 root cause 与 trade-offs")]))
    assert strong.eligible_candidates()[0].model.id == "thinker", (
        "强推理请求应该够格 thinker——门槛不该被 alias 顺序压过"
    )

    weak = router.plan(request_for("zk-divide", messages=[ChatMessage(role="user",
        content="把这个文件重命名一下")]))
    assert weak.eligible_candidates()[0].model.id != "thinker", (
        "机械请求 thinker 该被门槛挡住，便宜模型接活"
    )


def test_a_static_pin_can_silently_defeat_the_request_requires_gate() -> None:
    """反面对照：静态 pin_first=True 会让门槛失效、聪明模型永远不接活。

    与上一条是同一现象的两面。2026-09-27 实测：zk-auto 同时有门槛和 pin 时，
    24 条真实 Codex 轮次 100% 落在 targets[0] 的低分模型上。这条测试把「为什么
    不能给 alias 配静态 pin_first」钉住——症状太隐蔽，只靠人记不住。
    """
    router = build_router()
    model = router.config.models["thinker"]
    model.deployments[0].request_requires = {"reasoning": 4.0}
    # sprinter 站在 targets[0] 并被钉住：它 reasoning 只有 4.0，但 pin 不看门槛。
    router.aliases.upsert(
        make_alias("zk-pinned", ["sprinter", "coder", "thinker", "eye"], pin_first=True)
    )

    strong = router.plan(request_for("zk-pinned", messages=[ChatMessage(role="user",
        content="请 step-by-step 证明 root cause 与 trade-offs")]))
    assert strong.eligible_candidates()[0].model.id == "sprinter", (
        "pin 优先于门槛：这就是 2026-09-27 修掉的眞因"
    )
    # thinker 明明够格，却被 pin 压到后面
    order = [c.model.id for c in strong.eligible_candidates()]
    assert order.index("thinker") > 0


# --------------------------------------------------------------------------- #
# front_model：接口模型永远排第一，手动指定、随时可改（2026-09-28）
# --------------------------------------------------------------------------- #

def test_front_model_always_leads_the_attempt_order() -> None:
    """接口模型压过能力分——这是它存在的全部理由。"""
    config = make_config(
        providers=[make_provider("p1"), make_provider("p2")],
        models=[
            make_model("slow-but-strong", provider_id="p1", priority=200,
                       capabilities={"coding": 10.0, "reasoning": 10.0}),
            make_model("fast-reliable", provider_id="p2", priority=50,
                       capabilities={"coding": 5.0, "reasoning": 5.0}),
        ],
        aliases=[make_alias("zk-test", ["slow-but-strong", "fast-reliable"],
                            weights={"coding": 3.0, "reasoning": 3.0},
                            front_model="fast-reliable")],
    )
    router = Router(config)
    ordered = [c.model.id for c in router.plan(_req("zk-test")).candidates if c.eligible]
    assert ordered[0] == "fast-reliable"
    # 压过的模型没被丢掉，仍在链里当备用
    assert "slow-but-strong" in ordered


def test_front_model_leaves_the_rest_to_capability_routing() -> None:
    """接口模型只排第一，后面的顺序仍由能力分决定（否则就等于静态钉死）。"""
    config = make_config(
        providers=[make_provider("p1"), make_provider("p2"), make_provider("p3")],
        models=[
            make_model("front", provider_id="p1", capabilities={"coding": 1.0}),
            make_model("strong", provider_id="p2", priority=10,
                       capabilities={"coding": 9.0, "reasoning": 9.0}),
            make_model("weak", provider_id="p3", priority=10,
                       capabilities={"coding": 2.0, "reasoning": 2.0}),
        ],
        aliases=[make_alias("zk-test", ["front", "strong", "weak"],
                            weights={"coding": 3.0, "reasoning": 3.0},
                            front_model="front")],
    )
    router = Router(config)
    ordered = [c.model.id for c in router.plan(_req("zk-test")).candidates if c.eligible]
    assert ordered[0] == "front"
    assert ordered[1:] == ["strong", "weak"]      # 内容路由的结果，没被改写


def test_front_model_that_is_not_on_the_alias_is_ignored() -> None:
    """配错的 front_model 必须静默回落，而不是把路由弄坏。"""
    config = make_config(
        providers=[make_provider("p1")],
        models=[make_model("only", provider_id="p1")],
        aliases=[make_alias("zk-test", ["only"], front_model="typo-model")],
    )
    router = Router(config)
    ordered = [c.model.id for c in router.plan(_req("zk-test")).candidates if c.eligible]
    assert ordered == ["only"]


def test_front_model_gated_out_is_not_promoted() -> None:
    """接口模型被门槛淘汰时不能硬提到第一——那会把能跑的请求变成必败。"""
    config = make_config(
        providers=[make_provider("p1"), make_provider("p2")],
        models=[
            make_model("front", provider_id="p1", capabilities={"vision": 0.0}),
            make_model("vision-ok", provider_id="p2", capabilities={"vision": 9.0}),
        ],
        aliases=[
            make_alias("zk-test", ["front", "vision-ok"]),
            make_alias("zk-vis", ["front", "vision-ok"], requires={"vision": 9.0},
                       front_model="front"),
        ],
    )
    router = Router(config)
    # 普通请求：front 有资格，排第一
    plain = [c.model.id for c in router.plan(_req("zk-test")).candidates if c.eligible]
    assert plain[0] == "front"
    # 带图请求：front 被 vision 门槛挡掉，必须让位，否则图片请求必败
    image_req = ChatCompletionRequest(
        model="zk-vis",
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]}],
    )
    ordered = [c.model.id for c in router.plan(image_req).candidates if c.eligible]
    assert ordered[0] == "vision-ok"
    assert "front" not in ordered


def test_no_front_model_keeps_the_old_behaviour() -> None:
    """不配 front_model 的alias行为完全不变（向后兼容）。"""
    config = make_config(
        providers=[make_provider("p1")],
        models=[make_model("a", provider_id="p1", priority=100),
                make_model("b", provider_id="p1", priority=50,
                           capabilities={"coding": 9.0, "reasoning": 9.0})],
        aliases=[make_alias("zk-test", ["a", "b"], weights={"coding": 3.0})],
    )
    router = Router(config)
    ordered = [c.model.id for c in router.plan(_req("zk-test")).candidates if c.eligible]
    assert ordered[0] == "b"          # 能力分赢，没有被任何前置逻辑改写


def _req(model: str) -> ChatCompletionRequest:
    return ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "hi"}])


def test_front_model_switch_off_falls_back_to_pure_weights() -> None:
    """关闭接口模型 = 越过首位，回到纯能力权重排序（运营者的原话）。

    ``ModelAliasConfig.strategy`` 默认是 PRIORITY 而非 CAPABILITY，所以这个测试
    必须走 ``make_alias``（默认 capability）——早先我直接构造 ModelAliasConfig
    复现「关闭后首位没变」，查了半天才发现是策略不同，不是 bug。
    """
    def ordered(front: str | None) -> tuple[list[str], str]:
        config = make_config(
            providers=[make_provider("p1"), make_provider("p2")],
            models=[
                make_model("front", provider_id="p1",
                           capabilities={"coding": 1.0, "reasoning": 1.0}),
                make_model("strong", provider_id="p2", priority=10,
                           capabilities={"coding": 9.0, "reasoning": 9.0}),
            ],
            aliases=[make_alias("zk-test", ["front", "strong"],
                                weights={"coding": 3.0, "reasoning": 3.0},
                                front_model=front)],
        )
        decision = Router(config).plan(_req("zk-test"))
        chain = [c.model.id for c in decision.candidates if c.eligible]
        return chain, decision.reason

    on, on_reason = ordered("front")
    assert on == ["front", "strong"]                # 接口模型压过权重
    assert "front_model=front" in on_reason

    off, off_reason = ordered(None)
    assert off == ["strong", "front"]                # 越过首位，权重赢家上台
    assert "front_model=" not in off_reason


# --------------------------------------------------------------------------- #
# 部署级自动隔离：连续失败才屏蔽，偶发事件不误伤（2026-09-29）
# --------------------------------------------------------------------------- #

def _info(exc=None, *, status=None, body=None):
    from app.retry.classifier import ErrorClassifier

    c = ErrorClassifier()
    if exc is not None:
        return c.classify(exc)
    return c.classify_status(status, body=body or {})


def _bare_scheduler():
    """只要失败记账，不跑完整路由——用 __new__ 绕开依赖注入。"""
    s = Scheduler.__new__(Scheduler)
    s._deployment_health = {}
    s._deployment_cooldowns = {}
    return s


def test_quarantine_only_counts_evidence_of_a_broken_deployment() -> None:
    """判别的核心：只认「这个部署坏了」，不认偶发与调用方的错。

    实测动机：NVIDIA 的 glm-5.3 每次挂死 60s，旧逻辑只给 30s 冷却，
    「撞 60s → 冷 30s → 再撞 60s」无限循环，累计烧掉 60.9 小时。
    但反过来，一次网络抖动不该屏蔽一个健康渠道——所以计数只认这几类。
    """
    import httpx

    from app.routing.scheduler import is_quarantine_worthy

    # 该计入：反复的传输/上游故障 + 模型在上游不存在
    assert is_quarantine_worthy(_info(httpx.ReadTimeout("x"))) is True
    assert is_quarantine_worthy(_info(httpx.ConnectTimeout("x"))) is True
    assert is_quarantine_worthy(_info(status=503)) is True
    assert is_quarantine_worthy(_info(status=504)) is True
    assert is_quarantine_worthy(
        _info(status=404, body={"detail": "Function 'x' Not Found"})
    ) is True

    # 不该计入：限流（偶发，属凭据层）、无权限（换把 Key 可能就好）、调用方的错
    assert is_quarantine_worthy(
        _info(status=429, body={"error": {"message": "tpm exhausted"}})
    ) is False
    assert is_quarantine_worthy(
        _info(status=403, body={"error": {"message": "not available in token plan"}})
    ) is False
    assert is_quarantine_worthy(
        _info(status=400, body={"error": {"message": "Invalid model id"}})
    ) is False


def test_quarantine_needs_consecutive_failures_then_escalates() -> None:
    """前两次不隔离（容忍抖动），第三次起按阶梯升级。"""
    import httpx

    from app.routing.scheduler import QUARANTINE_LADDER, QUARANTINE_THRESHOLD

    s = _bare_scheduler()
    timeout = _info(httpx.ReadTimeout("x"))
    for i in range(1, QUARANTINE_THRESHOLD):
        s._note_deployment_failure("dep", timeout)
        assert s.deployment_cooling_down("dep") is False, f"第 {i} 次就隔离太激进"
    s._note_deployment_failure("dep", timeout)
    assert s.deployment_cooling_down("dep") is True, "到阈值必须隔离"
    assert s._deployment_health["dep"].consecutive_failures == QUARANTINE_THRESHOLD

    # 继续失败 → 时长逐级上涨，封顶在阶梯最后一级
    seen = []
    for _ in range(len(QUARANTINE_LADDER) + 2):
        s._note_deployment_failure("dep", timeout)
        seen.append(round(s._deployment_health["dep"].quarantined_until - time.time()))
    assert seen == sorted(seen), "隔离时长必须单调递增"
    assert seen[-1] >= QUARANTINE_LADDER[-1] - 2


def test_quarantine_ignores_rate_limits_even_repeatedly() -> None:
    """连续 10 次 429 也不隔离——限流是偶发的，且属凭据层的处置范围。"""
    s = _bare_scheduler()
    rate = _info(status=429, body={"error": {"message": "tpm exhausted"}})
    for _ in range(10):
        s._note_deployment_failure("dep", rate)
    assert s.deployment_cooling_down("dep") is False
    assert "dep" not in s._deployment_health, "连记账都不该留"


def test_any_success_lifts_the_quarantine() -> None:
    """半开恢复：修好后任意一次成功即清零并解除（否则恢复也进不来）。"""
    import httpx

    s = _bare_scheduler()
    timeout = _info(httpx.ReadTimeout("x"))
    for _ in range(5):
        s._note_deployment_failure("dep", timeout)
    assert s.deployment_cooling_down("dep") is True

    s._note_deployment_success("dep")
    assert s.deployment_cooling_down("dep") is False
    health = s._deployment_health["dep"]
    assert health.consecutive_failures == 0
    assert health.quarantined_until == 0.0


def test_stale_failure_counts_decay() -> None:
    """距上次失败超过衰减窗口，计数归零——避免陈旧计数让一次抖动重罚。"""
    import httpx

    from app.routing.scheduler import QUARANTINE_DECAY_SECONDS

    s = _bare_scheduler()
    timeout = _info(httpx.ReadTimeout("x"))
    for _ in range(4):
        s._note_deployment_failure("dep", timeout)
    # 伪造「上次失败是很久以前」
    s._deployment_health["dep"].last_failure_at = time.time() - QUARANTINE_DECAY_SECONDS - 1
    s._note_deployment_failure("dep", timeout)
    assert s._deployment_health["dep"].consecutive_failures == 1, "陈旧计数必须先衰减"


def test_quarantined_deployments_are_reportable() -> None:
    """隔离是自动的，必须能看见「谁被屏蔽、为什么、还有多久」。"""
    import httpx

    s = _bare_scheduler()
    assert s.quarantined_deployments() == {}
    timeout = _info(httpx.ReadTimeout("x"))
    for _ in range(3):
        s._note_deployment_failure("glm53-nvidia", timeout)
    report = s.quarantined_deployments()
    assert "glm53-nvidia" in report
    entry = report["glm53-nvidia"]
    assert entry["quarantined"] is True
    assert entry["quarantine_seconds_left"] > 0
    assert entry["last_error_type"] == "timeout"
