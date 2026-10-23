"""Curated model presets: official specs + pre-filled capability/price for common models.

The marketplace lists a provider's *raw* upstream model ids, but a raw id alone is
not enough to configure a gateway model: it needs a context window, max output,
an 8-dimension capability score and a price. This table carries those defaults so
"add a model" is a one-click pick.

**规格数字只写官方文档核实过的**（``spec_source="official"``），出处写在注释里。
没核对到的一律 ``spec_source="estimated"``，表单会标「预估」而不是冒充实测。
``GET /models`` 探测到的数字（上下文等）仍然压过预设——见 ``provider_models``。

Presets are keyed by *family* (bare model name without vendor prefix).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ModelPreset:
    """Pre-filled defaults for one model family, optionally provider-specific.

    **必须按供应商拆**：同一模型在不同供应商的档位/参数/上限可能不一样
    （例：kimi-k3 商汤有 low/medium/high/max，月之暗面官方只有 low/high/max）。
    ``provider=None`` 表示通用预设；带 provider 的条目优先命中。
    """

    family: str
    display_name: str
    context_window: int
    capabilities: dict[str, float] = field(default_factory=dict)
    input_price: float = 0.0  # USD per 1M tokens
    output_price: float = 0.0
    description: str = ""
    #: Vendors that carry this family; used to hint which provider to deploy on.
    providers: tuple[str, ...] = ()
    #: 本条预设限定的供应商 id；``None`` = 通用（任何供应商都能用）。
    provider: str | None = None
    #: 官方默认 max_tokens / 可设上限。``None`` = 官方没写，别猜。
    max_output_tokens: int | None = None
    max_output_tokens_max: int | None = None
    #: ``official`` = 上下文/输出/档位来自官方文档；``estimated`` = 没核对到，经验预估。
    spec_source: str = "estimated"
    #: 推理力度档位（reasoning effort）——**官方文档**给的可选值。
    #: 空 = 还没核对到官方文档，别猜。
    reasoning_efforts: tuple[str, ...] = ()
    #: 官方默认档（如 deepseek-flash 默认 high）。
    reasoning_effort_default: str = ""
    #: 兼容别名 → 原生档（如 deepseek-flash: minimal→low, medium/xhigh→high,
    #: ultra→max）。客户端用 Codex 词表时在这里收口。
    reasoning_effort_aliases: dict[str, str] = field(default_factory=dict)
    #: 官方文档写的工具调用 / 结构化输出支持情况。``None`` = 没核对到，别猜。
    supports_tools: bool | None = None
    supports_structured_output: bool | None = None
    #: 官方文档的参数推荐与使用限制（原文口径，表单只读展示）。
    spec_notes: tuple[str, ...] = ()

    def official_facts(self) -> list[dict[str, str]]:
        """官方规格只读行（表单展示用）。

        只在 ``spec_source == "official"`` 时给出——预估值冒充官方规格是撒谎。
        """
        if self.spec_source != "official":
            return []
        rows: list[dict[str, str]] = []
        if self.context_window:
            rows.append({
                "label": "上下文窗口",
                "value": f"{self.context_window:,} tokens",
                "meaning": "官方文档写的一次请求最大输入长度",
            })
        if self.max_output_tokens is not None:
            limit = f"{self.max_output_tokens:,}"
            if self.max_output_tokens_max is not None:
                limit += f"（最大 {self.max_output_tokens_max:,}）"
            rows.append({
                "label": "最大输出",
                "value": limit,
                "meaning": "官方默认 max_tokens；括号内为可设上限",
            })
        if self.reasoning_efforts:
            rows.append({
                "label": "推理力度",
                "value": " / ".join(self.reasoning_efforts)
                + (f"（默认 {self.reasoning_effort_default}）" if self.reasoning_effort_default else ""),
                "meaning": "官方文档给的可选档位，越高越慢也越准",
            })
        if self.supports_tools is not None:
            rows.append({
                "label": "工具调用",
                "value": "支持" if self.supports_tools else "不支持",
                "meaning": "function calling / 工具调用",
            })
        if self.supports_structured_output is not None:
            rows.append({
                "label": "结构化输出",
                "value": "支持" if self.supports_structured_output else "不支持",
                "meaning": "JSON / Schema 结构化输出",
            })
        for note in self.spec_notes:
            rows.append({"label": "使用须知", "value": note, "meaning": "官方文档原文口径"})
        return rows


#: Hand-tuned presets for the families we know and use.
#: 规格出处标注在每条注释里；**没核实的不写具体数字**。
_PRESETS: tuple[ModelPreset, ...] = (
    # --- 商汤 SenseNova（platform.sensenova.cn/docs，2026-10-23 核对）---------- #
    ModelPreset(
        family="sensenova-6.8-flash-lite",
        display_name="商汤 6.8 Flash Lite",
        # List Models: context_length=262144, max_output_length=65536
        context_window=262_144,
        capabilities={
            "coding": 7.0, "reasoning": 7.0, "tool_use": 8.0, "vision": 9.0,
            "long_context": 7.0, "structured_output": 7.5, "speed": 9.5, "cost": 10.0,
        },
        input_price=0.20, output_price=0.60,
        description="商汤公测免费，多模态，速度快",
        providers=("sensenova",),
        max_output_tokens=65_535,
        max_output_tokens_max=65_536,
        spec_source="official",
        reasoning_efforts=("none", "low", "medium", "high", "max"),
        reasoning_effort_default="high",
        supports_tools=True,
        supports_structured_output=True,
        spec_notes=(
            "max_tokens 范围 [1, 65536]，默认 65535",
            "思考模式默认开启；reasoning_effort=none 或 thinking=disabled 可关",
        ),
    ),
    ModelPreset(
        family="deepseek-flash",
        display_name="DeepSeek V4.1 Flash",
        # 「上下文长度最高支持 1M Tokens」；max_tokens 默认 131072，范围 [1, 393216]
        context_window=1_048_576,
        capabilities={
            "coding": 9.0, "reasoning": 9.0, "tool_use": 9.0, "vision": 9.0,
            "long_context": 10.0, "structured_output": 9.0, "speed": 9.0, "cost": 10.0,
        },
        input_price=0.27, output_price=1.10,
        description="V4.1 Flash（1017 版）：日常主力，公告 id 为 deepseek-flash",
        providers=("sensenova",),
        max_output_tokens=131_072,
        max_output_tokens_max=393_216,
        spec_source="official",
        # 原生 none/low/high/max，默认 high；兼容映射 minimal→low, medium/xhigh→high, ultra→max
        reasoning_efforts=("none", "low", "high", "max"),
        reasoning_effort_default="high",
        reasoning_effort_aliases={
            "minimal": "low", "medium": "high", "xhigh": "high", "ultra": "max",
        },
        supports_tools=True,
        supports_structured_output=True,
        spec_notes=(
            "max_tokens 范围 [1, 393216]，默认 131072；思维链超限会截断思考",
            "Chat 接口 reasoning_effort=\"none\" 可关思考；Messages 接口 output_config.effort=none 关不掉",
            "thinking 不支持 adaptive",
        ),
    ),
    ModelPreset(
        family="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        # 「上下文长度最高支持 1M」；max_tokens 默认 64K（思考）/ 8K（非思考），max 档 128K
        context_window=1_048_576,
        capabilities={
            "coding": 8.5, "reasoning": 8.5, "tool_use": 9.0, "vision": 0.0,
            "long_context": 10.0, "structured_output": 9.0, "speed": 9.0, "cost": 10.0,
        },
        input_price=0.27, output_price=1.10,
        description="V4 Flash（上一代 0731）。别与 V4.1 Flash 混——后者 id 是 deepseek-flash",
        providers=("sensenova", "modelscope", "nvidia"),
        max_output_tokens=65_536,
        max_output_tokens_max=131_072,
        spec_source="official",
        reasoning_efforts=("none", "low", "medium", "high", "max"),
        reasoning_effort_default="high",
        supports_tools=True,
        supports_structured_output=True,
        spec_notes=(
            "max_tokens 默认 64K（思考）/ 8K（非思考），reasoning_effort=max 时 128K",
            "思考模式下 temperature / presence_penalty / frequency_penalty 不生效；top_p 最小 0.95",
            "带 tools 时必须回传历史 reasoning_content，否则工具链路断",
        ),
    ),
    ModelPreset(
        family="deepseek-v4-pro",
        display_name="DeepSeek V4 Pro（2026-10-08 下线）",
        context_window=1_048_576,
        capabilities={
            "coding": 9.0, "reasoning": 9.5, "tool_use": 9.0, "vision": 0.0,
            "long_context": 10.0, "structured_output": 9.0, "speed": 7.0, "cost": 9.0,
        },
        input_price=0.55, output_price=2.20,
        description="旗舰推理档，已下线，过渡期重定向到 deepseek-flash",
        providers=("sensenova",),
        spec_source="official",
    ),
    ModelPreset(
        family="glm-5.2",
        display_name="GLM-5.2",
        # 智谱官方 docs.bigmodel.cn + 商汤文档：上下文 1M；默认 max_tokens 65536，最大 131072
        context_window=1_048_576,
        capabilities={
            "coding": 9.5, "reasoning": 9.0, "tool_use": 8.5, "vision": 0.0,
            "long_context": 10.0, "structured_output": 8.5, "speed": 7.0, "cost": 9.0,
        },
        input_price=1.00, output_price=4.00,
        description="智谱旗舰（长程 Coding 与推理）",
        providers=("sensenova", "nvidia"),
        max_output_tokens=65_536,
        max_output_tokens_max=131_072,
        spec_source="official",
        # 档位最宽，none/minimal/low/medium/high/xhigh/max，默认 max。
        # thinking.type="disabled" 会报错，关思考只能 reasoning_effort="none"。
        reasoning_efforts=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        reasoning_effort_default="max",
        supports_tools=True,
        supports_structured_output=True,
        spec_notes=(
            "默认 max_tokens 65536，最大 131072",
            "thinking.type 不支持 disabled，关思考只能 reasoning_effort=\"none\"",
            "temperature 默认 1.0 / top_p 默认 0.95，建议只调其中一个",
        ),
    ),
    ModelPreset(
        family="kimi-k3",
        display_name="Kimi K3",
        # 1M 上下文；max_completion_tokens 默认 131072，上限 1024*1024 - prompt_tokens
        context_window=1_048_576,
        capabilities={
            "coding": 9.5, "reasoning": 9.5, "tool_use": 9.0, "vision": 9.5,
            "long_context": 10.0, "structured_output": 8.5, "speed": 6.0, "cost": 9.0,
        },
        input_price=2.78, output_price=13.89,
        description="Kimi 旗舰（原生视觉 + 深度推理）· 商汤口径",
        providers=("sensenova",),
        provider="sensenova",
        max_output_tokens=131_072,
        max_output_tokens_max=1_048_576,
        spec_source="official",
        # 商汤文档：low/medium/high/max，默认 max。
        reasoning_efforts=("low", "medium", "high", "max"),
        reasoning_effort_default="max",
        supports_tools=True,
        supports_structured_output=True,
        spec_notes=(
            "max_completion_tokens 默认 131072，上限 1024*1024 - prompt_tokens（不是 max_tokens）",
            "temperature 固定 1、top_p 固定 0.95、presence/frequency_penalty 固定 0，传其他值会报错",
            "图片只收 Base64（Data URL），不支持公网 URL",
        ),
    ),
    # 月之暗面官方（platform.kimi.com/models-overview）：kimi-k3 **没有 medium**，
    # 只有 low/high/max。跟商汤词表不一样——所以按供应商拆，别混用。
    ModelPreset(
        family="kimi-k3",
        display_name="Kimi K3",
        context_window=1_048_576,
        capabilities={
            "coding": 9.5, "reasoning": 9.5, "tool_use": 9.0, "vision": 9.5,
            "long_context": 10.0, "structured_output": 8.5, "speed": 6.0, "cost": 9.0,
        },
        description="Kimi 旗舰 · 月之暗面官方口径",
        providers=("moonshot",),
        provider="moonshot",
        max_output_tokens=131_072,
        max_output_tokens_max=1_048_576,
        spec_source="official",
        reasoning_efforts=("low", "high", "max"),
        reasoning_effort_default="max",
        supports_tools=True,
        supports_structured_output=True,
        spec_notes=(
            "max_completion_tokens 默认 131072，最大 1048576",
            "temperature 固定 1.0 / top_p 固定 0.95 / n 固定 1，均不可改",
            "thinking 可省略；显式设置时仅接受 {\"type\":\"enabled\",\"keep\":\"all\"}",
        ),
    ),
    ModelPreset(
        family="kimi-k2.6",
        display_name="Kimi K2.6",
        # platform.kimi.com：上下文 256K；max 输出 256*1024 - prompt_tokens
        context_window=262_144,
        capabilities={
            "coding": 8.5, "reasoning": 8.5, "tool_use": 8.5, "vision": 9.0,
            "long_context": 8.0, "structured_output": 8.0, "speed": 8.0, "cost": 9.0,
        },
        input_price=1.40, output_price=5.60,
        description="Kimi 上一代旗舰",
        providers=("nvidia", "moonshot"),
        max_output_tokens_max=262_144,
        spec_source="official",
    ),
    # --- 阶跃 StepFun（platform.stepfun.com，2026-10-23 核对）----------------- #
    # step-5-preview：上下文 1M；最大输出 **64k**（API 文档口径；模型卡写 1M 不一致，
    # 按 API 文档）；reasoning_effort 仅 low/medium/high；max_tokens 默认 INF。
    ModelPreset(
        family="step-5-preview",
        display_name="Step 5 Preview",
        context_window=1_000_000,
        capabilities={
            "coding": 8.5, "reasoning": 8.5, "tool_use": 8.0, "vision": 10.0,
            "long_context": 10.0, "structured_output": 8.0, "speed": 5.5, "cost": 7.0,
        },
        input_price=1.00, output_price=2.70,
        description="阶跃旗舰预览版：1M 上下文 + 图像/视频 + 思考模式",
        providers=("stepfun",),
        max_output_tokens=65_536,
        max_output_tokens_max=65_536,
        spec_source="official",
        reasoning_efforts=("low", "medium", "high"),
        reasoning_effort_default="",
        supports_tools=True,
        supports_structured_output=True,
        spec_notes=(
            "max_tokens 默认 INF（不设就没上界，直到 64k 顶）——务必显式设预算",
            "reasoning_effort 只有 low/medium/high；Chat 用 reasoning_effort，"
            "Messages 用 output_config.effort，传错名会被静默忽略",
            "输出计费含思考过程",
        ),
    ),
    ModelPreset(
        family="step-3.7-flash",
        display_name="Step 3.7 Flash",
        # StepFun GET /models + 官方文档：256K 上下文
        context_window=262_144,
        capabilities={
            "coding": 8.0, "reasoning": 8.0, "tool_use": 8.0, "vision": 10.0,
            "long_context": 8.0, "structured_output": 8.0, "speed": 9.0, "cost": 9.0,
        },
        description="阶跃速度杯：256K 上下文 + 图像输入 + 思考模式",
        providers=("stepfun",),
        spec_source="official",
    ),
    ModelPreset(
        family="step-3.5-flash",
        display_name="Step 3.5 Flash",
        context_window=262_144,
        capabilities={
            "coding": 7.5, "reasoning": 7.5, "tool_use": 7.5, "vision": 0.0,
            "long_context": 8.0, "structured_output": 7.5, "speed": 9.0, "cost": 9.0,
        },
        description="阶跃上一代速度杯：256K 上下文，纯文本",
        providers=("stepfun",),
        spec_source="official",
    ),
    # --- 通义 Qwen（qwen.ai 官方博客 / ModelScope）--------------------------- #
    # 原生 262144，YaRN 可扩到 1M。推荐输出：推理 262144 + 正文 131072（1M 内）。
    ModelPreset(
        family="qwen3.8-flash-next",
        display_name="Qwen3.8 Flash Next",
        context_window=262_144,
        capabilities={
            "coding": 8.0, "reasoning": 8.0, "tool_use": 8.5, "vision": 0.0,
            "long_context": 8.0, "structured_output": 8.5, "speed": 9.0, "cost": 9.5,
        },
        input_price=0.40, output_price=1.20,
        description="通义新一代轻量档（原生 256K，可 YaRN 扩 1M）",
        providers=("modelscope",),
        max_output_tokens=131_072,
        max_output_tokens_max=262_144,
        spec_source="official",
    ),
    ModelPreset(
        family="qwen38-flash-next",
        display_name="Qwen3.8 Flash Next",
        context_window=262_144,
        capabilities={
            "coding": 7.5, "reasoning": 7.5, "tool_use": 8.0, "vision": 0.0,
            "long_context": 8.0, "structured_output": 8.0, "speed": 9.0, "cost": 9.5,
        },
        input_price=0.40, output_price=1.20,
        description="魔搭社区每日免费额度，国内直连",
        providers=("modelscope",),
        max_output_tokens=131_072,
        max_output_tokens_max=262_144,
        spec_source="official",
    ),
    # --- NVIDIA / 智谱旧档（来源混杂，标 estimated）-------------------------- #
    ModelPreset(
        family="glm-5.3",
        display_name="GLM-5.3",
        # NVIDIA 上新，官方公开页未给精确 max_tokens，上下文按同系 GLM-5.2 的 1M
        context_window=1_000_000,
        capabilities={
            "coding": 9.5, "reasoning": 9.0, "tool_use": 8.5, "vision": 0.0,
            "long_context": 10.0, "structured_output": 8.5, "speed": 6.5, "cost": 9.5,
        },
        input_price=1.00, output_price=4.00,
        description="智谱旗舰（NVIDIA 上新）",
        providers=("nvidia",),
        spec_source="estimated",
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
        spec_source="estimated",
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
        spec_source="estimated",
    ),
    # NVIDIA 官方博客称 native 1M 上下文；max output 官方未写死，不填。
    ModelPreset(
        family="nemotron-3-super",
        display_name="Nemotron 3 Super",
        context_window=1_000_000,
        capabilities={
            "coding": 7.0, "reasoning": 7.5, "tool_use": 7.5, "vision": 0.0,
            "long_context": 8.0, "structured_output": 7.0, "speed": 7.0, "cost": 8.0,
        },
        input_price=0.50, output_price=1.50,
        description="NVIDIA 免费额度兜底（官方称 native 1M 上下文）",
        providers=("nvidia",),
        spec_source="estimated",
    ),
)

#: Exact match first, then longest-suffix match on the family name.
#: 同一 family 可以有多条（通用 + 各供应商）；匹配时供应商专属优先。
_BY_FAMILY: dict[str, list[ModelPreset]] = {}
for _p in _PRESETS:
    _BY_FAMILY.setdefault(_p.family, []).append(_p)


#: 思考强度档位从弱到强。``none`` 是「关思考」不是「最弱思考」，单独放最前。
EFFORT_RANK: dict[str, int] = {
    "none": 0,
    "minimal": 1, "极简": 1,
    "low": 2, "低": 2,
    "medium": 3, "中": 3,
    "high": 4, "高": 4,
    "xhigh": 5, "ultra": 5, "超高": 5,
    "max": 6, "extreme": 6, "极致": 6,
}


def highest_effort(efforts: tuple[str, ...] | list[str]) -> str:
    """从可选档位里挑思考强度最高的一档（运营者默认上最高档）。

    ``none``（关思考）永远不被当成「最高」——它没有思考强度。
    """
    candidates = [e for e in efforts if e and e.lower() != "none"]
    if not candidates:
        return ""
    return max(candidates, key=lambda e: EFFORT_RANK.get(e.lower(), -1))


def _normalize(model_id: str) -> str:
    """Strip a vendor prefix and lower-case: ``z-ai/glm-5.3`` -> ``glm-5.3``."""
    return model_id.split("/")[-1].strip().lower()


def match_preset(
    upstream_id: str, provider_id: str | None = None,
) -> ModelPreset | None:
    """Resolve an upstream model id to a preset.

    **供应商专属条目优先**：``preset_for("kimi-k3", "moonshot")`` 拿月之暗面词表
    （low/high/max），``preset_for("kimi-k3", "sensenova")`` 拿商汤词表
    （low/medium/high/max）。没传 provider 时用通用条目，都没有再退最长后缀。
    """
    normalized = _normalize(upstream_id)
    entries = _BY_FAMILY.get(normalized)
    if entries:
        if provider_id:
            for item in entries:
                if item.provider == provider_id:
                    return item
        for item in entries:
            if item.provider is None:
                return item
        return entries[0]
    # Longest-suffix wins so "deepseek-v4-flash-0731" -> deepseek-v4-flash, not "v4".
    best: ModelPreset | None = None
    for family, items in _BY_FAMILY.items():
        if not (
            normalized.endswith(family) or normalized.startswith(family)
        ):
            continue
        if best is not None and len(family) <= len(best.family):
            continue
        # 优先选匹配供应商的，其次通用
        pick = None
        if provider_id:
            pick = next((i for i in items if i.provider == provider_id), None)
        pick = pick or next((i for i in items if i.provider is None), None) or items[0]
        best = pick
    return best


def guess_from_name(upstream_id: str) -> ModelPreset:
    """Heuristic defaults for a model with no preset (name-pattern based).

    刻意保守：上下文一律按 256K 估（不冒充 1M），max_output 不填——
    猜出来的数字配错了比留空更糟。``spec_source="estimated"`` 让表单标「预估」。
    """
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
        # 未收录：保守 256K，不按名字猜 1M
        context_window=256_000,
        capabilities={
            "coding": coder, "reasoning": reasoning, "tool_use": 7.5, "vision": vision,
            "long_context": long_ctx, "structured_output": 7.5, "speed": speed, "cost": cost,
        },
        input_price=0.0, output_price=0.0,
        description="未收录模型（按名字推断，规格未核对）",
        providers=(),
        spec_source="estimated",
    )


def preset_for(upstream_id: str, provider_id: str | None = None) -> ModelPreset:
    """Preset if known, else a name-pattern guess. 供应商专属优先。"""
    return match_preset(upstream_id, provider_id) or guess_from_name(upstream_id)


__all__ = [
    "EFFORT_RANK",
    "ModelPreset",
    "guess_from_name",
    "highest_effort",
    "match_preset",
    "preset_for",
]
