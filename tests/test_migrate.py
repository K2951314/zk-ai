"""Tests for scripts/migrate.py - the machine-transfer packer.

The two failure modes that actually hurt:

* forgetting a file that has to travel (silent: gateway starts, no keys work);
* overwriting a live config on the target without a way back.

So the suite pins the payload list, the round-trip, the wrong-password path,
and the overwrite/backup guard. Since the package now also reconfigures the
ChatGPT/Codex client on import, every test runs with ``ZKAI_CODEX_HOME``
pointed at a temp dir - a migration test must never touch the developer's
real ``~/.codex``.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.services import chatgpt_service
from scripts import migrate


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The import auto-applies the client config - never to the real home."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("ZKAI_CODEX_HOME", str(home))
    return home


def _populate(root: Path, *, with_db: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / ".env").write_text("ZKAI_API_TOKEN=t\nSENSENOVA_API_KEY=sk-x\n", encoding="utf-8")
    (root / "config" / "providers.yaml").write_text("providers: []\n", encoding="utf-8")
    (root / "config" / "models.yaml").write_text("models: []\n", encoding="utf-8")
    (root / "config" / "config.yaml").write_text("app: {}\n", encoding="utf-8")
    (root / "config" / "chatgpt.yaml").write_text(
        "mode: zk-ai\nmodel: zk-auto\nmodel_provider: zkai\nprovider_display: ZK-AI\n"
        "base_url: http://127.0.0.1:8317/v1\nwire_api: responses\nenv_key: ZKAI_API_TOKEN\n"
        "model_reasoning_effort: max\nofficial_model: gpt-5.6-terra\n",
        encoding="utf-8",
    )
    if with_db:
        import sqlite3

        con = sqlite3.connect(root / "data" / "zkai.db")
        con.execute("create table t (id integer primary key, v text)")
        con.execute("insert into t (v) values ('hello')")
        con.commit()
        con.close()
    (root / "data" / "burn_state.json").write_text("{}\n", encoding="utf-8")
    (root / "data" / "rate_limits.json").write_text("{}\n", encoding="utf-8")


def _decrypt_zip_bytes(archive: Path, passphrase: str) -> bytes:
    return migrate.decrypt_blob(archive.read_bytes(), passphrase)


def test_export_contains_everything(tmp_path: Path) -> None:
    _populate(tmp_path)
    out = tmp_path / "pkg.zip"
    manifest = migrate.export_package(out, "pw", root=tmp_path)

    names = set(manifest["files"])
    assert names == {
        ".env",
        "config/config.yaml",
        "config/providers.yaml",
        "config/models.yaml",
        "config/chatgpt.yaml",
        "data/zkai.db",
        "data/burn_state.json",
        "data/rate_limits.json",
    }
    # Encrypted at rest: a hex dump of the archive shows no key material.
    blob = out.read_bytes()
    assert b"sk-x" not in blob
    assert blob.startswith(migrate._MAGIC)
    # And the decrypted payload is a readable zip with the right names.
    plain = _decrypt_zip_bytes(out, "pw")
    with zipfile.ZipFile(io.BytesIO(plain)) as zf:
        assert ".env" in zf.namelist()
        assert zf.read(".env").decode().count("SENSENOVA_API_KEY") == 1


def test_round_trip_restores_bytes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    _populate(dst)
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)

    restored, backup = migrate.import_package(archive, "pw", root=dst, overwrite=True)
    assert ".env" in restored
    assert (dst / ".env").read_text(encoding="utf-8") == (src / ".env").read_text(encoding="utf-8")
    assert backup is not None and backup.exists()


def test_wrong_passphrase_fails_closed(tmp_path: Path) -> None:
    _populate(tmp_path)
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "right", root=tmp_path)
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        _decrypt_zip_bytes(archive, "wrong")


def test_import_refuses_to_clobber_without_flag(tmp_path: Path, capsys) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    _populate(dst)
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)

    with pytest.raises(SystemExit) as exc:
        migrate.import_package(archive, "pw", root=dst, overwrite=False)

    # 3, not 1: import_machine.cmd offers the --overwrite retry for this code only.
    assert exc.value.code == migrate._EXIT_CONFLICTS
    assert "--overwrite" in capsys.readouterr().out


def test_import_backs_up_before_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    _populate(dst)
    (dst / ".env").write_text("OLD=1\n", encoding="utf-8")
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)

    _, backup = migrate.import_package(archive, "pw", root=dst, overwrite=True)
    assert backup is not None
    backed = list(backup.rglob(".env"))
    assert len(backed) == 1
    assert backed[0].read_text(encoding="utf-8") == "OLD=1\n"


def test_export_without_env_is_a_clear_error(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "providers.yaml").write_text("providers: []\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"\.env"):
        migrate.export_package(tmp_path / "pkg.zip", "pw", root=tmp_path)


# --------------------------------------------------------------------------- #
# The database's SQLite side-cars
# --------------------------------------------------------------------------- #
def _add_stale_sidecars(root: Path) -> None:
    """A -wal/-shm pair left behind by the database that is about to be replaced."""
    (root / "data" / "zkai.db-wal").write_bytes(b"foreign wal frames")
    (root / "data" / "zkai.db-shm").write_bytes(b"foreign shm index")


def test_import_clears_stale_wal_sidecars(tmp_path: Path) -> None:
    """Restoring zkai.db without removing the old -wal corrupts the new database.

    SQLite replays whatever -wal it finds next to the file, so a WAL from the
    previous generation points the fresh database at pages it does not have -
    the next open reports "database disk image is malformed".
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    _populate(dst)
    _add_stale_sidecars(dst)
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)

    _, backup = migrate.import_package(archive, "pw", root=dst, overwrite=True)

    assert not (dst / "data" / "zkai.db-wal").exists()
    assert not (dst / "data" / "zkai.db-shm").exists()
    assert backup is not None
    # Backed up before deletion: removing them is correct, discarding them is not.
    assert (backup / "data" / "zkai.db-wal").read_bytes() == b"foreign wal frames"
    assert (backup / "data" / "zkai.db-shm").read_bytes() == b"foreign shm index"


def test_import_without_a_database_leaves_sidecars_alone(tmp_path: Path) -> None:
    """A keys-only package never touches the database, so its side-cars stay put."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src, with_db=False)
    _populate(dst)
    _add_stale_sidecars(dst)
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)

    migrate.import_package(archive, "pw", root=dst, overwrite=True)

    assert (dst / "data" / "zkai.db-wal").exists()
    assert (dst / "data" / "zkai.db").exists()


def test_cli_refuses_to_import_while_the_gateway_is_running(tmp_path: Path, monkeypatch,
                                                            capsys) -> None:
    """Overwriting an open database is how the WAL stops matching the file."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    _populate(dst)
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)
    (dst / ".env").write_text("ZKAI_API_TOKEN=untouched\n", encoding="utf-8")

    monkeypatch.setattr(migrate, "_ROOT", dst)
    monkeypatch.setattr(migrate, "_gateway_is_listening", lambda *a, **k: True)
    monkeypatch.setattr(migrate, "_ask_passphrase", lambda confirm=False: "pw")

    assert migrate.main(["import", str(archive), "--overwrite"]) == 1
    assert "网关正在端口" in capsys.readouterr().out
    assert (dst / ".env").read_text(encoding="utf-8") == "ZKAI_API_TOKEN=untouched\n"


def test_cli_propagates_the_conflict_exit_code(tmp_path: Path, monkeypatch) -> None:
    """``import_machine.cmd`` branches on exit code 3 to offer --overwrite."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    _populate(dst)
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)
    monkeypatch.setattr(migrate, "_ROOT", dst)
    # Pin the probe: a gateway that happens to be running on this machine would
    # otherwise make every CLI test take the "stop the gateway first" branch.
    monkeypatch.setattr(migrate, "_gateway_is_listening", lambda *a, **k: False)

    with pytest.raises(SystemExit) as exc:
        migrate.main(["import", str(archive), "--passphrase", "pw"])

    assert exc.value.code == migrate._EXIT_CONFLICTS


def test_cli_reports_a_wrong_passphrase_without_a_traceback(tmp_path: Path, monkeypatch,
                                                            capsys) -> None:
    """The manual promises a sentence; an uncaught ValueError printed a stack."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    _populate(dst)
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)
    (dst / ".env").write_text("ZKAI_API_TOKEN=untouched\n", encoding="utf-8")
    monkeypatch.setattr(migrate, "_ROOT", dst)
    monkeypatch.setattr(migrate, "_gateway_is_listening", lambda *a, **k: False)

    code = migrate.main(["import", str(archive), "--passphrase", "wrong"])

    assert code == migrate._EXIT_BAD_PASSPHRASE
    out = capsys.readouterr().out
    assert "导入失败" in out and "密码不对" in out
    assert (dst / ".env").read_text(encoding="utf-8") == "ZKAI_API_TOKEN=untouched\n"


def test_gateway_port_comes_from_env_file(tmp_path: Path, monkeypatch) -> None:
    """A double-click exports no ZKAI_PORT, so .env has to answer for it."""
    (tmp_path / ".env").write_text('ZKAI_PORT="8418"\n', encoding="utf-8")
    monkeypatch.setattr(migrate, "_ROOT", tmp_path)
    monkeypatch.delenv("ZKAI_PORT", raising=False)
    probed: list[int] = []

    def fake_connect(addr, timeout=None):
        probed.append(addr[1])
        raise OSError("connection refused")

    monkeypatch.setattr(migrate.socket, "create_connection", fake_connect)

    assert migrate._gateway_is_listening() is False
    assert probed == [8418]


# --------------------------------------------------------------------------- #
# ChatGPT / Codex client auto-configuration on the new machine
# --------------------------------------------------------------------------- #
def _forbid_env_write(*_args):
    """Guard: provisioning must not touch the real user environment in tests."""
    raise AssertionError("write_user_env_var must not run here")


_APP_OWNED_CODEX_CONFIG = """\
model = "zk-auto"
model_provider = "zkai"

[model_providers.zkai]
name = "ZK-AI"
base_url = "http://127.0.0.1:8317/v1"
wire_api = "responses"
env_key = "ZKAI_API_TOKEN"

[mcp_servers.node_repl]
command = 'C:\\Users\\me\\.codex\\node_repl.exe'

[projects.'d:\\work\\zk-ai']
trust_level = "trusted"
"""


def test_import_auto_configures_the_chatgpt_client(tmp_path, isolated_codex_home, capsys) -> None:
    """The point of a machine move: sit down and it just works."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    _populate(dst)
    # the target machine already has the app installed, with its own config
    codex = isolated_codex_home
    (codex / "config.toml").write_text(_APP_OWNED_CODEX_CONFIG, encoding="utf-8")
    # and the package asks for a different model + port
    (src / "config" / "chatgpt.yaml").write_text(
        "mode: zk-ai\nmodel: glm-5.3\nmodel_provider: zkai\nprovider_display: ZK-AI\n"
        "base_url: http://127.0.0.1:9000/v1\nwire_api: responses\n"
        "env_key: ZKAI_API_TOKEN\nmodel_reasoning_effort: high\n",
        encoding="utf-8",
    )
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)

    restored, _backup = migrate.import_package(archive, "pw", root=dst, overwrite=True)

    # restored 里是 Path 字符串，Windows 上是反斜杠——两种分隔符都算
    assert any(r.replace("\\", "/") == "config/chatgpt.yaml" for r in restored)
    text = (codex / "config.toml").read_text(encoding="utf-8")
    assert 'model = "glm-5.3"' in text
    assert "http://127.0.0.1:9000/v1" in text
    assert 'model_reasoning_effort = "high"' in text
    # app-owned sections survived
    assert "[mcp_servers.node_repl]" in text
    assert '[projects.\'d:\\work\\zk-ai\']' in text
    # the pre-write file was backed up
    assert len(list(codex.glob("config.toml.bak-*"))) == 1
    assert "客户端已自动配置" in capsys.readouterr().out


def test_import_configures_a_fresh_machine_without_the_app(tmp_path, isolated_codex_home) -> None:
    """No ~/.codex yet: create the minimal config so the first app start works."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    _populate(dst)
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)

    migrate.import_package(archive, "pw", root=dst, overwrite=True)

    cfg_file = isolated_codex_home / "config.toml"
    assert cfg_file.exists()
    data = chatgpt_service.read_config_toml(cfg_file)
    assert data["model"] == "zk-auto"
    assert data["model_providers"]["zkai"]["base_url"] == "http://127.0.0.1:8317/v1"


def test_import_without_chatgpt_yaml_leaves_the_client_alone(
    tmp_path, isolated_codex_home
) -> None:
    """A keys-only package (no chatgpt.yaml) must not touch the client config."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    (src / "config" / "chatgpt.yaml").unlink()
    _populate(dst)
    codex = isolated_codex_home
    (codex / "config.toml").write_text(_APP_OWNED_CODEX_CONFIG, encoding="utf-8")
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)

    migrate.import_package(archive, "pw", root=dst, overwrite=True)

    assert (codex / "config.toml").read_text(encoding="utf-8") == _APP_OWNED_CODEX_CONFIG
    assert not list(codex.glob("config.toml.bak-*"))


def test_import_with_broken_chatgpt_yaml_warns_and_skips(
    tmp_path, isolated_codex_home, capsys
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    (src / "config" / "chatgpt.yaml").write_text("mode: [broken\n", encoding="utf-8")
    _populate(dst)
    codex = isolated_codex_home
    (codex / "config.toml").write_text(_APP_OWNED_CODEX_CONFIG, encoding="utf-8")
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)

    migrate.import_package(archive, "pw", root=dst, overwrite=True)

    assert "跳过客户端自动配置" in capsys.readouterr().out
    assert (codex / "config.toml").read_text(encoding="utf-8") == _APP_OWNED_CODEX_CONFIG


def test_provision_chatgpt_env_writes_user_env_with_backup(
    tmp_path, monkeypatch, capsys
) -> None:
    """The last link of "just works": the desktop app reads env_key from the
    user environment. The real HKCU is never touched - the writer is patched."""
    root = tmp_path / "root"
    _populate(root)
    monkeypatch.setattr(migrate, "_ROOT", root)
    calls: list[tuple[str, str]] = []

    def fake_write(name: str, value: str):
        calls.append((name, value))
        return True, "old-token", ""

    monkeypatch.setattr(chatgpt_service, "write_user_env_var", fake_write)
    monkeypatch.setattr(chatgpt_service, "read_user_env_var", lambda name: "t")

    migrate._provision_chatgpt_env(root, [".env", "config/chatgpt.yaml"])

    assert calls == [("ZKAI_API_TOKEN", "t")]
    out = capsys.readouterr().out
    assert "已写入用户级环境变量" in out and "还原命令" in out
    assert "校验通过" in out
    backup = next(root.glob("imports_backup/*/chatgpt_user_env.json"))
    assert json.loads(backup.read_text(encoding="utf-8"))["ZKAI_API_TOKEN"] == "old-token"


def test_provision_chatgpt_env_skips_without_a_token(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "root"
    _populate(root)
    (root / ".env").write_text("SENSENOVA_API_KEY=sk-x\n", encoding="utf-8")
    monkeypatch.setattr(migrate, "_ROOT", root)
    monkeypatch.setattr(chatgpt_service, "write_user_env_var", _forbid_env_write)

    migrate._provision_chatgpt_env(root, [".env", "config/chatgpt.yaml"])

    out = capsys.readouterr().out  # readouterr() 会清空缓冲，只能读一次
    assert "ZKAI_API_TOKEN" in out
    # 新版不再静默跳过：把桌面版会看到的那句报错和两条修复路径原样给出
    assert "Missing environment variable: ZKAI_API_TOKEN" in out
    assert "setx ZKAI_API_TOKEN" in out


def test_provision_chatgpt_env_noop_when_not_in_package(tmp_path, monkeypatch) -> None:
    """没有 .env 的包（纯配置包）不动用户级环境变量。"""
    root = tmp_path / "root"
    _populate(root)
    monkeypatch.setattr(chatgpt_service, "write_user_env_var", _forbid_env_write)
    migrate._provision_chatgpt_env(root, ["config/models.yaml", "config/providers.yaml"])


def test_provision_chatgpt_env_works_without_chatgpt_yaml(tmp_path, isolated_codex_home,
                                                          monkeypatch, capsys) -> None:
    """2026-09-22 换机事故回归：源机没存过 chatgpt.yaml，操作员把旧机器的
    ~/.codex 整个手抄过来——config.toml 里的 env_key 照样要 provision。
    门控是 .env（不是 chatgpt.yaml），变量名以磁盘 config.toml 为第一真相。"""
    root = tmp_path / "root"
    _populate(root)
    (root / ".env").write_text("ZKAI_API_TOKEN=t\nMY_CODEX_KEY=my-secret\n", encoding="utf-8")
    (isolated_codex_home / "config.toml").write_text(
        'model = "zk-auto"\nmodel_provider = "zkai"\n\n'
        "[model_providers.zkai]\n"
        'base_url = "http://127.0.0.1:8317/v1"\nwire_api = "responses"\n'
        'env_key = "MY_CODEX_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(migrate, "_ROOT", root)
    calls: list[tuple[str, str]] = []

    def fake_write(name: str, value: str):
        calls.append((name, value))
        return True, None, ""

    monkeypatch.setattr(chatgpt_service, "write_user_env_var", fake_write)
    monkeypatch.setattr(chatgpt_service, "read_user_env_var", lambda name: "my-secret")

    migrate._provision_chatgpt_env(root, [".env", "config/models.yaml"])

    assert calls == [("MY_CODEX_KEY", "my-secret")]  # 认磁盘上的名字，不是默认名
    out = capsys.readouterr().out
    assert "已写入用户级环境变量 MY_CODEX_KEY" in out
    assert "校验通过" in out


def test_provision_chatgpt_env_verifies_by_reading_back(tmp_path, isolated_codex_home,
                                                        monkeypatch, capsys) -> None:
    """写成功但回读不一致=桌面版照样报错，必须大声 fail。"""
    root = tmp_path / "root"
    _populate(root)
    monkeypatch.setattr(migrate, "_ROOT", root)
    monkeypatch.setattr(
        chatgpt_service, "write_user_env_var", lambda name, value: (True, "old", "")
    )
    monkeypatch.setattr(chatgpt_service, "read_user_env_var", lambda name: "something-else")

    migrate._provision_chatgpt_env(root, [".env"])

    out = capsys.readouterr().out
    assert "回读校验失败" in out
    assert "setx ZKAI_API_TOKEN" in out  # 给出可执行的手动修复命令


def test_provision_chatgpt_env_warns_when_token_missing(tmp_path, isolated_codex_home,
                                                        monkeypatch, capsys) -> None:
    """.env 里没有 env_key 指向的变量时，把桌面版会看到的那句报错原样给出。"""
    root = tmp_path / "root"
    _populate(root)
    (root / ".env").write_text("SENSENOVA_API_KEY=sk-x\n", encoding="utf-8")
    monkeypatch.setattr(migrate, "_ROOT", root)
    monkeypatch.setattr(chatgpt_service, "write_user_env_var", _forbid_env_write)

    migrate._provision_chatgpt_env(root, [".env"])

    out = capsys.readouterr().out
    assert "Missing environment variable: ZKAI_API_TOKEN" in out
    assert "setx ZKAI_API_TOKEN" in out

