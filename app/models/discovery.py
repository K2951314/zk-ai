"""What a provider tells us about its own models - and how much to trust it.

Adding a model used to mean trusting a hand-written preset table for its context
window, whether it accepts images and whether it thinks. Providers that expose a
rich ``GET /models`` (StepFun does: ``max_input_tokens``, ``enable_vision_input``,
``enable_reason``, ``reasoning_effort_support_list`` …) already answered all of
that - the gateway just threw the fields away.

This module normalises those answers into one small :class:`ModelFacts` so the
marketplace can fill the console form from *measured* data. Two rules:

* a field the upstream did not report stays ``None`` - never invented, so the
  caller can tell "measured" from "guessed" and only show what is really known;
* the flat list of supported parameters is a *tunable* contract: the console
  shows a knob only when the provider says the model accepts it.

Field names vary per vendor, hence the alias tables; every alias seen in the wild
is listed, and an unknown vendor simply yields fewer facts (presets still fill the
rest, as before).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: How each vendor spells "how much can I feed this model".
_CONTEXT_KEYS = (
    "max_input_tokens",      # StepFun
    "context_length",        # vLLM / most OpenAI-compatible servers
    "context_window",
    "max_context_length",
    "input_token_limit",
    "max_tokens",            # some gateways misuse this for the input limit
)
#: "Does it accept images / vision input".
_VISION_KEYS = ("enable_vision_input", "supports_vision", "vision", "accepts_images")
#: "Does it emit a reasoning / thinking pass".
_REASON_KEYS = ("enable_reason", "supports_reasoning", "reasoning", "can_reason")
#: "Which reasoning-effort levels can be asked for".
_EFFORT_KEYS = ("reasoning_effort_support_list", "supported_reasoning_efforts",
                "reasoning_efforts")
#: "Which wire protocols the endpoint speaks" (chat / messages / responses).
_PROTOCOL_KEYS = ("supported_protocols", "protocols")
#: "Which request parameters the model accepts" - the tunable-knob contract.
_PARAMETER_KEYS = ("supported_parameters", "supported_params", "parameters")

#: Gateway capability dimension that a vision fact decides.
_VISION_DIMENSION = "vision"


@dataclass(slots=True)
class ModelFacts:
    """What the upstream said about one model. ``None`` = it did not say."""

    context_window: int | None = None
    vision_input: bool | None = None
    reasoning: bool | None = None
    reasoning_effort: tuple[str, ...] = field(default_factory=tuple)
    protocols: tuple[str, ...] = field(default_factory=tuple)
    supported_parameters: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "context_window": self.context_window,
            "vision_input": self.vision_input,
            "reasoning": self.reasoning,
        }
        if self.reasoning_effort:
            out["reasoning_effort"] = list(self.reasoning_effort)
        if self.protocols:
            out["protocols"] = list(self.protocols)
        if self.supported_parameters:
            out["supported_parameters"] = list(self.supported_parameters)
        return out

    @property
    def known(self) -> bool:
        """True when the upstream told us anything worth acting on."""
        return bool(
            self.context_window is not None
            or self.vision_input is not None
            or self.reasoning is not None
            or self.reasoning_effort
            or self.supported_parameters
        )

    def describe(self) -> list[dict[str, str]]:
        """Plain-language rows for the console: what is true, and what it means.

        Each row is a fact an operator can act on, phrased as a consequence rather
        than a field name, so the form can show "可调 / 不可调" without the reader
        needing to know the upstream's vocabulary.
        """
        rows: list[dict[str, str]] = []
        if self.context_window is not None:
            rows.append({
                "label": "上下文窗口",
                "value": f"{self.context_window:,} tokens",
                "meaning": "一次请求能带进去的最大内容长度（提问 + 历史 + 附件折算），超了会被上游拒",
            })
        if self.vision_input is not None:
            rows.append({
                "label": "图像输入",
                "value": "支持" if self.vision_input else "不支持",
                "meaning": "能不能把图片一起发给它；不支持时发图会被拒绝，此项已固定 0 分",
            })
        if self.reasoning is not None:
            rows.append({
                "label": "思考模式",
                "value": "支持" if self.reasoning else "不支持",
                "meaning": "回答前是否先内部推理；支持时可要求它输出思考过程",
            })
        if self.reasoning_effort:
            rows.append({
                "label": "思考强度",
                "value": " / ".join(self.reasoning_effort),
                "meaning": "可选的推理投入档位，越高越慢也越准；不支持时该参数不出现，用默认",
            })
        if self.protocols:
            rows.append({
                "label": "接口协议",
                "value": " / ".join(self.protocols),
                "meaning": "该模型在上游支持的调用方式；本网关已按其中之一接入",
            })
        if self.supported_parameters:
            rows.append({
                "label": "可调参数",
                "value": "、".join(self.supported_parameters),
                "meaning": "只有这些请求参数该模型真正接受；表单只显示这些，其余一律默认",
            })
        return rows


def _first_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit() and int(value) > 0:
            return int(value)
    return None


def _first_bool(payload: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        if key in payload:
            value = payload[key]
            if isinstance(value, bool):
                return value
    # Some gateways spell modality support as {"input_modalities": ["text","image"]}.
    modalities = payload.get("input_modalities") or payload.get("modalities")
    if isinstance(modalities, list):
        return any(str(m).lower() in {"image", "vision", "image_url"} for m in modalities)
    return None


def _first_strs(payload: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, ...]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list) and value:
            return tuple(str(item) for item in value if str(item).strip())
    return ()


def model_facts(payload: dict[str, Any] | None) -> ModelFacts:
    """Extract :class:`ModelFacts` from one entry of a provider's ``/models``."""
    if not isinstance(payload, dict):
        return ModelFacts()
    facts = ModelFacts(
        context_window=_first_int(payload, _CONTEXT_KEYS),
        vision_input=_first_bool(payload, _VISION_KEYS),
        reasoning=_first_bool(payload, _REASON_KEYS),
        reasoning_effort=_first_strs(payload, _EFFORT_KEYS),
        protocols=_first_strs(payload, _PROTOCOL_KEYS),
        supported_parameters=_first_strs(payload, _PARAMETER_KEYS),
    )
    return facts


#: Gateway capability dimensions a vision fact overrides, and the value it implies.
CAPABILITY_FROM_FACTS = {_VISION_DIMENSION: True}


__all__ = ["ModelFacts", "model_facts"]
