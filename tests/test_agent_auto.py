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
