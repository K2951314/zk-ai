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
