"""agent-auto: request-shape-based alias rewriting, before the capability scorer.

The four tuning rounds documented in AGENTS.md proved one thing conclusively:
``zk-auto`` cannot put Kimi K3 and cheaper models in the same linear-weighted
chain and expect a middle ground. K3 dominates on every quality dimension, so
any weight vector that lets cheap models win some requests also lets them win
the *wrong* requests (deep reasoning stolen by step-5-preview at 0.951 vs
K3 0.998). There is no intermediate regime.

The fix is not to tune harder. It is to stop asking one weighted sum to do
two jobs (classify the request *and* rank the candidates). This module does
the classification separately, *before* the scorer sees the request:

  - The request's *shape* (has image? has tools? has tool results already in the
    history? estimated size? small output?) is read directly. Shape is objective;
    keywords are not.
  - Based on shape, ``zk-auto`` is rewritten to ``zk-long`` (mechanical/bulk
    work, deliberately without K3), ``zk-vision`` (image present), or left as
    ``zk-auto`` (genuine reasoning/coding, K3 stays in the chain).
  - The scorer then ranks candidates *inside* whichever chain was picked, with
    its existing weights. No scorer change, no new math.

Why this is hard where the old approach was soft:

  - A request with an image can only be served by a vision model. That is a
    hard gate, not a preference: ``zk-vision`` has ``requires: {vision: 8.0}``.
  - A request carrying 30 tool schemas is definitionally tool-driving; the
    Codex workload proves 84% of its tool rounds end in sub-500-token replies
    (ack / diff / status). Those rounds do not need K3's 9.5 reasoning.
    ``zk-long`` has no K3 in its target list at all, so the scorer *cannot*
    pick it, no matter what the linear combination says. The strongest version
    of this signal is "the history already carries tool output": that is an
    agent round the client has already planned, and it is what
    :data:`TOOL_TURN_IS_MECHANICAL` keys on - keyword heuristics never fire for
    it, because "run the tests and report" has no reasoning words in it.
  - A long-context request (>100K estimated tokens) has a hard window gate.
    ``zk-long``'s chain (glm-5.3 / step-5-preview) all have 1M windows; K3's
    sensenova deployment is the *only* 1M window K3 has, and it is already
    saturated (72% of all input tokens). Routing long mechanical reads away
    from K3 is the single biggest cost lever we have.

The rewrite is *not* a hard switch on the client-visible model name. The
``requested_model`` recorded in the DB stays ``zk-auto``; only the alias
used for candidate expansion changes. The response's ``zk_ai.alias`` field
shows the rewritten alias, so the decision is fully auditable.

Misclassification is bounded: the rewritten alias only changes *which chain*
ranks candidates, never *whether* a candidate is eligible. A request wrongly
sent to ``zk-long`` still gets a capable model (glm-5.3 is 9.5 coding); it
just does not get K3. A request wrongly kept on ``zk-auto`` pays K3's price
for a mechanical round — wasteful but never wrong. The asymmetry is
deliberate: false negatives (missed K3) are cheaper than false positives
(wasted K3).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.models.request import ChatCompletionRequest, ChatMessage

logger = get_logger("routing.agent_auto")

#: Estimated input tokens above which a request is "long context". Matches the
#: hard gate in capability.py (LONG_CONTEXT_HARD) but expressed here so the
#: rewrite can happen before the scorer builds its requirement.
LONG_CONTEXT_TOKENS = 100_000

#: Estimated input tokens above which a tool-carrying request is treated as
#: mechanical bulk work and sent to zk-long. Below this, a tool-carrying
#: request still gets the full zk-auto chain (K3 included), because short
#: tool-driving rounds are exactly where K3's planning quality pays off.
BULK_TOOL_TOKENS = 24_000

#: The client-visible alias that opts into shape-based rewriting. Any other
#: model name (including a bare model id like "kimi-k3") is left untouched:
#: an explicit choice is a contract, not a suggestion.
AGENT_AUTO_ALIAS = "zk-auto"

#: Rewritten aliases. They must exist in the alias registry; a missing target
#: used to be a configuration error, but that contract was written when rewrites
#: were rare edge cases (long context, bulk tools). Now that a tool *result* in
#: the history rewrites too, every Codex turn would hard-fail on a config that
#: simply never defined ``zk-long``. So a missing target degrades to ``zk-auto``
#: with a WARNING instead - better to pay flagship prices on a mechanical round
#: than to answer 404 to the entire agent workload. The operator still learns
#: about it from the log, once per request.
ALIAS_LONG = "zk-long"
ALIAS_VISION = "zk-vision"

#: Rewrite targets, checked against the alias registry when one is supplied.
_REWRITE_TARGETS = frozenset({ALIAS_LONG, ALIAS_VISION})

#: Treat a conversation that already contains tool *results* as an agent round.
#: A tool-calling turn is mechanical by shape - the client already decided the
#: plan, the model is being asked to consume tool output and continue - and
#: shape is objective, unlike the keyword heuristics in ``capability.py`` which
#: never fire for code-shaped tool rounds. Measured: 84% of Codex tool rounds
#: end in sub-500-token replies (ack / diff / status), which is exactly the
#: traffic that should not pay flagship rates.
#:
#: Deliberately **not** applied when the client only *declares* tools without
#: any result in the history: that is a planning round, and planning quality is
#: what the flagship is for. Set False to restore the token-count-only rule.
TOOL_TURN_IS_MECHANICAL = True


@dataclass(frozen=True, slots=True)
class RewriteDecision:
    """What the shape classifier decided, and why."""

    original: str
    rewritten: str
    reason: str
    estimated_tokens: int
    has_image: bool
    has_tools: bool
    has_tool_result: bool = False

    @property
    def changed(self) -> bool:
        return self.original != self.rewritten


def has_tool_result(messages: list[ChatMessage]) -> bool:
    """True when the history already carries a tool's *output*, not just a schema.

    ``role == "tool"`` is the chat-completions shape; ``tool_call_id`` on any
    role covers the Responses-side translation, which keeps the id on the
    message that carries the result instead of setting a tool role.
    """
    return any(m.role == "tool" or m.tool_call_id for m in messages)


def _available(rewritten: str, known_aliases: set[str] | None) -> str:
    """Fall back to ``zk-auto`` when the rewrite target cannot serve traffic.

    *known_aliases* is "aliases that resolve to at least one model" as computed by
    ``Router._servable_aliases()`` - **not** every name in the registry. Those are
    different questions, and the difference bit for real on 2026-09-29: a name-only
    check let the rewrite target be an ``enabled: false`` alias, so every image /
    tool-turn / long-context request 404'd with no warning at all.

    ``known_aliases=None`` skips the check entirely (callers without a registry,
    and the pure unit tests). See :data:`ALIAS_LONG` for why this degrades
    instead of raising: a missing ``zk-long`` used to be a rare misconfiguration
    and is now a hard blocker for all tool traffic, and 404-ing the whole agent
    workload is a far worse outcome than serving it expensively.
    """
    if known_aliases is None or rewritten not in _REWRITE_TARGETS:
        return rewritten
    if rewritten in known_aliases:
        return rewritten
    logger.warning(
        "agent-auto wants to rewrite to '%s' but it cannot serve traffic right now "
        "(not configured, disabled, or every model on it is unreachable); "
        "falling back to '%s'. Fix the alias in models.yaml to get the intended "
        "(cheaper) routing - see config/models.real.example.yaml.",
        rewritten,
        AGENT_AUTO_ALIAS,
    )
    return AGENT_AUTO_ALIAS


def rewrite_model(
    request: ChatCompletionRequest, *, known_aliases: set[str] | None = None
) -> RewriteDecision:
    """Classify *request* by shape and return the alias to expand.

    Only ``zk-auto`` is eligible for rewriting; every other model name is
    returned unchanged. The function is pure: no I/O, no side effects, and
    the request object is not mutated.

    ``known_aliases`` is the set of alias names the router can actually resolve.
    When given, a rewrite target that is not in it degrades to ``zk-auto`` with a
    warning (see :func:`_available`) instead of producing a plan that 404s.
    """
    requested = request.model
    estimated = request.estimated_input_tokens()
    has_image = any(m.has_image() for m in request.messages)
    has_tools = bool(request.tools)
    tool_result = has_tool_result(request.messages)

    def _done(rewritten: str, reason: str) -> RewriteDecision:
        return RewriteDecision(
            original=requested,
            rewritten=_available(rewritten, known_aliases),
            reason=reason,
            estimated_tokens=estimated,
            has_image=has_image,
            has_tools=has_tools,
            has_tool_result=tool_result,
        )

    if requested != AGENT_AUTO_ALIAS:
        return _done(requested, f"explicit model '{requested}' -> no rewrite")

    # 1. Vision is a hard gate, not a preference. A model without vision
    #    cannot see the image no matter how high its coding score is.
    if has_image:
        return _done(ALIAS_VISION, "request contains image -> zk-vision (hard vision gate)")

    # 2. An agent round (the history already holds tool output) is mechanical by
    #    shape. This is the one signal the keyword heuristics in ``capability.py``
    #    cannot produce: a tool round rarely contains "prove"/"root cause", so
    #    without this rule the flagship stays eligible for every ack/diff/status
    #    turn and the 9.5 reasoning is paid for nothing.
    if tool_result and TOOL_TURN_IS_MECHANICAL:
        return _done(
            ALIAS_LONG,
            "history already carries tool results -> zk-long (agent round, mechanical)",
        )

    # 3. Long context is a hard window gate. glm-5.3 and step-5-preview both
    #    have 1M windows and are currently idle; K3's only 1M deployment is
    #    saturated. Mechanical long reads belong on the fast chain.
    if estimated >= LONG_CONTEXT_TOKENS:
        return _done(
            ALIAS_LONG,
            f"~{estimated} tokens >= {LONG_CONTEXT_TOKENS} -> zk-long (long-context gate)",
        )

    # 4. Bulk tool-driving with a large context is mechanical work: Codex
    #    replaying 30 tool schemas and 50K tokens of history to produce a
    #    300-token ack does not need K3. The threshold is deliberately high
    #    so short, planning-heavy tool rounds still get K3.
    if has_tools and estimated >= BULK_TOOL_TOKENS:
        return _done(
            ALIAS_LONG,
            f"{len(request.tools or [])} tools + ~{estimated} tokens -> zk-long (bulk mechanical)",
        )

    # 5. Everything else stays on zk-auto: short questions, code generation,
    #    reasoning, and planning rounds (tools declared, no result yet) where
    #    K3's quality is worth its price.
    return _done(requested, "no bulk/vision/long signal -> zk-auto unchanged")
