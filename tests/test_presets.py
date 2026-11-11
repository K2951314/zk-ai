"""Model presets: curated defaults for the marketplace."""

from __future__ import annotations

from app.models.presets import match_preset, preset_for


def test_exact_match() -> None:
    preset = match_preset("glm-5.3")
    assert preset is not None
    assert preset.display_name == "GLM-5.3"
    assert preset.context_window == 1_048_576


def test_vendor_prefix_stripped() -> None:
    preset = match_preset("z-ai/glm-5.3-flash")
    assert preset is not None
    assert preset.family == "glm-5.3-flash"


def test_vendor_prefix_deepseek() -> None:
    preset = match_preset("deepseek-ai/DeepSeek-V4-Flash-0731")
    assert preset is not None
    assert preset.family == "deepseek-v4-flash"


def test_case_insensitive() -> None:
    assert match_preset("KIMI-K3") is not None


def test_unknown_model_falls_back_to_guess() -> None:
    preset = preset_for("acme/vision-max-9000")
    assert preset.capabilities["vision"] > 0  # "vision" in the name


def test_unknown_coder_guess() -> None:
    preset = preset_for("someone/codegemma-7b")
    assert preset.capabilities["coding"] >= 8.0


def test_unknown_reasoner_guess() -> None:
    preset = preset_for("someone/deepseek-r1-distill")
    assert preset.capabilities["reasoning"] >= 9.0


def test_flash_is_fast_and_cheap() -> None:
    preset = preset_for("someone/model-flash")
    assert preset.capabilities["speed"] >= 9.0
    assert preset.capabilities["cost"] >= 9.0


def test_preset_for_always_returns_a_preset() -> None:
    for name in ["foo/bar", "weird", "x"]:
        preset = preset_for(name)
        assert preset.display_name
        assert preset.context_window > 0
        assert preset.capabilities


def test_deepseek_v41_flash_maps_to_its_announced_id() -> None:
    """产品名 ↔ ID 的映射必须齐全，否则运营者只能去翻官方公告。

    2026-09-29 实测的教训：日日新 Token Plan 公告（2026-09-22）说
    「DeepSeek V4.1 Flash 上线，Model ID：deepseek-flash」。但预设表里只有
    `deepseek-v4-flash`（上一代），没有 `deepseek-flash`，于是：
      * 市场把 `deepseek-flash` 显示成裸 ID，没人知道它就是 V4.1 Flash；
      * 反而把陈旧的 `deepseek-v4.1-flash`（403 套餐无权限）当成了目标。
    探针能判「这个 ID 能不能调」，判不了「你要的产品是哪个 ID」——这一行就是那个缺口。
    """
    preset = match_preset("deepseek-flash")
    assert preset is not None, "deepseek-flash 没有预设 —— 产品名就丢了"
    assert preset.display_name == "DeepSeek V4.1 Flash"
    # 两者必须能区分开，别又混成一个
    older = match_preset("deepseek-v4-flash")
    assert older is not None and older.display_name == "DeepSeek V4 Flash"
    assert older.display_name != preset.display_name


def test_deprecated_deepseek_v4_pro_is_labelled() -> None:
    """v4-pro 2026-10-08 下线，市场至少要显示出来，别让人以为它长期可用。"""
    preset = match_preset("deepseek-v4-pro")
    assert preset is not None
    assert "下线" in preset.display_name


def test_qwen38_flash_next_keeps_its_org_prefix_mapping() -> None:
    """魔搭的上游 id 带 Qwen/ 前缀；预设要按去前缀后的名字匹配到友好名。"""
    preset = match_preset("Qwen/Qwen3.8-Flash-Next")
    assert preset is not None
    assert preset.display_name == "Qwen3.8 Flash Next"


# --------------------------------------------------------------------------- #
# 华为云 MaaS（provider id: HW，西南-贵阳一）
# 2026-11-11 运营者实测：市场上的 deepseek-v4.1-flash-1017 显示 256K / 无思考
# 档位——因为预设表的 family 键是商汤的公告 id（deepseek-flash），华为目录用的
# 是另一套 id，最长后缀匹配不上，整行掉进 guess_from_name 的保守 256K。
# 官方口径（Model List + 计费说明 + Anthropic 兼容接口文档 + 下线公告）录入后，
# 这几个测试钉住三件事：HW 目录 id 全部命中、规格不被猜、别的供应商不被带偏。
# --------------------------------------------------------------------------- #
def test_huawei_v41_flash_1017_has_real_specs() -> None:
    """运营者开在华为上的主力模型：1M 上下文 + none/low/high/max（默认 high）。"""
    preset = preset_for("deepseek-v4.1-flash-1017", "HW")
    assert preset.provider == "HW"
    assert preset.context_window == 1_048_576, "V4.1 Flash 1017 是 1M，不是 256K"
    assert preset.reasoning_efforts == ("none", "low", "high", "max")
    assert preset.reasoning_effort_default == "high"
    assert preset.spec_source == "official"
    assert preset.max_output_tokens_max == 393_216  # 384K


def test_huawei_catalogue_ids_all_resolve_to_official_presets() -> None:
    """HW 目录里的每个 id 都必须命中 HW 专属预设，且不是 256K 的猜测值。"""
    expected = {
        "deepseek-v4.1-flash-1017": 1_048_576,
        "deepseek-v4.1-flash": 1_048_576,
        "deepseek-v4-flash": 1_048_576,
        "glm-5.3": 1_048_576,
        "glm-5.2": 1_048_576,
        "glm-5.1": 202_752,               # 198K
        "glm.5.2-arkts-spark": 202_752,   # 198K
        "openpangu-2.0-pro": 524_288,     # 512K
        "openpangu-2.0-flash": 524_288,
    }
    for model_id, ctx in expected.items():
        preset = preset_for(model_id, "HW")
        assert preset.provider == "HW", f"{model_id} 没命中 HW 预设（会掉进 256K 猜测）"
        assert preset.spec_source == "official", f"{model_id} 的规格不是官方口径"
        assert preset.context_window == ctx, f"{model_id} 上下文应为 {ctx}"


def test_huawei_arkts_spark_id_carries_a_dot() -> None:
    """华为目录里 ArkTS-SPARK 的 id 带点（glm.5.2-arkts-spark）——不是笔误。

    官方文档写连字符，但实测连字符 404「Invalid model」、带点 403「未开通」，
    所以预设的 family 必须按目录的带点 id 建，否则市场永远匹配不上。
    """
    dotted = match_preset("glm.5.2-arkts-spark", "HW")
    assert dotted is not None and dotted.provider == "HW"
    assert dotted.context_window == 202_752, "arkts 自己的规格是 198K"
    # 连字符版（官方文档的写法）不该命中 arkts 条目：它上游 404，只会经最长
    # 前缀落到 glm-5.2 家族（1M），然后被「验证可调用」的 not_found 拦下。
    dashed = match_preset("glm-5.2-arkts-spark", "HW")
    assert dashed is None or dashed.context_window != 202_752


def test_huawei_retiring_models_are_labelled() -> None:
    """华为 2026-11 下线潮：glm-5.1（11-16）与 V4.1-Flash 非 1017 版（11-30）
    都要在市场显示里标出来，别让运营者把下线模型加进链路。"""
    for model_id in ("glm-5.1", "deepseek-v4.1-flash", "deepseek-v4-flash",
                     "openpangu-2.0-pro", "openpangu-2.0-flash"):
        preset = preset_for(model_id, "HW")
        assert "下线" in preset.display_name, f"{model_id} 没标下线"


def test_huawei_entries_do_not_hijack_other_providers() -> None:
    """按供应商拆的契约：加 HW 条目不能改变别的供应商的命中结果。"""
    sensenova = preset_for("deepseek-v4-flash", "sensenova")
    assert sensenova.provider is None, "商汤的通用条目还在"
    nvidia = preset_for("glm-5.3", "nvidia")
    assert nvidia.provider == "nvidia", "NVIDIA 的 glm-5.3 条目还在"
    # modelscope / AMD 的同名模型顺带拿到 1M（模型级事实），不再是 256K 猜测
    modelscope = preset_for("deepseek-ai/DeepSeek-V4.1-Flash-1017", "modelscope")
    assert modelscope.context_window == 1_048_576
    assert modelscope.spec_source == "official"


def test_sensenova_68_flash_carries_the_official_1m_window() -> None:
    """商汤目录报 262144，官方文档写 1M —— 预设必须按官方写，并留证据。

    2026-11-04 实测：`GET /v1/models` 里 sensenova-6.8-flash 的
    `context_length` 是 **262144**，官方文档却写「支持1 M 上下文窗口」。
    真请求塞 1,047,678 prompt tokens 正常返回，1,049,xxx 才 400——**目录错了**。
    这条预设存在的意义就是让"添加模型"不用去翻文档：数字、档位、坑都写在里面。
    """
    preset = match_preset("sensenova-6.8-flash", "sensenova")
    assert preset is not None, "sensenova-6.8-flash 没有预设 —— 又只能按目录的旧值填"
    assert preset.context_window == 1_048_576
    assert preset.spec_source == "official"
    assert preset.max_output_tokens == 65_536
    assert preset.max_output_tokens_max == 131_072
    assert preset.supports_tools is True
    assert preset.supports_structured_output is True
    # 官方默认 high；档位表刻意不收 none（见下一条）
    assert preset.reasoning_effort_default == "high"
    assert "minimal" in preset.reasoning_efforts
    assert "xhigh" in preset.reasoning_efforts


def test_sensenova_68_flash_does_not_advertise_an_off_switch_it_lacks() -> None:
    """「关思考」在这家不成立：传 none 不报错，但思考会泄进 content。

    实测（2026-11-04）：reasoning_effort="none" 与 thinking.type="disabled"
    都不报错，返回的 reasoning_tokens 也确实为 0——但正文变成
    "Here's a thinking process: ..." / "France's capital is Paris.</think>Paris"，
    即思考搬进了 content。对客户端这是脏回答，不是省了思考。
    所以档位表不列 none：运营者在控制台选不到它，就不会配出一个"答非所问"的部署。
    """
    preset = match_preset("sensenova-6.8-flash", "sensenova")
    assert preset is not None
    assert "none" not in preset.reasoning_efforts
    notes = " ".join(preset.spec_notes)
    assert "泄进 content" in notes or "泄进" in notes
    # 思考与正文共享 max_tokens 这条也要写上——预算给小了会只有思考没有正文
    assert "max_tokens" in notes


def test_the_68_flash_family_is_not_confused_with_its_lite_sibling() -> None:
    """`sensenova-6.8-flash` 与 `-flash-lite` 是两条预设，别互相冒领。"""
    flash = match_preset("sensenova-6.8-flash", "sensenova")
    lite = match_preset("sensenova-6.8-flash-lite", "sensenova")
    assert flash is not None and lite is not None
    assert flash.family != lite.family
    assert flash.context_window == 1_048_576
    assert lite.context_window == 262_144
