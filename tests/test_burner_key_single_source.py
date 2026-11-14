"""burner 配置键表的单一来源（2026-11-14）。

背景：同一份「键 → 类型」表原本在两处各写一遍，靠注释互相提醒
「保持一致」。实测当时确实一致（31 键、类型全同），但那是人工维护的
巧合——新加一个键漏改一处，表现就是「写在 burner.yaml 里被静默忽略」。
AGENTS.md 已经记过同型的坑（商汤额度翻倍要同步三处）。

现在单一来源是 `config/burner_keys.json`。这里钉三件事：
1. JSON 存在且可解析；
2. 脚本与 config_io 读出来**完全一致**；
3. 脚本的兜底表（JSON 缺失时用）也与 JSON 同形——否则独立运行时行为会漂。
"""

from __future__ import annotations

import json
import sys

from app.core.config import PROJECT_ROOT

JSON_PATH = PROJECT_ROOT / "config" / "burner_keys.json"


def _script_module():
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    try:
        import burn_sensenova  # type: ignore[import-not-found]

        return burn_sensenova
    finally:
        sys.path.pop(0)


def test_the_single_source_file_exists_and_parses() -> None:
    assert JSON_PATH.is_file(), f"单一来源文件缺失：{JSON_PATH}"
    raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    keys = raw.get("keys")
    assert isinstance(keys, dict) and keys, "burner_keys.json 里 keys 必须是非空映射"
    assert set(keys.values()) <= {"str", "int", "float", "bool"}, "出现了未知类型名"


def test_script_and_config_io_read_the_same_table() -> None:
    """这是本次改动的核心断言：两边必须逐键一致。"""
    mod = _script_module()
    from app.services.burner_service.config_io import CONFIG_KEYS

    script_keys = mod._CONFIG_KEYS
    assert script_keys, "脚本读到空表——JSON 可能没被读到"
    assert set(script_keys) == set(CONFIG_KEYS), (
        f"键不一致：仅脚本 {sorted(set(script_keys) - set(CONFIG_KEYS))}，"
        f"仅 config_io {sorted(set(CONFIG_KEYS) - set(script_keys))}"
    )
    mismatched = {
        k: (script_keys[k].__name__, CONFIG_KEYS[k].__name__)
        for k in set(script_keys) & set(CONFIG_KEYS)
        if script_keys[k] is not CONFIG_KEYS[k]
    }
    assert not mismatched, f"类型不一致：{mismatched}"


def test_the_script_fallback_matches_the_json() -> None:
    """脚本必须能独立跑：JSON 读不到时用兜底，兜底不能与 JSON 漂移。"""
    mod = _script_module()
    raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))["keys"]
    fallback = mod._FALLBACK_CONFIG_KEYS
    assert set(fallback) == set(raw), (
        f"兜底与 JSON 键不一致：仅兜底 {sorted(set(fallback) - set(raw))}，"
        f"仅 JSON {sorted(set(raw) - set(fallback))}"
    )
    for name, typ in raw.items():
        assert fallback[name].__name__ == typ, f"{name}: 兜底 {fallback[name].__name__} vs JSON {typ}"


def test_the_script_still_runs_without_the_json(tmp_path, monkeypatch) -> None:
    """JSON 被挪走时，脚本仍要拿到完整键表（这是它「不依赖 app」的独立性）。"""
    mod = _script_module()
    original = mod._load_config_keys.__globals__["Path"]
    # 把项目根指到空目录，模拟 JSON 不存在
    monkeypatch.setattr(mod, "Path", lambda _p: tmp_path / "nope")
    loaded = mod._load_config_keys()
    monkeypatch.setattr(mod, "Path", original)
    assert set(loaded) == set(mod._FALLBACK_CONFIG_KEYS)
    assert len(loaded) == 31
