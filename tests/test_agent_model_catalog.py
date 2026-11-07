"""重点模型画像的离线测试：只读临时文件，不碰现役配置。"""
from __future__ import annotations

from pathlib import Path

from app.services.agent import model_catalog


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "model_catalog.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_load_valid_catalog(tmp_path: Path) -> None:
    _write(
        tmp_path,
        """
models:
  - id: hy4-preview
    name: 腾讯 Hy4（预览版）
    via: codebuddy
    tier: free
    context: 1000000
    strengths: [通用, 中文]
    verified: live
    note: 正式版只在桌面版可见
""",
    )
    entries, warnings = model_catalog.load_catalog(tmp_path)
    assert warnings == []
    assert len(entries) == 1
    entry = entries[0]
    assert entry.id == "hy4-preview"
    assert entry.tier == "free"
    assert entry.context == 1_000_000
    assert entry.strengths == ("通用", "中文")


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert model_catalog.load_catalog(tmp_path) == ([], [])


def test_entry_without_id_is_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "models:\n  - name: 没有编号\n    tier: free\n")
    entries, warnings = model_catalog.load_catalog(tmp_path)
    assert entries == []
    assert any("缺 id" in item for item in warnings)


def test_invalid_tier_is_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "models:\n  - id: x\n    tier: 免费\n")
    entries, warnings = model_catalog.load_catalog(tmp_path)
    assert entries == []
    assert any("tier" in item for item in warnings)


def test_invalid_verified_falls_back(tmp_path: Path) -> None:
    _write(tmp_path, "models:\n  - id: x\n    tier: free\n    verified: 听说\n")
    entries, warnings = model_catalog.load_catalog(tmp_path)
    assert len(entries) == 1
    assert entries[0].verified == "unverified"
    assert any("verified" in item for item in warnings)


def test_non_numeric_context_becomes_zero(tmp_path: Path) -> None:
    _write(tmp_path, "models:\n  - id: x\n    tier: free\n    context: 很多\n")
    entries, _ = model_catalog.load_catalog(tmp_path)
    assert entries[0].context == 0


def test_top_level_not_mapping(tmp_path: Path) -> None:
    _write(tmp_path, "- 只是一个列表\n")
    entries, warnings = model_catalog.load_catalog(tmp_path)
    assert entries == []
    assert any("顶层" in item for item in warnings)


def test_missing_models_key(tmp_path: Path) -> None:
    _write(tmp_path, "version: 1\n")
    entries, warnings = model_catalog.load_catalog(tmp_path)
    assert entries == []
    assert any("models" in item for item in warnings)


def test_as_dict_shape(tmp_path: Path) -> None:
    _write(tmp_path, "models:\n  - id: x\n    name: 示例\n    tier: free\n")
    entries, _ = model_catalog.load_catalog(tmp_path)
    data = entries[0].as_dict()
    assert data["id"] == "x"
    assert data["strengths"] == []
    assert isinstance(data["context"], int)
