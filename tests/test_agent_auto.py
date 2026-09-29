"""agent-auto 的回归测试：请求形状 → alias 改写。

每一条规则都对应 AGENTS.md 里的一次真实教训：

- 「推理请求必须 K3」不是调权能调出来的，是把 K3 从候选集里物理移除。
- 「机械请求必须离开 K3」也不是调权能调出来的，是把 zk-auto 改写成 zk-long。
- 客户端明确写了模型名（kimi-k3 / zk-k3 / glm-5.3）时，改写是**禁止**的——
  显式选择是契约，不是建议。
"""

from __future__ import annotations

import pytest

from app.models.request import ChatCompletionRequest, ChatMessage
from app.routing.agent_auto import AGENT_AUTO_ALIAS, ALIAS_LONG, ALIAS_VISION, rewrite_model
from tests.conftest import (
    FakeAdapter,
    build_harness,
    make_alias,
    make_config,
    make_model,
    make_provider,
)


def _req(model: str, *, tools: int = 0, image: bool = False, history_chars: int = 0,
         text: str = "hello") -> ChatCompletionRequest:
    msgs: list[ChatMessage] = []
    if history_chars:
        msgs.append(ChatMessage(role="system", content="x" * history_chars))
    msgs.append(ChatMessage(role="user", content=text))
    if image:
        msgs.append(ChatMessage(role="user", content=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
        ]))
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": f"t{i}",
                "description": "x" * 100,
                "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
            },
        }
        for i in range(tools)
    ] or None
    return ChatCompletionRequest(model=model, messages=msgs, tools=tool_defs)


# --------------------------------------------------------------------------- #
# Pure classifier
# --------------------------------------------------------------------------- #

def test_short_reasoning_stays_on_zk_auto() -> None:
    req = _req(AGENT_AUTO_ALIAS, text="step by step prove the root cause")
    rw = rewrite_model(req)
    assert rw.rewritten == AGENT_AUTO_ALIAS
    assert not rw.changed


def test_bulk_tool_round_rewrites_to_zk_long() -> None:
    req = _req(AGENT_AUTO_ALIAS, tools=31, history_chars=200_000)
    rw = rewrite_model(req)
    assert rw.rewritten == ALIAS_LONG
    assert rw.changed
    assert "bulk mechanical" in rw.reason


def test_long_context_without_tools_rewrites_to_zk_long() -> None:
    req = _req(AGENT_AUTO_ALIAS, history_chars=500_000)
    rw = rewrite_model(req)
    assert rw.rewritten == ALIAS_LONG
    assert "long-context" in rw.reason


def test_image_rewrites_to_zk_vision() -> None:
    req = _req(AGENT_AUTO_ALIAS, image=True)
    rw = rewrite_model(req)
    assert rw.rewritten == ALIAS_VISION
    assert rw.has_image


def test_image_beats_long_context() -> None:
    # An image request that is also long must go to vision: the vision gate is
    # harder than the window gate (a blind model cannot see at all; a small-
    # window model can still read a truncated context).
    req = _req(AGENT_AUTO_ALIAS, image=True, history_chars=500_000)
    rw = rewrite_model(req)
    assert rw.rewritten == ALIAS_VISION


@pytest.mark.parametrize("model", ["kimi-k3", "glm-5.3", "zk-k3", "zk-long", "step-5-preview"])
def test_explicit_model_is_never_rewritten(model: str) -> None:
    req = _req(model, tools=31, history_chars=200_000, image=True)
    rw = rewrite_model(req)
    assert rw.rewritten == model
    assert not rw.changed


# --------------------------------------------------------------------------- #
# Tool-turn detection (2026-09-28)
# --------------------------------------------------------------------------- #

def _agent_round(upstream_text: str = "继续") -> ChatCompletionRequest:
    """A history that already holds a tool result - the Codex mid-loop shape."""
    return ChatCompletionRequest(
        model=AGENT_AUTO_ALIAS,
        messages=[
            ChatMessage(role="system", content="you are an agent"),
            ChatMessage(role="user", content="跑一下测试"),
            ChatMessage(
                role="assistant",
                content="我先跑",
                tool_calls=[
                    {"id": "call_1", "type": "function",
                     "function": {"name": "run_tests", "arguments": "{}"}}
                ],
            ),
            ChatMessage(role="tool", tool_call_id="call_1", content="3 passed 1 failed"),
            ChatMessage(role="user", content=upstream_text),
        ],
        tools=[{"type": "function", "function": {"name": "run_tests", "parameters": {}}}],
    )


def test_history_with_tool_result_is_mechanical() -> None:
    """The one signal keyword heuristics cannot produce: "run the tests" has no
    reasoning words, so without this rule the flagship stays eligible for every
    ack round."""
    rw = rewrite_model(_agent_round())
    assert rw.rewritten == ALIAS_LONG
    assert rw.changed
    assert rw.has_tool_result
    assert "agent round" in rw.reason


def test_declared_tools_without_results_still_reach_k3() -> None:
    """Planning round: the client only *declares* tools. That is where the
    flagship's planning quality pays, so it must stay on zk-auto."""
    req = _req(AGENT_AUTO_ALIAS, tools=4, history_chars=8_000,
               text="帮我规划一下怎么修这个 bug")
    rw = rewrite_model(req)
    assert rw.rewritten == AGENT_AUTO_ALIAS
    assert not rw.changed
    assert not rw.has_tool_result


def test_tool_result_beats_bulk_tool_threshold() -> None:
    """A short agent round must rewrite even when it is far below BULK_TOOL_TOKENS."""
    req = _agent_round()
    assert req.estimated_input_tokens() < 24_000
    assert rewrite_model(req).rewritten == ALIAS_LONG


def test_image_still_wins_over_tool_result() -> None:
    """Vision is the harder gate: a blind model cannot see at all, a small-window
    model can still read a truncated context."""
    req = ChatCompletionRequest(
        model=AGENT_AUTO_ALIAS,
        messages=[
            ChatMessage(role="tool", tool_call_id="call_1", content="ok"),
            ChatMessage(role="user", content=[
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
            ]),
        ],
    )
    assert rewrite_model(req).rewritten == ALIAS_VISION


def test_tool_turn_rule_can_be_switched_off() -> None:
    """The knob exists because dropping context is a product decision, not a
    default - see ``app/services/history.py`` for the same reasoning."""
    import app.routing.agent_auto as agent_auto

    original = agent_auto.TOOL_TURN_IS_MECHANICAL
    agent_auto.TOOL_TURN_IS_MECHANICAL = False
    try:
        rw = rewrite_model(_agent_round())
        assert rw.rewritten == AGENT_AUTO_ALIAS
        assert rw.has_tool_result  # still detected, just not acted on
    finally:
        agent_auto.TOOL_TURN_IS_MECHANICAL = original


def test_responses_side_tool_role_is_detected() -> None:
    """/v1/responses translates ``function_call_output`` into ``role="tool"``
    with the ``call_id`` on the message; the rewrite must see that too, or the
    Codex path would keep sending every round to the flagship."""
    req = _agent_round()
    req.messages[3].tool_call_id = "call_1"
    assert rewrite_model(req).has_tool_result
    assert rewrite_model(req).rewritten == ALIAS_LONG


# --------------------------------------------------------------------------- #
# Missing rewrite target degrades instead of 404-ing the workload (2026-09-28)
# --------------------------------------------------------------------------- #

def test_missing_zk_long_falls_back_to_zk_auto() -> None:
    """A config that never defined zk-long must not turn every agent round into a 404.

    This is not hypothetical: ``tests/test_agent.py`` ships an agent config with
    only ``zk-auto``, and 16 of its tests failed the moment the tool-turn rule
    started firing. Real minimal configs are the same shape.
    """
    rw = rewrite_model(_agent_round(), known_aliases={"zk-auto"})
    assert rw.rewritten == AGENT_AUTO_ALIAS
    assert not rw.changed
    assert rw.has_tool_result  # still detected, just not acted on


def test_present_zk_long_still_rewrites() -> None:
    rw = rewrite_model(_agent_round(), known_aliases={"zk-auto", "zk-long"})
    assert rw.rewritten == ALIAS_LONG


def test_missing_zk_vision_falls_back_for_images() -> None:
    req = _req(AGENT_AUTO_ALIAS, image=True)
    assert rewrite_model(req, known_aliases={"zk-auto"}).rewritten == AGENT_AUTO_ALIAS
    assert rewrite_model(req, known_aliases={"zk-auto", "zk-vision"}).rewritten == ALIAS_VISION


def test_no_registry_supplied_keeps_the_old_contract() -> None:
    """``known_aliases=None`` means "don't check" - the pure unit tests."""
    assert rewrite_model(_agent_round()).rewritten == ALIAS_LONG


def test_unknown_aliases_do_not_trigger_the_fallback() -> None:
    """A target that is not one of ours is never rewritten, so it needs no check."""
    rw = rewrite_model(_req(AGENT_AUTO_ALIAS, text="hello"), known_aliases={"zk-auto"})
    assert rw.rewritten == AGENT_AUTO_ALIAS


# --------------------------------------------------------------------------- #
# End-to-end through the router
# --------------------------------------------------------------------------- #

async def test_router_rewrites_zk_auto_to_zk_long_and_excludes_k3() -> None:
    """A bulk mechanical request on zk-auto must not have K3 in its candidate list."""
    provider = make_provider("fake")
    models = [
        make_model("fake-k3", capabilities={"coding": 9.5, "reasoning": 9.5, "long_context": 10.0}),
        make_model("fake-glm", capabilities={"coding": 8.5, "reasoning": 8.5, "long_context": 9.0}),
        make_model("fake-flash", capabilities={"coding": 7.0, "reasoning": 7.0, "long_context": 6.0}),
    ]
    config = make_config(
        providers=[provider],
        models=models,
        aliases=[
            make_alias("zk-auto", ["fake-k3", "fake-glm", "fake-flash"]),
            make_alias("zk-long", ["fake-glm", "fake-flash"]),
            make_alias("zk-vision", ["fake-k3"]),
        ],
    )
    adapter = FakeAdapter(provider)
    harness = await build_harness(config, adapters={"fake": adapter})
    try:
        req = _req("zk-auto", tools=31, history_chars=200_000)
        decision = harness.router.plan(req)
        assert decision.alias == "zk-long"
        assert "fake-k3" not in decision.resolved_models
        assert decision.candidates[0].model.id == "fake-glm"
        assert "agent_auto=zk-long" in decision.reason
    finally:
        await harness.container.shutdown()


async def test_router_keeps_k3_for_short_reasoning() -> None:
    """A short reasoning request must stay on zk-auto and let K3 win."""
    provider = make_provider("fake")
    models = [
        make_model("fake-k3", capabilities={"coding": 9.5, "reasoning": 9.5}),
        make_model("fake-glm", capabilities={"coding": 8.5, "reasoning": 8.5}),
    ]
    config = make_config(
        providers=[provider],
        models=models,
        aliases=[
            make_alias("zk-auto", ["fake-k3", "fake-glm"]),
            make_alias("zk-long", ["fake-glm"]),
            make_alias("zk-vision", ["fake-k3"]),
        ],
    )
    adapter = FakeAdapter(provider)
    harness = await build_harness(config, adapters={"fake": adapter})
    try:
        req = _req("zk-auto", text="step by step prove the root cause")
        decision = harness.router.plan(req)
        assert decision.alias == "zk-auto"
        assert "fake-k3" in decision.resolved_models
        assert decision.candidates[0].model.id == "fake-k3"
    finally:
        await harness.container.shutdown()


async def test_router_respects_explicit_kimi_k3() -> None:
    """An explicit model name must bypass the rewrite entirely."""
    provider = make_provider("fake")
    models = [
        make_model("fake-k3", capabilities={"coding": 9.5, "reasoning": 9.5}),
        make_model("fake-glm", capabilities={"coding": 8.5, "reasoning": 8.5}),
    ]
    config = make_config(
        providers=[provider],
        models=models,
        aliases=[
            make_alias("zk-auto", ["fake-k3", "fake-glm"]),
            make_alias("zk-long", ["fake-glm"]),
        ],
    )
    adapter = FakeAdapter(provider)
    harness = await build_harness(config, adapters={"fake": adapter})
    try:
        req = _req("fake-k3", tools=31, history_chars=200_000)
        decision = harness.router.plan(req)
        assert decision.alias is None  # bare model id, not an alias
        assert decision.resolved_models == ["fake-k3"]
        assert decision.candidates[0].model.id == "fake-k3"
    finally:
        await harness.container.shutdown()
