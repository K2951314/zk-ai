"""History budgeting: what may be dropped, and what must never be.

The invariants here are upstream *protocol* requirements, not preferences:

* a ``tool`` message whose ``assistant`` ``tool_calls`` was dropped is rejected
  by chat-completions upstreams (SenseNova answers with a bare 400), and the
  gateway's own docs record how that looks when the reverse happens;
* a conversation may not open on ``assistant`` or ``tool``;
* the system preamble and the final turn are not negotiable.

Everything else is a budget decision, so the tests focus on "did it trim as much
as it safely could, and not one message more".
"""

from __future__ import annotations

import pytest

from app.models.request import ChatCompletionRequest, ChatMessage
from app.services.history import message_tokens, trim_history

BIG = "x" * 4_000  # ~1000 tokens


def _turn(index: int, *, tools: bool = False, size: int = 4_000) -> list[ChatMessage]:
    if tools:
        return [
            ChatMessage(
                role="assistant",
                content=f"calling on turn {index}",
                tool_calls=[{
                    "id": f"call_{index}", "type": "function",
                    "function": {"name": "t", "arguments": '{"a": 1}'},
                }],
            ),
            ChatMessage(
                role="tool", tool_call_id=f"call_{index}", content=f"output {index} " + ("y" * size)
            ),
        ]
    return [ChatMessage(role="assistant", content=f"answer {index} " + ("y" * size))]


def _conversation(
    *, turns: int, tools: bool = False, system: bool = True, size: int = 4_000
) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    if system:
        messages.append(ChatMessage(role="system", content="be helpful " + ("z" * size)))
    for index in range(turns):
        messages.append(ChatMessage(role="user", content=f"question {index} " + ("y" * size)))
        messages.extend(_turn(index, tools=tools, size=size))
    messages.append(ChatMessage(role="user", content="the actual question " + ("y" * size)))
    return messages


def _assert_valid_shape(messages: list[ChatMessage]) -> None:
    """Every pairing/opening rule upstreams enforce."""
    open_calls: set[str] = set()
    for index, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls:
            open_calls = {call.id for call in message.tool_calls}
        elif message.role == "tool":
            assert message.tool_call_id in open_calls, (
                f"orphan tool message at {index}: its assistant call was dropped"
            )
            open_calls.discard(message.tool_call_id)
    first = next((m for m in messages if m.role != "system"), None)
    assert first is None or first.role == "user", (
        f"conversation opens on {first.role if first else None}"
    )
    assert messages[-1].content, "the final turn must survive"


# --------------------------------------------------------------------------- #
# Disabled / no-op paths
# --------------------------------------------------------------------------- #

def test_zero_budget_disables_trimming() -> None:
    request = ChatCompletionRequest(model="zk-auto", messages=_conversation(turns=20))
    before = list(request.messages)
    result = trim_history(request, budget=0)
    assert not result.changed
    assert request.messages == before


def test_under_budget_is_a_no_op() -> None:
    messages = _conversation(turns=3)
    request = ChatCompletionRequest(model="zk-auto", messages=messages)
    budget = request.estimated_input_tokens() + 5_000
    assert not trim_history(request, budget=budget).changed


def test_tail_too_big_for_budget_is_not_trimmed() -> None:
    """A budget smaller than the recent turns must fail loudly, not be mangled."""
    request = ChatCompletionRequest(model="zk-auto", messages=_conversation(turns=20))
    untouched = len(request.messages)
    assert untouched == 1 + 20 * 2 + 1
    result = trim_history(request, budget=100)
    assert not result.changed
    assert len(request.messages) == untouched


def test_two_message_conversation_is_untouched() -> None:
    request = ChatCompletionRequest(
        model="zk-auto",
        messages=[ChatMessage(role="user", content=BIG), ChatMessage(role="user", content="hi")],
    )
    assert not trim_history(request, budget=10).changed


# --------------------------------------------------------------------------- #
# Trimming actually happens
# --------------------------------------------------------------------------- #

def test_long_conversation_is_trimmed_under_budget() -> None:
    request = ChatCompletionRequest(model="zk-auto", messages=_conversation(turns=40))
    before = request.estimated_input_tokens()
    result = trim_history(request, budget=4_000)
    assert result.changed
    assert result.removed >= 2
    assert request.estimated_input_tokens() <= 4_000
    assert request.estimated_input_tokens() < before
    _assert_valid_shape(request.messages)


def test_trim_keeps_as_much_history_as_the_budget_allows() -> None:
    """Not one message more than necessary: trimming is a cost, not a goal.

    Small turns so the assertion is about the algorithm rather than about how a
    1000-token message straddles the budget line.
    """
    request = ChatCompletionRequest(model="zk-auto", messages=_conversation(turns=40, size=200))
    trim_history(request, budget=2_000)
    kept = request.estimated_input_tokens()
    # Within one small turn (~55 tokens) of the budget, never over it.
    assert 2_000 - 100 <= kept <= 2_000


def test_system_preamble_and_final_turn_always_survive() -> None:
    messages = _conversation(turns=30)
    final = messages[-1].content
    request = ChatCompletionRequest(model="zk-auto", messages=messages)
    trim_history(request, budget=3_000)
    assert request.messages[0].role == "system"
    assert request.messages[0].content.startswith("be helpful")
    assert request.messages[-1].content == final


def test_placeholder_says_what_was_dropped() -> None:
    request = ChatCompletionRequest(model="zk-auto", messages=_conversation(turns=30))
    result = trim_history(request, budget=3_000)
    placeholders = [
        m for m in request.messages
        if isinstance(m.content, str) and m.content.startswith("[ZK-AI]")
    ]
    assert len(placeholders) == 1
    assert str(result.removed) in placeholders[0].content


# --------------------------------------------------------------------------- #
# Tool-call pairing
# --------------------------------------------------------------------------- #

def test_tool_pairs_are_never_split() -> None:
    """The pairing rule, straight from the upstream 400 this would cause."""
    request = ChatCompletionRequest(model="zk-auto", messages=_conversation(turns=25, tools=True))
    before = len(request.messages)
    result = trim_history(request, budget=5_000)
    assert result.changed
    assert len(request.messages) < before
    _assert_valid_shape(request.messages)


def test_tool_conversation_is_trimmed_under_budget() -> None:
    request = ChatCompletionRequest(model="zk-auto", messages=_conversation(turns=25, tools=True))
    trim_history(request, budget=5_000)
    assert request.estimated_input_tokens() <= 5_000
    _assert_valid_shape(request.messages)


def test_no_preamble_still_trims_correctly() -> None:
    request = ChatCompletionRequest(
        model="zk-auto", messages=_conversation(turns=40, system=False)
    )
    result = trim_history(request, budget=3_000)
    assert result.changed
    # The placeholder opens the conversation; what follows may be assistant,
    # because the placeholder itself is what makes that legal.
    assert request.messages[0].role == "user"
    assert request.estimated_input_tokens() <= 3_000
    _assert_valid_shape(request.messages)


def test_assistant_only_suffix_is_trimmed_now_that_placeholder_opens_it() -> None:
    """A tail of bare ``assistant`` messages is trimmable: the placeholder is a
    ``user`` message and that is what legally opens the conversation. Requiring
    the suffix itself to start on ``user`` used to waste 40% of the budget."""
    messages = [ChatMessage(role="system", content="s")]
    messages.extend(_turn(index)[0] for index in range(20))
    messages.append(ChatMessage(role="user", content="final"))
    request = ChatCompletionRequest(model="zk-auto", messages=messages)
    result = trim_history(request, budget=1_500)
    assert result.changed
    assert request.messages[0].role == "system"
    assert request.messages[1].role == "user"          # the placeholder
    assert request.messages[2].role == "assistant"     # suffix starts here
    _assert_valid_shape(request.messages)


# --------------------------------------------------------------------------- #
# Cost model
# --------------------------------------------------------------------------- #

def test_message_tokens_counts_images_and_tool_arguments() -> None:
    """A budget that ignores either would under-report and trim nothing."""
    text_only = message_tokens(ChatMessage(role="user", content=BIG))
    with_call = message_tokens(ChatMessage(
        role="assistant",
        content=BIG,
        tool_calls=[{"id": "c", "function": {"name": "t", "arguments": "y" * 4_000}}],
    ))
    assert with_call > text_only
    with_image = message_tokens(ChatMessage(
        role="user",
        content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 4000}}],
    ))
    assert with_image > 0


@pytest.mark.parametrize("budget", [1, 100, 1_000, 50_000])
def test_trimming_never_exceeds_its_budget(budget: int) -> None:
    """Whatever the budget, the result either fits or the request is untouched."""
    request = ChatCompletionRequest(model="zk-auto", messages=_conversation(turns=30, tools=True))
    result = trim_history(request, budget=budget)
    if result.changed:
        assert request.estimated_input_tokens() <= budget
        _assert_valid_shape(request.messages)
