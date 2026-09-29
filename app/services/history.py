"""History budgeting: stop a growing conversation from re-billing the whole past.

The problem this module exists for
----------------------------------
``zk-auto`` clients (Codex above all) re-send the *entire* conversation on every
turn. That is the client's design, not a gateway bug, but it means turn 30 pays
for turns 1..29 again - and 84% of those turns are mechanical ack/diff/status
rounds whose output nobody will ever reference again. Measured on this gateway:
2,956 requests on one alias averaged 228,745 input tokens each.

What this module does **not** do
--------------------------------
It never rewrites what the model is asked. It only removes *old* turns the model
has already consumed, keeps every pairing rule that upstream providers enforce,
and says so in the payload with a placeholder line. If it cannot trim safely it
returns the request untouched - a silently broken tool call is far worse than an
expensive request.

Why it is off by default
------------------------
Dropping context can change an answer: a file read on turn 4 and referenced on
turn 40 genuinely disappears. The operator who wants the saving should opt in
with ``ZKAI_TRIM_HISTORY_TOKENS`` and watch the routing reason for the numbers.
Tuning that is a product decision, not something to sneak in behind a default.

Invariants (all covered by ``tests/test_history.py``)
-----------------------------------------------------
1. Never drop a ``tool`` message whose ``assistant`` ``tool_calls`` was dropped -
   providers reject a ``tool`` role with no matching call, and the gateway's own
   docs record SenseNova answering that with a bare 400.
2. Never drop the leading ``system`` / ``developer`` preamble.
3. Never drop the last message.
4. Never start the kept suffix on a ``tool`` message - it would be an orphan
   whose ``assistant`` call was dropped. (``assistant`` *is* allowed: the
   placeholder is emitted as ``user`` and legally opens the conversation, and
   forbidding it was measured to waste 40% of the budget.)
5. Never trim when the tail alone still exceeds the budget: that case must fail
   loudly (the ``ZKAI_MAX_INPUT_TOKENS`` gate) rather than be mangled.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.models.request import ChatCompletionRequest, ChatMessage

logger = get_logger("services.history")

#: Roles that may legally open a conversation.
_PREAMBLE_ROLES = frozenset({"system", "developer"})
#: Roles the kept suffix may **not** start on. ``tool`` is the only hard one: it
#: would be an orphan whose ``assistant`` ``tool_calls`` was just dropped, which
#: is exactly the shape upstreams reject. ``assistant`` is fine, because the
#: placeholder is inserted as a ``user`` message and *it* legally opens the
#: conversation - requiring the suffix itself to start on ``user`` would throw
#: away a whole turn for no protocol reason (measured: 40% of the budget).
_FORBIDDEN_SUFFIX_START = frozenset({"tool"})
#: Roughly matching ``ChatCompletionRequest.estimated_input_tokens``.
_CHARS_PER_TOKEN = 4

#: Only bother when at least this many turns actually disappear - trimming one
#: message to save 2% is not worth the placeholder's token cost or the surprise.
_MIN_REMOVED_MESSAGES = 2

#: Tokens set aside for the placeholder line. Generous on purpose (the line is
#: ~16 tokens); the exact post-trim estimate is re-checked against the budget.
_PLACEHOLDER_RESERVE = 64


@dataclass(frozen=True, slots=True)
class TrimResult:
    """Outcome of one trim decision."""

    messages: list[ChatMessage]
    removed: int = 0
    saved_tokens: int = 0
    before_tokens: int = 0
    after_tokens: int = 0

    @property
    def changed(self) -> bool:
        return self.removed > 0


def message_tokens(message: ChatMessage) -> int:
    """Estimated cost of one message, on the same footing as the request estimator.

    Text parts, ``tool_calls.function.arguments`` and image pixels all count -
    skipping any of them is how the request-level estimator used to under-report
    by 2.67x, and a budget built on an under-report trims nothing.
    """
    total = len(message.text()) // _CHARS_PER_TOKEN
    total += message.estimated_image_tokens()
    for call in message.tool_calls or ():
        total += len(call.function.arguments or "") // _CHARS_PER_TOKEN
    # Role envelope: the estimator counts it, so the budget must too.
    return total + 4


def _preamble_end(messages: list[ChatMessage]) -> int:
    """Index just past the leading system/developer preamble."""
    index = 0
    while index < len(messages) and messages[index].role in _PREAMBLE_ROLES:
        index += 1
    return index


def _trim_request(
    request: ChatCompletionRequest, *, budget: int
) -> TrimResult:
    messages = list(request.messages)
    before = request.estimated_input_tokens()
    if budget <= 0 or before <= budget or len(messages) < 3:
        return TrimResult(messages=messages, before_tokens=before, after_tokens=before)

    costs = [message_tokens(m) for m in messages]
    preamble_end = _preamble_end(messages)
    preamble_cost = sum(costs[:preamble_end])
    # Reserve room for the placeholder line; the final estimate is re-checked
    # exactly below, so this only has to be in the right ballpark.
    tail_budget = budget - preamble_cost - _PLACEHOLDER_RESERVE
    if tail_budget <= 0:
        return TrimResult(messages=messages, before_tokens=before, after_tokens=before)

    # Walk the history backwards and remember the *earliest* message whose
    # suffix still fits. Earliest, not the first one that fits: the suffix must be
    # as long as the budget allows, otherwise we throw away context for nothing.
    # ``tool`` can never start the suffix - its ``assistant`` call was just
    # dropped and a bare ``tool`` role is exactly what upstreams reject. Every
    # other role is fine because the placeholder is emitted as ``user`` and
    # legally opens the conversation.
    best_cut: int | None = None
    running = 0
    index = len(messages)
    while index > preamble_end:
        index -= 1
        running += costs[index]
        if running > tail_budget:
            break
        if messages[index].role not in _FORBIDDEN_SUFFIX_START:
            best_cut = index

    if best_cut is None or best_cut <= preamble_end:
        return TrimResult(messages=messages, before_tokens=before, after_tokens=before)

    removed = best_cut - preamble_end
    if removed < _MIN_REMOVED_MESSAGES:
        return TrimResult(messages=messages, before_tokens=before, after_tokens=before)

    saved = sum(costs[preamble_end:best_cut])
    trimmed = [*messages[:preamble_end], _placeholder(removed, saved), *messages[best_cut:]]
    after = sum(message_tokens(m) for m in trimmed)
    if after > budget:  # safety net: never leave the request over budget
        return TrimResult(messages=messages, before_tokens=before, after_tokens=before)
    return TrimResult(
        messages=trimmed,
        removed=removed,
        saved_tokens=saved,
        before_tokens=before,
        after_tokens=after,
    )


def _placeholder(removed: int, saved_tokens: int) -> ChatMessage:
    """The line that tells the model what it is no longer being shown."""
    return ChatMessage(
        role="user",
        content=(
            f"[ZK-AI] 为控制上下文长度，已省略更早的 {removed} 条历史消息"
            f"（约 {saved_tokens:,} tokens）。"
            "如需其中内容，请重新提出或引用具体部分。"
        ),
    )


def trim_history(request: ChatCompletionRequest, *, budget: int) -> TrimResult:
    """Budget *request*'s history in place, or leave it untouched.

    ``budget`` in estimated tokens; ``<= 0`` disables trimming entirely. The
    request's ``messages`` are replaced only when something was actually
    removed, so callers that pass the disabled default see zero change.
    """
    result = _trim_request(request, budget=budget)
    if not result.changed:
        return result
    request.messages = result.messages
    logger.warning(
        "历史裁剪：移除 %d 条旧消息，估算 input %d -> %d tokens（预算 %d）",
        result.removed,
        result.before_tokens,
        result.after_tokens,
        budget,
    )
    return result


__all__ = ["TrimResult", "message_tokens", "trim_history"]
