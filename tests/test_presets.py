"""Model presets: curated defaults for the marketplace."""

from __future__ import annotations

from app.models.presets import match_preset, preset_for


def test_exact_match() -> None:
    preset = match_preset("glm-5.3")
    assert preset is not None
    assert preset.display_name == "GLM-5.3"
    assert preset.context_window == 1_000_000


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
