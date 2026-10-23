"""推理等级（reasoning effort）配置与下发。

这是模型自己支持的思考投入档位（低/中/高/超高/极致这类），**与积分、配额无关**。
两条不变量：

1. 优先级是「客户端 > 部署默认 > 不指定」——网关默认绝不能盖掉客户端显式
   传来的 ``reasoning_effort``（Codex 的 ``reasoning.effort`` 走同一条 extra 通道）。
2. 档位证据（``reasoning_efforts``）与运营者选择（``reasoning_effort``）分字段：
   前者来自供应商元数据，不猜；后者才是可改的配置。
"""

from __future__ import annotations

from app.models.provider import DeploymentConfig, ModelConfig, ProviderConfig
from app.models.request import ChatCompletionRequest, ChatMessage
from app.providers.base import OpenAICompatibleAdapter


def _adapter() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        ProviderConfig(
            id="stepfun",
            type="openai_compatible",
            base_url="https://example.invalid/v1",
        )
    )


def _deployment(**kwargs: object) -> DeploymentConfig:
    base: dict = {
        "id": "step-5-preview-stepfun",
        "provider_id": "stepfun",
        "model": "step-5-preview",
        "reasoning_efforts": ["low", "medium", "high"],
    }
    base.update(kwargs)
    return DeploymentConfig(**base)  # type: ignore[arg-type]


def _request(**extra: object) -> ChatCompletionRequest:
    payload: dict = {
        "model": "step-5-preview",
        "messages": [ChatMessage(role="user", content="hello")],
    }
    payload.update(extra)
    return ChatCompletionRequest(**payload)  # type: ignore[arg-type]


class TestDeploymentFields:
    def test_defaults_are_unset(self) -> None:
        d = DeploymentConfig(id="d", provider_id="p", model="m")
        assert d.reasoning_effort is None
        assert d.reasoning_efforts is None

    def test_accepts_levels_and_chosen_default(self) -> None:
        d = _deployment(reasoning_effort="medium")
        assert d.reasoning_effort == "medium"
        assert d.reasoning_efforts == ["low", "medium", "high"]

    def test_empty_levels_mean_known_unsupported(self) -> None:
        """空表 = 探测过、明确不支持调档；None = 未探测。两者语义不同。"""
        d = _deployment(reasoning_efforts=[])
        assert d.reasoning_efforts == []

    def test_model_describe_exposes_both_fields(self) -> None:
        from app.core.config import AppConfig, Settings
        from app.services.model_service import ModelService

        model = ModelConfig(
            id="step-5-preview",
            deployments=[_deployment(reasoning_effort="high")],
        )
        svc = ModelService(
            AppConfig(settings=Settings(), models={model.id: model}),
            router=None,  # type: ignore[arg-type]  # describe 不碰路由
        )
        data = svc.describe(model.id)
        dep = data["deployments"][0]  # type: ignore[index]
        assert dep["reasoning_effort"] == "high"
        assert dep["reasoning_efforts"] == ["low", "medium", "high"]


class TestInjectionPriority:
    def test_default_is_injected_when_client_says_nothing(self) -> None:
        payload, _ = _adapter().build_payload(
            _request(), _deployment(reasoning_effort="high")
        )
        assert payload["reasoning_effort"] == "high"

    def test_client_effort_wins_over_the_deployment_default(self) -> None:
        request = _request()
        request.__pydantic_extra__ = {"reasoning_effort": "low"}
        payload, _ = _adapter().build_payload(
            request, _deployment(reasoning_effort="high")
        )
        assert payload["reasoning_effort"] == "low"

    def test_responses_style_effort_also_wins(self) -> None:
        """Codex 把 reasoning.effort 翻成 extra 的 reasoning_effort，同一条道。"""
        request = _request()
        request.__pydantic_extra__ = {"reasoning_effort": "medium"}
        payload, _ = _adapter().build_payload(
            request, _deployment(reasoning_effort="max")
        )
        assert payload["reasoning_effort"] == "medium"

    def test_empty_default_injects_nothing(self) -> None:
        payload, _ = _adapter().build_payload(_request(), _deployment())
        assert "reasoning_effort" not in payload

    def test_falsey_default_injects_nothing(self) -> None:
        payload, _ = _adapter().build_payload(
            _request(), _deployment(reasoning_effort="")
        )
        assert "reasoning_effort" not in payload

    def test_default_is_not_reported_as_unmapped(self) -> None:
        """部署默认是网关有意注入的，不算「客户端传了映射不了的参数」。"""
        _, unsupported = _adapter().build_payload(
            _request(), _deployment(reasoning_effort="high")
        )
        assert "reasoning_effort" not in unsupported

    def test_options_still_win_over_the_default(self) -> None:
        """deployment.options 是更高优先级的显式覆盖，保持原语义。"""
        payload, _ = _adapter().build_payload(
            _request(),
            _deployment(
                reasoning_effort="high",
                options={"reasoning_effort": "low"},
            ),
        )
        assert payload["reasoning_effort"] == "low"


class TestOfficialPresetEfforts:
    """官方文档词表必须落在预设里——商汤 /models 不回档位字段，只靠探测永远未知。"""

    def test_deepseek_flash_native_levels(self) -> None:
        from app.models.presets import preset_for

        p = preset_for("deepseek-flash")
        assert p.reasoning_efforts == ("none", "low", "high", "max")
        assert p.reasoning_effort_default == "high"
        assert p.reasoning_effort_aliases["minimal"] == "low"
        assert p.reasoning_effort_aliases["ultra"] == "max"

    def test_glm52_has_the_widest_vocabulary(self) -> None:
        from app.models.presets import preset_for

        p = preset_for("glm-5.2")
        assert p.reasoning_efforts == (
            "none", "minimal", "low", "medium", "high", "xhigh", "max",
        )
        assert p.reasoning_effort_default == "max"

    def test_kimi_k3_levels(self) -> None:
        from app.models.presets import preset_for

        p = preset_for("kimi-k3")
        assert p.reasoning_efforts == ("low", "medium", "high", "max")
        assert p.reasoning_effort_default == "max"

    def test_unknown_family_does_not_invent_levels(self) -> None:
        from app.models.presets import preset_for

        p = preset_for("totally-made-up-model-9000")
        assert p.reasoning_efforts == ()

    def test_marketplace_falls_back_to_preset_when_discovery_is_silent(self) -> None:
        """商汤只回 id 时，档位必须来自官方文档预设，否则表单永远「档位未知」。"""
        from app.models.presets import preset_for

        preset = preset_for("deepseek-flash")
        # 合成路径与 provider_models 一致：探测没有 → 用预设
        facts_efforts: tuple[str, ...] = ()
        resolved = list(facts_efforts) or list(preset.reasoning_efforts)
        assert resolved == ["none", "low", "high", "max"]


class TestOfficialSpecs:
    """规格数字（上下文/最大输出/参数限制）必须来自官方文档，不许瞎填。"""

    def test_deepseek_flash_max_output_from_official_docs(self) -> None:
        from app.models.presets import preset_for

        p = preset_for("deepseek-flash")
        assert p.spec_source == "official"
        assert p.max_output_tokens == 131_072
        assert p.max_output_tokens_max == 393_216
        assert p.context_window == 1_048_576

    def test_kimi_k3_uses_max_completion_token_notes(self) -> None:
        from app.models.presets import preset_for

        p = preset_for("kimi-k3")
        assert p.spec_source == "official"
        assert p.max_output_tokens == 131_072
        assert any("max_completion_tokens" in n for n in p.spec_notes)
        assert p.supports_tools is True

    def test_unknown_model_does_not_claim_official_specs(self) -> None:
        from app.models.presets import preset_for

        p = preset_for("totally-made-up-model-9000")
        assert p.spec_source == "estimated"
        assert p.max_output_tokens is None  # 不猜具体数字

    def test_official_facts_rows_only_include_verified_fields(self) -> None:
        from app.models.presets import preset_for

        rows = preset_for("deepseek-flash").official_facts()
        labels = {r["label"] for r in rows}
        assert "最大输出" in labels
        assert "推理力度" in labels
        assert "工具调用" in labels
        assert "牌价 $/Mtok" in labels
        # 未收录模型：只有预估上下文，不编造 max_output / 档位 / 工具支持
        unknown = preset_for("totally-made-up-model-9000").official_facts()
        unknown_labels = {r["label"] for r in unknown}
        assert "最大输出" not in unknown_labels
        assert "推理力度" not in unknown_labels
        assert any("预估" in r["value"] for r in unknown)


class TestPerProviderAndHighestEffort:
    """同一模型按供应商拆档位表；默认上最高思考档。"""

    def test_kimi_k3_sensenova_has_medium(self) -> None:
        from app.models.presets import preset_for

        p = preset_for("kimi-k3", "sensenova")
        assert p.reasoning_efforts == ("low", "medium", "high", "max")

    def test_kimi_k3_moonshot_has_no_medium(self) -> None:
        """月之暗面官方只有 low/high/max——跟商汤词表不一样，不许混用。"""
        from app.models.presets import preset_for

        p = preset_for("kimi-k3", "moonshot")
        assert p.reasoning_efforts == ("low", "high", "max")
        assert "medium" not in p.reasoning_efforts

    def test_provider_specific_wins_over_generic(self) -> None:
        from app.models.presets import match_preset

        assert match_preset("kimi-k3", "moonshot").reasoning_efforts == ("low", "high", "max")
        assert match_preset("kimi-k3", "sensenova").reasoning_efforts == (
            "low", "medium", "high", "max",
        )

    def test_highest_effort_picks_the_strongest(self) -> None:
        from app.models.presets import highest_effort

        assert highest_effort(("none", "low", "high", "max")) == "max"
        assert highest_effort(("low", "medium", "high")) == "high"
        assert highest_effort(("none", "minimal", "low")) == "low"
        # none 是关思考，不算「最高」
        assert highest_effort(("none", "low")) == "low"
        assert highest_effort(("none",)) == ""
        assert highest_effort(()) == ""

    def test_xhigh_and_ultra_rank_above_high(self) -> None:
        from app.models.presets import highest_effort

        assert highest_effort(("low", "medium", "high", "xhigh", "max")) == "max"
        assert highest_effort(("high", "ultra")) == "ultra"
        assert highest_effort(("medium", "high", "xhigh")) == "xhigh"

    def test_nvidia_glm53_effort_vocabulary(self) -> None:
        """NVIDIA 上的 GLM-5.3 只有 low/high/max，与商汤 GLM-5.2 的 7 档不同。"""
        from app.models.presets import preset_for

        p = preset_for("glm-5.3", "nvidia")
        assert p.reasoning_efforts == ("low", "high", "max")
        assert p.reasoning_effort_default == "max"
        assert p.spec_source == "official"

    def test_stepfun_37_has_three_levels(self) -> None:
        from app.models.presets import preset_for

        p = preset_for("step-3.7-flash", "stepfun")
        assert p.reasoning_efforts == ("low", "medium", "high")
        assert p.reasoning_effort_default == "medium"
        assert p.supports_tools is True

    def test_partial_facts_still_show_with_estimate_tag(self) -> None:
        from app.models.presets import preset_for

        rows = preset_for("nemotron-3-super", "nvidia").official_facts()
        labels = {r["label"] for r in rows}
        assert "工具调用" in labels
        assert "使用须知" in labels
        # nemotron 上下文口径冲突 → 标预估
        assert any("预估" in r["value"] for r in rows if r["label"] == "上下文窗口")
