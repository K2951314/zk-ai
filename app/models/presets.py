"""Curated model presets: pre-filled context / capability / price for common models.

The marketplace (``/admin/providers/{id}/models`` + the models console page) lists a
provider's *raw* upstream model ids, but a raw id alone is not enough to configure a
gateway model: it needs a context window, an 8-dimension capability score and a price
for cost estimation. This table carries those defaults so "add a model" is a one-click
pick instead of a form-filling chore.

Presets are keyed by *family* (the bare model name without a vendor prefix, e.g.
``glm-5.3``). ``match_preset`` resolves an upstream id like ``z-ai/glm-5.3-flash`` or
``deepseek-ai/DeepSeek-V4-Flash-0731`` to the right family. Unknown ids fall back to
name-pattern heuristics so even an unlisted model lands on sensible defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ModelPreset:
    """Pre-filled defaults for one model family."""

    family: str
    display_name: str
    context_window: int
    capabilities: dict[str, float] = field(default_factory=dict)
    input_price: float = 0.0  # USD per 1M tokens
    output_price: float = 0.0
    description: str = ""
    #: Vendors that carry this family; used to hint which provider to deploy on.
    providers: tuple[str, ...] = ()


#: Hand-tuned presets for the families we know and use. Add rows as new models appear.
_PRESETS: tuple[ModelPreset, ...] = (
    ModelPreset(
        family="glm-5.3",
        display_name="GLM-5.3",
        context_window=1_000_000,
        capabilities={
            "coding": 9.5, "reasoning": 9.0, "tool_use": 8.5, "vision": 0.0,
            "long_context": 10.0, "structured_output": 8.5, "speed": 6.5, "cost": 9.5,
        },
        input_price=1.00, output_price=4.00,
        description="智谱旗舰（NVIDIA 上新）",
        providers=("nvidia",),
    ),
    ModelPreset(
        family="glm-5.3-flash",
        display_name="GLM-5.3 Flash",
        context_window=256_000,
        capabilities={
            "coding": 8.5, "reasoning": 8.5, "tool_use": 8.5, "vision": 0.0,
            "long_context": 8.0, "structured_output": 8.5, "speed": 9.0, "cost": 10.0,
        },
        input_price=0.30, output_price=1.20,
        description="GLM-5.3 的速度杯",
        providers=("nvidia",),
    ),
    ModelPreset(
        family="glm-5.2",
        display_name="GLM-5.2",
        context_window=1_000_000,
        capabilities={
            "coding": 9.5, "reasoning": 9.0, "tool_use": 8.5, "vision": 0.0,
            "long_context": 10.0, "structured_output": 8.5, "speed": 7.0, "cost": 9.0,
        },
        input_price=1.00, output_price=4.00,
        description="智谱旗舰（长程 Coding 与推理）",
        providers=("sensenova", "nvidia"),
    ),
    ModelPreset(
        family="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        context_window=1_000_000,
        capabilities={
            "coding": 8.5, "reasoning": 8.5, "tool_use": 9.0, "vision": 0.0,
            "long_context": 10.0, "structured_output": 9.0, "speed": 9.0, "cost": 10.0,
        },
        input_price=0.27, output_price=1.10,
        description="日常主力，1M 上下文，便宜快",
        providers=("sensenova", "modelscope", "nvidia"),
    ),
    ModelPreset(
        family="kimi-k3",
        display_name="Kimi K3",
        context_window=1_000_000,
        capabilities={
            "coding": 9.5, "reasoning": 9.5, "tool_use": 9.0, "vision": 9.5,
            "long_context": 10.0, "structured_output": 8.5, "speed": 6.0, "cost": 9.0,
        },
        input_price=2.78, output_price=13.89,
        description="Kimi 旗舰（原生视觉 + 深度推理）",
        providers=("sensenova", "moonshot", "nvidia"),
    ),
    ModelPreset(
        family="kimi-k2.6",
        display_name="Kimi K2.6",
        context_window=256_000,
        capabilities={
            "coding": 8.5, "reasoning": 8.5, "tool_use": 8.5, "vision": 9.0,
            "long_context": 8.0, "structured_output": 8.0, "speed": 8.0, "cost": 9.0,
        },
        input_price=1.40, output_price=5.60,
        description="Kimi 上一代旗舰",
        providers=("nvidia", "moonshot"),
    ),
    ModelPreset(
        family="sensenova-6.8-flash-lite",
        display_name="商汤 6.8 Flash Lite",
        context_window=256_000,
        capabilities={
            "coding": 7.0, "reasoning": 7.0, "tool_use": 8.0, "vision": 9.0,
            "long_context": 7.0, "structured_output": 7.5, "speed": 9.5, "cost": 10.0,
        },
        input_price=0.20, output_price=0.60,
        description="商汤公测免费，多模态，速度快",
        providers=("sensenova",),
    ),
    ModelPreset(
        family="sensenova-6.7-flash-lite",
        display_name="商汤 6.7 Flash Lite",
        context_window=256_000,
        capabilities={
            "coding": 6.5, "reasoning": 6.5, "tool_use": 7.5, "vision": 9.0,
            "long_context": 7.0, "structured_output": 7.0, "speed": 9.0, "cost": 10.0,
        },
        input_price=0.15, output_price=0.50,
        description="商汤上一代 Flash Lite",
        providers=("sensenova",),
    ),
    ModelPreset(
        family="qwen38-flash-next",
        display_name="Qwen3.8 Flash Next",
        context_window=256_000,
        capabilities={
            "coding": 7.5, "reasoning": 7.5, "tool_use": 8.0, "vision": 0.0,
            "long_context": 8.0, "structured_output": 8.0, "speed": 9.0, "cost": 9.5,
        },
        input_price=0.40, output_price=1.20,
        description="魔搭社区每日免费额度，国内直连",
        providers=("modelscope",),
    ),
    ModelPreset(
        family="nemotron-3-super",
        display_name="Nemotron 3 Super",
        context_window=128_000,
        capabilities={
            "coding": 7.0, "reasoning": 7.5, "tool_use": 7.5, "vision": 0.0,
            "long_context": 5.0, "structured_output": 7.0, "speed": 7.0, "cost": 8.0,
        },
        input_price=0.50, output_price=1.50,
        description="NVIDIA 免费额度兜底",
        providers=("nvidia",),
    ),
)

#: Exact match first, then longest-suffix match on the family name.
_BY_FAMILY: dict[str, ModelPreset] = {preset.family: preset for preset in _PRESETS}


def _normalize(model_id: str) -> str:
    """Strip a vendor prefix and lower-case: ``z-ai/glm-5.3`` -> ``glm-5.3``."""
    return model_id.split("/")[-1].strip().lower()


def match_preset(upstream_id: str) -> ModelPreset | None:
    """Resolve an upstream model id to a preset, or None when unknown."""
    normalized = _normalize(upstream_id)
    if normalized in _BY_FAMILY:
        return _BY_FAMILY[normalized]
    # Longest-suffix wins so "deepseek-v4-flash-0731" -> deepseek-v4-flash, not "v4".
    best: ModelPreset | None = None
    for family, preset in _BY_FAMILY.items():
        if (
            normalized.endswith(family) or normalized.startswith(family)
        ) and (best is None or len(family) > len(best.family)):
            best = preset
    return best


def guess_from_name(upstream_id: str) -> ModelPreset:
    """Heuristic defaults for a model with no preset (name-pattern based)."""
    name = _normalize(upstream_id)
    vision = 5.0 if any(t in name for t in ("vision", "vl", "vlm", "image", "pic", "omni")) else 0.0
    coder = 8.5 if "coder" in name or "code" in name else 7.0
    reasoning = 9.0 if any(t in name for t in ("reason", "r1", "think", "o1", "deepseek-r")) else 7.0
    flash = any(t in name for t in ("flash", "lite", "mini", "small", "nano", "fast", "turbo"))
    speed = 9.0 if flash else 6.0
    cost = 9.5 if flash else 7.0
    long_ctx = 8.0 if any(t in name for t in ("long", "128k", "256k", "1m", "1000k")) else 5.0
    return ModelPreset(
        family=name,
        display_name=name,
        context_window=256_000 if flash else 1_000_000,
        capabilities={
            "coding": coder, "reasoning": reasoning, "tool_use": 7.5, "vision": vision,
            "long_context": long_ctx, "structured_output": 7.5, "speed": speed, "cost": cost,
        },
        input_price=0.0, output_price=0.0,
        description="未收录模型（按名字推断）",
        providers=(),
    )


def preset_for(upstream_id: str) -> ModelPreset:
    """Preset if known, else a name-pattern guess."""
    return match_preset(upstream_id) or guess_from_name(upstream_id)


__all__ = [
    "ModelPreset",
    "guess_from_name",
    "match_preset",
    "preset_for",
]
