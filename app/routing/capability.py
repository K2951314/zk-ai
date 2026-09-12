"""Capability Router: infer what a request needs, then score candidates.

The algorithm is deliberately **explainable**: every candidate gets a weighted
score over the eight capability dimensions and the router records the winning
score, the runner-up and the reason string. Swapping the algorithm later only
requires replacing :func:`score_candidate` / :func:`order_candidates`; the API and
scheduler contracts stay identical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.models.provider import (
    CAPABILITY_NAMES,
    CapabilityScores,
    DeploymentConfig,
    ModelAliasConfig,
    ModelConfig,
)
from app.models.request import ChatCompletionRequest

#: Above this estimated input size a request is considered "long context".
LONG_CONTEXT_HINT = 24_000
#: Above this it becomes a hard requirement on ``context_window``.
LONG_CONTEXT_HARD = 100_000
#: Weight given to a dimension that carries a hard gate. A "must have" capability
#: has to dominate the ranking, otherwise two eligible candidates would be ordered
#: by unrelated dimensions and the gate would only filter without steering.
GATE_WEIGHT = 8.0

_CODE_HINTS = re.compile(
    r"```|\bdef\s+\w+|\bclass\s+\w+|\bimport\s+\w+|\bfunction\s+\w+|\bconst\s+\w+"
    r"|\bSELECT\b.*\bFROM\b|\bTraceback\b|\bnull\b|\bundefined\b|\brefactor\b|\bcompile\b"
    r"|\bpytest\b|\bgit\b|\bregex\b|\bAPI\b|\bSQL\b",
    re.IGNORECASE,
)
_REASONING_HINTS = re.compile(
    r"\bstep[- ]by[- ]step\b|\bprove\b|\bderive\b|\bwhy\b|\bexplain\b|\banaly[sz]e\b"
    r"|\barchitecture\b|\btrade[- ]?offs?\b|\broot cause\b|\bmath\b|\bproof\b"
    r"|为什么|推理|证明|分析|原理|架构|权衡|根因",
    re.IGNORECASE,
)
_VISION_HINTS = re.compile(r"\bimage\b|\bscreenshot\b|\bdiagram\b|图片|截图|看图", re.IGNORECASE)


@dataclass(slots=True)
class CapabilityRequirement:
    """What the request needs, expressed as weights + hard gates."""

    #: Dimension -> relative importance (0 disables the dimension).
    weights: dict[str, float] = field(default_factory=dict)
    #: Dimension -> minimum score required (hard gate).
    minimums: dict[str, float] = field(default_factory=dict)
    #: Estimated input tokens (context fitting).
    estimated_tokens: int = 0
    #: Human readable explanation, surfaced in ``routing_reason``.
    notes: list[str] = field(default_factory=list)

    def weight(self, name: str) -> float:
        return float(self.weights.get(name, 0.0))

    def normalized_weights(self) -> dict[str, float]:
        total = sum(max(0.0, v) for v in self.weights.values())
        if total <= 0:
            return {name: 1.0 / len(CAPABILITY_NAMES) for name in CAPABILITY_NAMES}
        return {name: max(0.0, self.weights.get(name, 0.0)) / total for name in CAPABILITY_NAMES}

    def describe(self) -> str:
        return "; ".join(self.notes) if self.notes else "no explicit capability hints"


def infer_requirement(
    request: ChatCompletionRequest,
    *,
    alias: ModelAliasConfig | None = None,
    model: ModelConfig | None = None,
) -> CapabilityRequirement:
    """Derive the capability requirement from the request body.

    Three sources are merged, in increasing priority:

    1. **Inference** from the request (keywords, images, tools, response_format,
       estimated input size).
    2. **Alias ``requires``** - additional hard gates.
    3. **Alias ``weights``** - explicit weight overrides, applied last so they win.

    Every dimension that ends up in ``minimums`` is also given at least
    :data:`GATE_WEIGHT` in ``weights``: a gate filters candidates, the weight then
    steers the order of the survivors.
    """
    weights: dict[str, float] = dict.fromkeys(CAPABILITY_NAMES, 0.0)
    # Baseline: quality dimensions matter, cost/speed are tie-breakers.
    weights.update(
        {
            "coding": 1.0,
            "reasoning": 1.0,
            "tool_use": 0.2,
            "structured_output": 0.2,
            "long_context": 0.3,
            "vision": 0.0,
            "speed": 0.4,
            "cost": 0.4,
        }
    )
    minimums: dict[str, float] = {}
    notes: list[str] = []

    user_text_parts: list[str] = []
    for message in request.messages:
        if message.role in {"user", "system", "developer"}:
            user_text_parts.append(message.text())
        if message.has_image():
            minimums["vision"] = 1.0
            if "vision" not in notes:
                notes.append("request contains an image -> vision required")

    combined = "\n".join(user_text_parts)

    # The three "modal" requirements below become *hard gates*; their scoring
    # weight is derived from the gate itself further down.
    if request.tools:
        minimums.setdefault("tool_use", 1.0)
        notes.append(f"{len(request.tools)} tool(s) declared -> tool_use required")

    if request.response_format:
        minimums.setdefault("structured_output", 1.0)
        notes.append("response_format set -> structured_output required")

    if _CODE_HINTS.search(combined):
        weights["coding"] = 5.0
        notes.append("code-like content detected -> coding weight raised")

    if _REASONING_HINTS.search(combined):
        weights["reasoning"] = 5.0
        notes.append("reasoning keywords detected -> reasoning weight raised")

    estimated = request.estimated_input_tokens()
    if estimated >= LONG_CONTEXT_HINT:
        weights["long_context"] = 5.0
        notes.append(f"~{estimated} input tokens -> long_context weight raised")
    if estimated >= LONG_CONTEXT_HARD:
        minimums["long_context"] = 1.0

    requirement = CapabilityRequirement(
        weights=weights,
        minimums=minimums,
        estimated_tokens=estimated,
        notes=notes,
    )

    # Alias level overrides win over inference.
    if alias is not None:
        for name, value in alias.requires.items():
            requirement.minimums[name] = float(value)

    # A hard gate must also dominate the score (see ``GATE_WEIGHT``).
    for name in requirement.minimums:
        requirement.weights[name] = max(requirement.weights.get(name, 0.0), GATE_WEIGHT)

    if alias is not None:
        # Explicit alias weights are applied last so they always win.
        for name, value in alias.weights.items():
            if name in requirement.weights:
                requirement.weights[name] = float(value)
        if alias.weights or alias.requires:
            requirement.notes.append(
                f"alias '{alias.name}' overrides weights/gates (strategy={alias.strategy.value})"
            )

    if model is not None and estimated > model.context_window:
        requirement.notes.append(
            f"estimated {estimated} tokens exceeds model context {model.context_window}"
        )
    return requirement


def context_fits(
    deployment: DeploymentConfig, model: ModelConfig, requirement: CapabilityRequirement
) -> bool:
    """Hard gate: does the deployment's context window cover the request?"""
    window = deployment.context_window or model.context_window
    if requirement.estimated_tokens <= 0:
        return True
    # Allow 10% slack for tokeniser differences.
    return window >= requirement.estimated_tokens * 0.9


@dataclass(slots=True)
class ScoreCard:
    """Explainable score for one candidate."""

    total: float
    breakdown: dict[str, float]
    gates_failed: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def eligible(self) -> bool:
        return not self.gates_failed

    def describe(self, top: int = 3) -> str:
        if not self.eligible:
            return f"gated out: {', '.join(self.gates_failed)}"
        items = sorted(self.breakdown.items(), key=lambda kv: kv[1], reverse=True)[:top]
        parts = ", ".join(f"{name}={value:.2f}" for name, value in items if value > 0)
        return f"score={self.total:.3f} ({parts})"


def score_candidate(
    *,
    model: ModelConfig,
    deployment: DeploymentConfig,
    requirement: CapabilityRequirement,
) -> ScoreCard:
    """Weighted-sum score over capability dimensions, plus hard gates."""
    capabilities: CapabilityScores = model.effective_capabilities(deployment)
    scores = capabilities.as_dict()
    weights = requirement.normalized_weights()

    gates_failed: list[str] = []
    for name, minimum in requirement.minimums.items():
        if scores.get(name, 0.0) < minimum:
            gates_failed.append(f"{name}>={minimum:g} not met ({scores.get(name, 0.0):g})")
    if not context_fits(deployment, model, requirement):
        gates_failed.append(f"context window {deployment.context_window} too small")

    if not deployment.enabled:
        gates_failed.append("deployment disabled")

    total = 0.0
    breakdown: dict[str, float] = {}
    for name in CAPABILITY_NAMES:
        contribution = weights.get(name, 0.0) * (scores.get(name, 0.0) / 10.0)
        breakdown[name] = contribution
        total += contribution

    # Deployment priority acts as a small, explicit tie-breaker.
    priority_bonus = min(0.05, max(0.0, deployment.priority) / 2000.0)
    total += priority_bonus
    breakdown["priority"] = priority_bonus

    return ScoreCard(
        total=round(total, 6),
        breakdown=breakdown,
        gates_failed=gates_failed,
        note="weighted capability sum",
    )


def explain_scores(cards: list[tuple[str, ScoreCard]], *, limit: int = 5) -> dict[str, Any]:
    """Compact explanation payload for ``/admin/router/preview``."""
    ranked = sorted(cards, key=lambda item: item[1].total, reverse=True)[:limit]
    return {
        str(name): {
            "score": card.total,
            "eligible": card.eligible,
            "breakdown": {k: round(v, 4) for k, v in card.breakdown.items() if v},
            "gates_failed": card.gates_failed,
        }
        for name, card in ranked
    }
