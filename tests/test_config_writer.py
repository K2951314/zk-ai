"""YAML write-through (app/core/config_writer.py): comment-preserving edits."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.config_writer import (
    delete_list_entry,
    load_document,
    sync_provider_rate_limits,
    upsert_list_entry,
)

SAMPLE = """\
# 顶部大注释：四层概念
models:
  # -----
  # 模型 A 说明
  # -----
  - id: a
    enabled: true
    context_window: 100
    capabilities:
      coding: 5.0
    deployments:
      - id: a-dep
        provider_id: p
        model: a
        priority: 100
  # -----
  # 模型 B 说明
  # -----
  - id: b
    enabled: true
    context_window: 200
    capabilities:
      coding: 6.0
    deployments:
      - id: b-dep
        provider_id: p
        model: b
        priority: 90

aliases:
  # 别名入口
  - name: zk-all
    targets: [a, b]
    strategy: priority
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def _ids(path: Path) -> list[str]:
    return [m["id"] for m in yaml.safe_load(path.read_text("utf-8"))["models"]]


def test_upsert_appends_new_model_and_keeps_comments(cfg: Path) -> None:
    upsert_list_entry(cfg, "models", "id", "c", {
        "id": "c", "enabled": True, "context_window": 300,
        "capabilities": {"coding": 7.0},
        "deployments": [{"id": "c-dep", "provider_id": "p", "model": "c", "priority": 50}],
    })
    assert _ids(cfg) == ["a", "b", "c"]
    text = cfg.read_text("utf-8")
    assert "顶部大注释" in text and "模型 B 说明" in text


def test_upsert_edits_existing_in_place(cfg: Path) -> None:
    upsert_list_entry(cfg, "models", "id", "a", {
        "id": "a", "enabled": True, "context_window": 999,
        "capabilities": {"coding": 5.0},
        "deployments": [{"id": "a-dep", "provider_id": "p", "model": "a", "priority": 100}],
    })
    assert _ids(cfg) == ["a", "b"]
    models = {m["id"]: m for m in yaml.safe_load(cfg.read_text("utf-8"))["models"]}
    assert models["a"]["context_window"] == 999
    assert "模型 B 说明" in cfg.read_text("utf-8")


def test_delete_removes_entry_and_its_header(cfg: Path) -> None:
    assert delete_list_entry(cfg, "models", "id", "b")
    assert _ids(cfg) == ["a"]
    text = cfg.read_text("utf-8")
    assert "模型 B 说明" not in text  # header gone
    assert "模型 A 说明" in text  # neighbour header intact


def test_delete_missing_is_noop(cfg: Path) -> None:
    before = cfg.read_text("utf-8")
    assert not delete_list_entry(cfg, "models", "id", "ghost")
    assert cfg.read_text("utf-8") == before


def test_delete_creates_rolling_backup(cfg: Path) -> None:
    delete_list_entry(cfg, "models", "id", "a")
    assert Path(str(cfg) + ".bak").exists()


def test_delete_alias(cfg: Path) -> None:
    assert delete_list_entry(cfg, "aliases", "name", "zk-all")
    data = yaml.safe_load(cfg.read_text("utf-8"))
    assert not data["aliases"]  # None or [] — the list is empty now
    assert "别名入口" not in cfg.read_text("utf-8")  # header comment removed too


# --------------------------------------------------------------------------- #
# providers.yaml rate_limits round-trip
# --------------------------------------------------------------------------- #
def test_sync_provider_rate_limits(cfg: Path) -> None:
    providers = tmp_providers(cfg.parent / "providers.yaml")
    sync_provider_rate_limits(providers, "nvidia", [
        {"scope": "credential", "window_seconds": 60, "max_requests": 40},
    ])
    data = yaml.safe_load(providers.read_text("utf-8"))
    nvidia = next(p for p in data["providers"] if p["id"] == "nvidia")
    assert nvidia["options"]["rate_limits"][0]["max_requests"] == 40
    # other provider untouched
    sen = next(p for p in data["providers"] if p["id"] == "sensenova")
    assert "rate_limits" not in sen.get("options", {})


def test_sync_provider_rate_limits_removes_when_empty(cfg: Path) -> None:
    providers = tmp_providers(cfg.parent / "providers.yaml")
    sync_provider_rate_limits(providers, "nvidia", [])
    data = yaml.safe_load(providers.read_text("utf-8"))
    nvidia = next(p for p in data["providers"] if p["id"] == "nvidia")
    assert "rate_limits" not in (nvidia.get("options") or {})


def test_sync_provider_rate_limits_unknown_raises(cfg: Path) -> None:
    providers = tmp_providers(cfg.parent / "providers.yaml")
    with pytest.raises(KeyError):
        sync_provider_rate_limits(providers, "ghost", [{"window_seconds": 60, "max_requests": 1}])


def tmp_providers(path: Path) -> Path:
    path.write_text(
        "providers:\n"
        "  - id: nvidia\n"
        "    type: openai_compatible\n"
        "    base_url: x\n"
        "  - id: sensenova\n"
        "    type: openai_compatible\n"
        "    base_url: y\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# Real-world: every model/alias survives a delete+add round-trip and re-parses
# --------------------------------------------------------------------------- #
def test_real_config_roundtrip_stays_schema_valid() -> None:
    real = Path(__file__).resolve().parents[1] / "config" / "models.yaml"
    if not real.exists():  # only meaningful when the repo ships a real config
        pytest.skip("no local config/models.yaml")
    import shutil
    import tempfile

    from app.models.provider import ModelAliasConfig, ModelConfig

    with tempfile.TemporaryDirectory() as d:
        work = Path(d) / "models.yaml"
        shutil.copy2(real, work)
        models = load_document(work)["models"]
        victim = str(models[1]["id"])
        assert delete_list_entry(work, "models", "id", victim)
        upsert_list_entry(work, "models", "id", "zz-probe", {
            "id": "zz-probe", "enabled": True, "context_window": 128000,
            "capabilities": {"coding": 5.0},
            "deployments": [{"id": "zz-dep", "provider_id": "nvidia", "model": "z-ai/glm-5.3",
                             "priority": 100}],
        })
        data = yaml.safe_load(work.read_text("utf-8"))
        ids = [m["id"] for m in data["models"]]
        assert victim not in ids and "zz-probe" in ids
        for m in data["models"]:
            ModelConfig(**m)  # would raise on any malformed round-trip
        for a in data["aliases"]:
            ModelAliasConfig(**a)


def test_upsert_provider_keeps_existing_credentials(tmp_path) -> None:
    """Console edits must not wipe the hand-written credentials list."""
    import yaml

    from app.core.config_writer import upsert_provider

    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers:\n"
        "  - id: fake\n"
        "    type: openai_compatible\n"
        "    base_url: http://x/v1\n"
        "    credentials:\n"
        "      - id: k1\n"
        "        env: FAKE_KEY\n"
        "      - id: k2\n"
        "        env: FAKE_KEY_2\n",
        encoding="utf-8",
    )
    upsert_provider(path, "fake", {"id": "fake", "type": "openai_compatible",
                                   "base_url": "http://x/v1", "timeout": 99.0})
    data = yaml.safe_load(path.read_text("utf-8"))
    entry = data["providers"][0]
    assert entry["timeout"] == 99.0
    assert [c["id"] for c in entry["credentials"]] == ["k1", "k2"]


def test_delete_provider_removes_entry(tmp_path) -> None:
    import yaml

    from app.core.config_writer import delete_provider, upsert_provider

    path = tmp_path / "providers.yaml"
    path.write_text("providers:\n  - id: fake\n    type: openai_compatible\n", encoding="utf-8")
    upsert_provider(path, "to-delete", {"id": "to-delete", "type": "openai_compatible",
                                         "base_url": "http://y/v1"})
    assert delete_provider(path, "to-delete") is True
    assert yaml.safe_load(path.read_text("utf-8"))["providers"][0]["id"] == "fake"


# --------------------------------------------------------------------------- #
# .env writing (secrets never go into the YAML)
# --------------------------------------------------------------------------- #
def test_upsert_env_var_appends_and_preserves_comments(tmp_path) -> None:
    from app.core.config_writer import upsert_env_var

    path = tmp_path / ".env"
    # No trailing newline: the append must not glue itself onto the last line.
    path.write_text("# 注释\nZKAI_PORT=8317", encoding="utf-8")
    assert upsert_env_var(path, "STEPFUN_API_KEY", "sk-new") is False
    text = path.read_text("utf-8")
    assert text == "# 注释\nZKAI_PORT=8317\nSTEPFUN_API_KEY=sk-new\n"
    assert text.endswith("\n")


def test_upsert_env_var_updates_in_place(tmp_path) -> None:
    from app.core.config_writer import upsert_env_var

    path = tmp_path / ".env"
    path.write_text("A=1\nSTEPFUN_API_KEY=old\nB=2\n", encoding="utf-8")
    assert upsert_env_var(path, "STEPFUN_API_KEY", "sk-new") is True
    text = path.read_text("utf-8")
    assert "STEPFUN_API_KEY=sk-new" in text
    assert "old" not in text
    assert text.index("A=1") < text.index("STEPFUN_API_KEY") < text.index("B=2")


def test_upsert_env_var_handles_export_and_crlf(tmp_path) -> None:
    from app.core.config_writer import upsert_env_var

    path = tmp_path / ".env"
    path.write_bytes(b"export KEY_A=1\r\nOTHER=2\r\n")
    assert upsert_env_var(path, "KEY_A", "9") is True
    assert upsert_env_var(path, "NEW_ONE", "x") is False
    raw = path.read_bytes()
    assert b"export KEY_A=9" not in raw  # rewritten as a plain NAME=value line
    assert b"KEY_A=9" in raw and b"NEW_ONE=x" in raw
    assert raw.count(b"\r\n") >= 3 and b"\n\n" not in raw  # EOL style preserved


def test_upsert_env_var_keeps_one_rolling_backup(tmp_path) -> None:
    from app.core.config_writer import upsert_env_var

    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")
    upsert_env_var(path, "B", "2")
    upsert_env_var(path, "C", "3")
    bak = tmp_path / ".env.bak"
    assert bak.exists()
    assert "B=2" in bak.read_text("utf-8")  # backup holds the pre-second-write state
    assert "C=3" not in bak.read_text("utf-8")


def test_looks_like_secret_flags_pasted_keys() -> None:
    from app.core.config_writer import looks_like_secret

    # The real-world mistake: a 64-char key pasted into the id field.
    assert looks_like_secret("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
    assert looks_like_secret("sk-" + "a" * 40)
    # Legitimate short ids are left alone.
    assert not looks_like_secret("sensenova-01")
    assert not looks_like_secret("stepfun-01")
    assert not looks_like_secret("a")
    assert not looks_like_secret(None)
    assert not looks_like_secret("")
