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

  - The request's *shape* (has image? has tools? estimated size? small output?)
    is read directly. Shape is objective; keywords are not.
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
    pick it, no matter what the linear combination says.
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
from app.models.request import ChatCompletionRequest

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
#: is a configuration error, not a silent fallback.
ALIAS_LONG = "zk-long"
ALIAS_VISION = "zk-vision"


@dataclass(frozen=True, slots=True)
class RewriteDecision:
    """What the shape classifier decided, and why."""

    original: str
    rewritten: str
    reason: str
    estimated_tokens: int
    has_image: bool
    has_tools: bool

    @property
    def changed(self) -> bool:
        return self.original != self.rewritten


def rewrite_model(request: ChatCompletionRequest) -> RewriteDecision:
    """Classify *request* by shape and return the alias to expand.

    Only ``zk-auto`` is eligible for rewriting; every other model name is
    returned unchanged. The function is pure: no I/O, no side effects, and
    the request object is not mutated.
    """
    requested = request.model
    estimated = request.estimated_input_tokens()
    has_image = any(m.has_image() for m in request.messages)
    has_tools = bool(request.tools)

    if requested != AGENT_AUTO_ALIAS:
        return RewriteDecision(
            original=requested,
            rewritten=requested,
            reason=f"explicit model '{requested}' -> no rewrite",
            estimated_tokens=estimated,
            has_image=has_image,
            has_tools=has_tools,
        )

    # 1. Vision is a hard gate, not a preference. A model without vision
    #    cannot see the image no matter how high its coding score is.
    if has_image:
        return RewriteDecision(
            original=requested,
            rewritten=ALIAS_VISION,
            reason="request contains image -> zk-vision (hard vision gate)",
            estimated_tokens=estimated,
            has_image=has_image,
            has_tools=has_tools,
        )

    # 2. Long context is a hard window gate. glm-5.3 and step-5-preview both
    #    have 1M windows and are currently idle; K3's only 1M deployment is
    #    saturated. Mechanical long reads belong on the fast chain.
    if estimated >= LONG_CONTEXT_TOKENS:
        return RewriteDecision(
            original=requested,
            rewritten=ALIAS_LONG,
            reason=f"~{estimated} tokens >= {LONG_CONTEXT_TOKENS} -> zk-long (long-context gate)",
            estimated_tokens=estimated,
            has_image=has_image,
            has_tools=has_tools,
        )

    # 3. Bulk tool-driving with a large context is mechanical work: Codex
    #    replaying 30 tool schemas and 50K tokens of history to produce a
    #    300-token ack does not need K3. The threshold is deliberately high
    #    so short, planning-heavy tool rounds still get K3.
    if has_tools and estimated >= BULK_TOOL_TOKENS:
        return RewriteDecision(
            original=requested,
            rewritten=ALIAS_LONG,
            reason=f"{len(request.tools or [])} tools + ~{estimated} tokens -> zk-long (bulk mechanical)",
            estimated_tokens=estimated,
            has_image=has_image,
            has_tools=has_tools,
        )

    # 4. Everything else stays on zk-auto: short questions, code generation,
    #    reasoning, and short tool rounds where K3's quality is worth its price.
    return RewriteDecision(
        original=requested,
        rewritten=requested,
        reason="no bulk/vision/long signal -> zk-auto unchanged",
        estimated_tokens=estimated,
        has_image=has_image,
        has_tools=has_tools,
    )
