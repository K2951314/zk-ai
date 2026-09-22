"""Tests for app/services/chatgpt_service.py - the ChatGPT/Codex client config.

The two invariants that matter here:

* the app owns ``~/.codex/config.toml`` (mcp_servers / plugins / projects /
  desktop sections), so our writes must be surgical - other bytes untouched;
* applying twice must be a no-op (no churn, no backup spam), and everything
  overwritten must have a timestamped backup first.
"""

from __future__ import annotations

import contextlib
import sys
import tomllib
from pathlib import Path

import pytest

from app.services import chatgpt_service as cg

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: A trimmed copy of a real desktop-owned config.toml structure (no secrets).
APP_OWNED_CONFIG = """\
model = "zk-auto"
model_provider = "zkai"
model_reasoning_effort = "max"
notify = [ "C:\\\\Users\\\\me\\\\codex-computer-use.exe", "turn-ended" ]

# ---- ZK-AI gateway ----
[model_providers.zkai]
name = "ZK-AI"
base_url = "http://127.0.0.1:8317/v1"
wire_api = "responses"
env_key = "ZKAI_API_TOKEN"

[mcp_servers.node_repl]
command = 'C:\\Users\\me\\.codex\\node_repl.exe'
startup_timeout_sec = 120

[projects.'d:\\work\\zk-ai']
trust_level = "trusted"

[features]
memories = true

[desktop]
followUpQueueMode = "queue"
"""


@pytest.fixture
def codex_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("ZKAI_CODEX_HOME", str(home))
    return home


def _desired(**overrides: object) -> cg.ChatGptConfig:
    cfg = cg.ChatGptConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# apply: create / update / preserve
# --------------------------------------------------------------------------- #
def test_apply_creates_minimal_config_from_scratch(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    result = cg.apply_config(path, _desired())

    assert not result.no_op
    assert result.backup is None  # nothing existed to back up
    data = _load(path)
    assert data["model"] == "zk-auto"
    assert data["model_provider"] == "zkai"
    assert data["model_reasoning_effort"] == "max"
    table = data["model_providers"]["zkai"]
    assert table == {
        "name": "ZK-AI",
        "base_url": "http://127.0.0.1:8317/v1",
        "wire_api": "responses",
        "env_key": "ZKAI_API_TOKEN",
    }


def test_apply_updates_provider_table_in_place_and_preserves_app_sections(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text(APP_OWNED_CONFIG, encoding="utf-8")

    result = cg.apply_config(path, _desired(base_url="http://127.0.0.1:9999/v1"))

    text = path.read_text(encoding="utf-8")
    data = _load(path)
    assert data["model_providers"]["zkai"]["base_url"] == "http://127.0.0.1:9999/v1"
    # app-owned sections survive byte for byte (mcp_servers / projects / features / desktop)
    assert "[mcp_servers.node_repl]" in text
    assert "command = 'C:\\Users\\me\\.codex\\node_repl.exe'" in text
    assert "[projects.'d:\\work\\zk-ai']" in text
    assert 'trust_level = "trusted"' in text
    assert "[features]" in text and "memories = true" in text
    assert "[desktop]" in text and 'followUpQueueMode = "queue"' in text
    # our keys changed; the header comment above the table stays put
    assert "# ---- ZK-AI gateway ----" in text
    assert result.backup is not None and result.backup.exists()
    assert any(c.key == "base_url" for c in result.changes)


def test_apply_replaces_top_level_keys_in_place(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text(APP_OWNED_CONFIG, encoding="utf-8")

    cg.apply_config(path, _desired(model="glm-5.3", model_reasoning_effort="low"))

    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == 'model = "glm-5.3"'
    data = _load(path)
    assert data["model_reasoning_effort"] == "low"
    # untouched top-level array key is still there
    assert '"turn-ended"' in text


def test_apply_creates_missing_provider_table_at_eof(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text('model = "zk-auto"\nmodel_provider = "zkai"\n', encoding="utf-8")

    cg.apply_config(path, _desired())

    data = _load(path)
    assert data["model_providers"]["zkai"]["base_url"] == "http://127.0.0.1:8317/v1"


def test_apply_renaming_provider_creates_a_new_table_and_leaves_the_old_one(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text(APP_OWNED_CONFIG, encoding="utf-8")

    cg.apply_config(path, _desired(model_provider="zk-ai-gw"))

    data = _load(path)
    assert data["model_provider"] == "zk-ai-gw"
    assert data["model_providers"]["zk-ai-gw"]["base_url"] == "http://127.0.0.1:8317/v1"
    assert data["model_providers"]["zkai"]  # old table stays (switch back is instant)


# --------------------------------------------------------------------------- #
# official mode
# --------------------------------------------------------------------------- #
def test_official_mode_removes_model_provider_and_sets_official_model(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text(APP_OWNED_CONFIG, encoding="utf-8")

    cg.apply_config(path, _desired(mode="official", official_model="gpt-5.6-terra"))

    data = _load(path)
    text = path.read_text(encoding="utf-8")
    assert data["model"] == "gpt-5.6-terra"
    assert "model_provider" not in data
    # the provider table is kept so switching back to the gateway is instant
    assert data["model_providers"]["zkai"]["base_url"] == "http://127.0.0.1:8317/v1"
    # app sections untouched again
    assert "[mcp_servers.node_repl]" in text


def test_official_mode_then_back_to_gateway_is_a_full_restore(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text(APP_OWNED_CONFIG, encoding="utf-8")

    cg.apply_config(path, _desired(mode="official"))
    cg.apply_config(path, _desired())  # back to the gateway

    data = _load(path)
    assert data["model"] == "zk-auto"
    assert data["model_provider"] == "zkai"
    assert data["model_providers"]["zkai"] == {
        "name": "ZK-AI",
        "base_url": "http://127.0.0.1:8317/v1",
        "wire_api": "responses",
        "env_key": "ZKAI_API_TOKEN",
    }


# --------------------------------------------------------------------------- #
# idempotency / backups / style preservation
# --------------------------------------------------------------------------- #
def test_second_apply_is_a_no_op_without_new_backup(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text(APP_OWNED_CONFIG, encoding="utf-8")

    cg.apply_config(path, _desired(model="kimi-k3"))
    first_text = path.read_text(encoding="utf-8")

    second = cg.apply_config(path, _desired(model="kimi-k3"))

    assert second.no_op
    assert second.changes == []
    assert second.backup is None
    assert path.read_text(encoding="utf-8") == first_text
    # exactly one backup exists after two applies
    backups = list(codex_dir.glob("config.toml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == APP_OWNED_CONFIG


def test_backup_holds_the_pre_write_content(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text(APP_OWNED_CONFIG, encoding="utf-8")

    result = cg.apply_config(path, _desired(wire_api="chat"))

    assert result.backup is not None
    assert result.backup.name.startswith("config.toml.bak-")
    assert result.backup.read_text(encoding="utf-8") == APP_OWNED_CONFIG


def test_crlf_line_endings_are_preserved(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text(APP_OWNED_CONFIG.replace("\n", "\r\n"), encoding="utf-8", newline="")

    cg.apply_config(path, _desired(base_url="http://127.0.0.1:7777/v1"))

    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert raw.replace(b"\r\n", b"") .count(b"\n") == 0  # no bare LF left
    assert b"http://127.0.0.1:7777/v1" in raw


def test_model_reasoning_effort_empty_removes_the_line(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text(APP_OWNED_CONFIG, encoding="utf-8")

    cg.apply_config(path, _desired(model_reasoning_effort=""))

    data = _load(path)
    assert "model_reasoning_effort" not in data


def test_empty_existing_file_is_populated(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text("", encoding="utf-8")

    cg.apply_config(path, _desired())

    data = _load(path)
    assert data["model"] == "zk-auto"


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"model": ""}, "model"),
        ({"base_url": "127.0.0.1:8317"}, "base_url"),
        ({"base_url": "ftp://x/v1"}, "base_url"),
        ({"wire_api": "soap"}, "wire_api"),
        ({"env_key": "9bad"}, "env_key"),
        ({"model_reasoning_effort": "ultra"}, "思考强度"),
        ({"model_provider": "bad name!"}, "model_provider"),
        ({"provider_display": ""}, "provider_display"),
        ({"mode": "official", "official_model": ""}, "official_model"),
    ],
)
def test_validate_rejects_garbage(overrides: dict, needle: str) -> None:
    errors = cg.validate(_desired(**overrides))
    assert any(needle in e for e in errors), errors


def test_validate_accepts_the_defaults() -> None:
    assert cg.validate(cg.ChatGptConfig()) == []


# --------------------------------------------------------------------------- #
# plan / drift / disk state
# --------------------------------------------------------------------------- #
def test_plan_changes_lists_key_level_diffs() -> None:
    import tomllib as _t

    disk = _t.loads(APP_OWNED_CONFIG)
    changes = cg.plan_changes(disk, _desired(base_url="http://127.0.0.1:9000/v1"))
    keys = {(c.scope, c.key) for c in changes}
    assert ("provider", "base_url") in keys
    by_key = {(c.scope, c.key): c for c in changes}
    assert by_key[("provider", "base_url")].old == "http://127.0.0.1:8317/v1"
    assert by_key[("provider", "base_url")].new == "http://127.0.0.1:9000/v1"


def test_plan_changes_is_empty_when_already_in_sync() -> None:
    import tomllib as _t

    disk = _t.loads(APP_OWNED_CONFIG)
    assert cg.plan_changes(disk, cg.ChatGptConfig()) == []


def test_read_disk_state_reads_the_active_provider_table(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text(APP_OWNED_CONFIG, encoding="utf-8")

    state = cg.read_disk_state(path)

    assert state.exists
    assert state.model == "zk-auto"
    assert state.model_provider == "zkai"
    assert state.base_url == "http://127.0.0.1:8317/v1"
    assert state.wire_api == "responses"
    assert state.env_key == "ZKAI_API_TOKEN"
    assert state.model_reasoning_effort == "max"


def test_read_disk_state_missing_file(codex_dir: Path) -> None:
    state = cg.read_disk_state(codex_dir / "config.toml")
    assert not state.exists


def test_read_disk_state_broken_toml_reports_error(codex_dir: Path) -> None:
    path = codex_dir / "config.toml"
    path.write_text("model = [unclosed\n", encoding="utf-8")

    state = cg.read_disk_state(path)

    assert state.exists
    assert state.parse_error


# --------------------------------------------------------------------------- #
# desired config yaml round-trip
# --------------------------------------------------------------------------- #
def test_desired_yaml_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.yaml"

    cg.save_desired(path, _desired(model="glm-5.3", base_url="http://127.0.0.1:9000/v1"))
    loaded = cg.load_desired(path)

    assert loaded is not None
    assert loaded.model == "glm-5.3"
    assert loaded.base_url == "http://127.0.0.1:9000/v1"
    assert loaded.mode == "zk-ai"
    # save keeps a rolling backup of the previous file
    assert path.with_name("chatgpt.yaml.bak").exists() is False  # first save: nothing to rotate
    cg.save_desired(path, _desired(model="kimi-k3"))
    assert path.with_name("chatgpt.yaml.bak").exists()


def test_load_desired_missing_file_returns_none(tmp_path: Path) -> None:
    assert cg.load_desired(tmp_path / "nope.yaml") is None


def test_load_desired_broken_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.yaml"
    path.write_text("model: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML"):
        cg.load_desired(path)


def test_render_desired_round_trips_through_yaml(tmp_path: Path) -> None:
    text = cg.render_desired(_desired(model_reasoning_effort=""))
    assert "model_reasoning_effort: \n" in text  # empty is a valid "don't write it" state


def test_validate_catches_a_malformed_saved_yaml(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt.yaml"
    path.write_text("mode: zk-ai\nmodel: zk-auto\nwire_api: smoke-signals\n", encoding="utf-8")
    loaded = cg.load_desired(path)
    assert loaded is not None
    assert cg.validate(loaded)


# --------------------------------------------------------------------------- #
# auth.json: read-only, never written
# --------------------------------------------------------------------------- #
def test_apply_never_touches_auth_json(codex_dir: Path) -> None:
    (codex_dir / "config.toml").write_text(APP_OWNED_CONFIG, encoding="utf-8")
    auth = codex_dir / "auth.json"
    auth.write_text('{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-legit"}\n', encoding="utf-8")

    info_before = cg.read_auth_info(codex_dir)
    cg.apply_config(codex_dir / "config.toml", _desired())
    info_after = cg.read_auth_info(codex_dir)

    assert info_before == {"exists": True, "has_openai_key": True}
    assert info_after == info_before
    assert auth.read_text(encoding="utf-8") == '{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-legit"}\n'


def test_read_auth_info_missing(codex_dir: Path) -> None:
    assert cg.read_auth_info(codex_dir) == {"exists": False, "has_openai_key": False}


# --------------------------------------------------------------------------- #
# codex home resolution
# --------------------------------------------------------------------------- #
def test_codex_home_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZKAI_CODEX_HOME", str(tmp_path / "elsewhere"))
    assert cg.codex_home() == tmp_path / "elsewhere"
    assert cg.config_toml_path() == tmp_path / "elsewhere" / "config.toml"


# --------------------------------------------------------------------------- #
# Windows user-level environment variable
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only user env")
def test_read_user_env_var_reflects_the_registry() -> None:
    """桌面版能不能读到 Key，取决于用户级注册表——读函数就是这件事的答案。"""
    import winreg

    name = "ZKAI_TEST_CHATGPT_READ"
    original: str | None = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            try:
                original = str(winreg.QueryValueEx(key, name)[0])
            except FileNotFoundError:
                original = None

        assert cg.read_user_env_var(name) is None
        written, _, _ = cg.write_user_env_var(name, "read-me-123")
        assert written
        assert cg.read_user_env_var(name) == "read-me-123"
    finally:
        with (
            contextlib.suppress(FileNotFoundError),
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_WRITE) as key,
        ):
            winreg.DeleteValue(key, name)
        if original is not None:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, original)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only user env")
def test_read_user_env_var_missing_is_none() -> None:
    assert cg.read_user_env_var("ZKAI_TEST_DEFINITELY_NOT_SET_9F3A") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only user env")
def test_write_user_env_var_round_trip_and_restore_hint() -> None:
    import winreg

    name = "ZKAI_TEST_CHATGPT_SERVICE"
    original: str | None = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            try:
                original = str(winreg.QueryValueEx(key, name)[0])
            except FileNotFoundError:
                original = None

        written, old, note = cg.write_user_env_var(name, "test-value-123")
        assert written and old == original and note == ""
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            assert str(winreg.QueryValueEx(key, name)[0]) == "test-value-123"

        # empty value: refused, nothing written
        again, _, why = cg.write_user_env_var(name, "")
        assert again is False and why

        # restoring to the original state leaves no trace
        if original is None:
            hint = cg.env_restore_hint(name, None)
            assert "reg delete" in hint
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_WRITE) as key:
                winreg.DeleteValue(key, name)
        else:
            cg.write_user_env_var(name, original)
    finally:
        with (
            contextlib.suppress(FileNotFoundError),
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_WRITE) as key,
        ):
            winreg.DeleteValue(key, name)
        if original is not None:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, original)
