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
    (root / "config" / "burner.yaml").write_text(
        "model: sensenova-6.8-flash-lite\nrate_out: 2500\n", encoding="utf-8")
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
        "config/burner.yaml",
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


def test_provision_survives_a_registry_write_failure(tmp_path, isolated_codex_home,
                                                     monkeypatch, capsys) -> None:
    """注册表写失败（权限/被锁）不该让恢复完文件的导入带上 traceback。"""
    root = tmp_path / "root"
    _populate(root)

    def boom(name: str, value: str):
        raise OSError("registry is locked")

    monkeypatch.setattr(chatgpt_service, "write_user_env_var", boom)
    monkeypatch.setattr(migrate, "_ROOT", root)

    migrate._provision_chatgpt_env(root, [".env"])  # 不抛异常即通过

    out = capsys.readouterr().out
    assert "写用户级环境变量 ZKAI_API_TOKEN 失败" in out
    assert "setx ZKAI_API_TOKEN" in out  # 给出可手动执行的修复


# --------------------------------------------------------------------------- #
# ChatGPT / Codex client auto-configuration on the new machine
# --------------------------------------------------------------------------- #
def _forbid_env_write(*_args):
    """Guard: provisioning must not touch the real user environment in tests."""
    raise AssertionError("write_user_env_var must not run here")


def test_apply_chatgpt_client_skips_an_unparseable_config(tmp_path, isolated_codex_home,
                                                           capsys) -> None:
    """桌面版自己的 config.toml 坏了（tomllib 都读不了）：告警跳过，
    绝不猜着改，更不让整个导入崩掉。"""
    src = tmp_path / "src"
    _populate(src)
    broken = "model = [unclosed\n"
    (isolated_codex_home / "config.toml").write_text(broken, encoding="utf-8")

    migrate._apply_chatgpt_client(src, ["config/chatgpt.yaml", ".env"])

    assert (isolated_codex_home / "config.toml").read_text(encoding="utf-8") == broken
    assert "未写入客户端配置" in capsys.readouterr().out


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


# --------------------------------------------------------------------------- #
# 积分消耗器配置必须跟着换机
# --------------------------------------------------------------------------- #
def test_burner_config_travels_with_the_package(tmp_path: Path) -> None:
    """烧哪些 Key、每账号锚点、费率都在 config/burner.yaml 里。不带它，新机的
    消耗器只会用代码默认值——费率退回低估 7 倍的旧默认，锚点全丢，专属池被烧穿
    只是时间问题。"""
    _populate(tmp_path)
    out = tmp_path / "pkg.zip"
    migrate.export_package(out, "pw", root=tmp_path)

    target = tmp_path / "new-machine"
    target.mkdir()
    restored, _backup = migrate.import_package(out, "pw", root=target)

    burner = target / "config" / "burner.yaml"
    assert burner.exists()
    assert "sensenova-6.8-flash-lite" in burner.read_text(encoding="utf-8")
    assert "config/burner.yaml" in [r.replace("\\", "/") for r in restored]


def test_import_asks_the_tray_to_restart_the_burner(tmp_path: Path) -> None:
    """消耗器只在启动时读配置：换了 burner.yaml / burn_state.json 之后，运行中的
    实例还在用旧值。导入必须留一个重启请求，托盘心跳看到才会重启。"""
    _populate(tmp_path)
    out = tmp_path / "pkg.zip"
    migrate.export_package(out, "pw", root=tmp_path)

    target = tmp_path / "new-machine"
    target.mkdir()
    migrate.import_package(out, "pw", root=target)
    assert (target / "data" / "burner_restart.request").exists()


# --------------------------------------------------------------------------- #
# .env 换行损坏：两天内踩了两次同一个坑（2026-09-24 / 2026-09-26），必须有人拦
# --------------------------------------------------------------------------- #


def _sicken_env(root: Path) -> None:
    """Give .env the exact damage seen twice: every line ends CR CR LF.

    按字节写，不用 write_text：后者 newline=None 会把 \n 按平台转成 \r\n，
    文件里就没剩下可替换的裸 \n 了（第一版 helper 就这么静默失效：
    替换目标是字面反斜杠文本而非换行符，改完双CR=0、LF=0，测试全绿但啥也没测）。
    """
    path = root / ".env"
    body = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    sick = "\r\r\n".join(body.split("\n"))
    path.write_bytes(sick.encode("utf-8"))


def test_env_health_check_flags_doubled_cr(tmp_path: Path) -> None:
    """坏 .env 必须被认出来——这是整个修复的判据。"""
    from scripts.migrate import _check_env_health, _env_newline_problem

    healthy = tmp_path / "healthy"
    healthy.mkdir()
    (healthy / ".env").write_bytes(b"ZKAI_HOST=0.0.0.0\nZKAI_PORT=8317\n")
    assert _env_newline_problem((healthy / ".env").read_bytes()) == (0, 2)
    assert _check_env_health(healthy, stage="测试") == []

    sick = tmp_path / "sick"
    sick.mkdir()
    (sick / ".env").write_bytes(b"ZKAI_HOST=0.0.0.0\r\r\nZKAI_PORT=8317\r\r\n")
    doubled, lf = _env_newline_problem((sick / ".env").read_bytes())
    assert doubled == 2 and doubled == lf, "全文件损坏的判据就是这两个数相等"
    problems = _check_env_health(sick, stage="导出前：这份 .env 的损坏")
    assert problems and "\\r\\r\\n" in problems[0]
    # 空目录不能误报
    assert _check_env_health(tmp_path / "nope", stage="测试") == []


def test_export_warns_about_a_damaged_env(tmp_path: Path, capsys) -> None:
    """导出坏 .env 前必须警告——不拦住，损坏就被原样搬去新机器。"""
    from scripts.migrate import export_package

    root = tmp_path / "src"
    _populate(root)
    _sicken_env(root)
    export_package(tmp_path / "p.zip", "pw", root=root)

    out = capsys.readouterr().out
    assert "\\r\\r\\n" in out and "getaddrinfo failed" in out


def test_round_trip_preserves_the_newline_damage(tmp_path: Path, capsys) -> None:
    """导入端也要能发现：包可能是在这道检查存在之前打的。"""
    from scripts.migrate import export_package, import_package

    src = tmp_path / "src"
    _populate(src)
    _sicken_env(src)
    export_package(tmp_path / "p.zip", "pw", root=src)

    dst = tmp_path / "dst"
    dst.mkdir()
    import_package(tmp_path / "p.zip", "pw", root=dst, overwrite=True)

    out = capsys.readouterr().out
    # 导出与导入两侧各警告一次
    assert out.count("\\r\\r\\n") >= 2
    raw = (dst / ".env").read_bytes()
    doubled, lf = raw.count(b"\r\r"), raw.count(b"\n")
    assert doubled == lf and doubled > 0, "字节流是原样搬过去的（修不修由人决定）"


def test_healthy_env_produces_no_warning(tmp_path: Path, capsys) -> None:
    """正常 .env 不该有任何噪音——否则这道检查会被训练成「直接忽略」。"""
    from scripts.migrate import export_package

    root = tmp_path / "src"
    _populate(root)
    export_package(tmp_path / "p.zip", "pw", root=root)

    out = capsys.readouterr().out
    assert "\\r\\r\\n" not in out


# --------------------------------------------------------------------------- #
# credential keys that exist only in the OS environment (2026-09-29)
# --------------------------------------------------------------------------- #
def _providers_with(env_name: str, credential_id: str = "sensenova-01") -> str:
    return (
        "providers:\n"
        "  - id: sensenova\n"
        "    type: openai\n"
        "    base_url: https://example.invalid/v1\n"
        "    credentials:\n"
        f"      - id: {credential_id}\n"
        f"        env: {env_name}\n"
    )


def test_export_warns_about_a_key_that_only_lives_in_the_os_environment(
    tmp_path, capsys, monkeypatch
) -> None:
    """The real case: SENSENOVA_API_KEY is set at the user level, not in .env.

    The source machine works perfectly, which is why this never shows up until
    the move — and then the new machine has a DISABLED credential and no clue.
    """
    from scripts.migrate import export_package

    root = tmp_path / "src"
    _populate(root)
    (root / "config" / "providers.yaml").write_text(
        _providers_with("SENSENOVA_API_KEY"), encoding="utf-8"
    )
    (root / ".env").write_text("ZKAI_API_TOKEN=t\n", encoding="utf-8")
    monkeypatch.setenv("SENSENOVA_API_KEY", "os-level-secret-value")

    export_package(tmp_path / "pkg.zip", "pw", root=root)

    out = capsys.readouterr().out
    assert "SENSENOVA_API_KEY" in out
    assert "sensenova-01" in out          # 说的是哪把凭据，不只是变量名
    assert "OS 环境变量" in out
    assert "os-level-secret-value" not in out, "密钥永不回显"


def test_import_warns_on_the_receiving_machine_too(
    tmp_path, capsys, monkeypatch
) -> None:
    """导入端再说一次：重跑导入救不回来，必须回源机器补 .env。"""
    from scripts.migrate import export_package, import_package

    src = tmp_path / "src"
    _populate(src)
    (src / "config" / "providers.yaml").write_text(
        _providers_with("SENSENOVA_API_KEY"), encoding="utf-8"
    )
    (src / ".env").write_text("ZKAI_API_TOKEN=t\n", encoding="utf-8")
    monkeypatch.setenv("SENSENOVA_API_KEY", "os-level-secret-value")
    archive = tmp_path / "pkg.zip"
    export_package(archive, "pw", root=src)

    monkeypatch.delenv("SENSENOVA_API_KEY", raising=False)
    dst = tmp_path / "dst"
    dst.mkdir()
    import_package(archive, "pw", root=dst, overwrite=True)

    out = capsys.readouterr().out
    assert out.count("SENSENOVA_API_KEY") >= 2, "导出与导入两侧各说一次"
    assert "重跑导入也拿不回来" in out


def test_a_key_declared_in_env_file_is_not_a_problem(tmp_path, capsys, monkeypatch) -> None:
    """正常机器不许有噪音，否则这道警告会被训练成「直接忽略」。"""
    from scripts.migrate import export_package

    root = tmp_path / "src"
    _populate(root)  # _populate 的 .env 里就有 SENSENOVA_API_KEY
    (root / "config" / "providers.yaml").write_text(
        _providers_with("SENSENOVA_API_KEY"), encoding="utf-8"
    )
    monkeypatch.delenv("SENSENOVA_API_KEY", raising=False)

    export_package(tmp_path / "pkg.zip", "pw", root=root)

    assert "不在 .env 里" not in capsys.readouterr().out


def test_a_key_missing_everywhere_is_reported_as_already_disabled(
    tmp_path, capsys, monkeypatch
) -> None:
    """两边都没有 = 这台机器上它已经是 DISABLED，导出帮不了它（信息不同）。"""
    from scripts.migrate import export_package

    root = tmp_path / "src"
    _populate(root)
    (root / "config" / "providers.yaml").write_text(
        _providers_with("NOWHERE_KEY"), encoding="utf-8"
    )
    root.joinpath(".env").write_text("ZKAI_API_TOKEN=t\n", encoding="utf-8")
    monkeypatch.delenv("NOWHERE_KEY", raising=False)

    export_package(tmp_path / "pkg.zip", "pw", root=root)

    out = capsys.readouterr().out
    assert "里都没有" in out
    assert "已经是 DISABLED" in out


def test_burner_only_names_are_scanned_too(tmp_path, capsys, monkeypatch) -> None:
    """消耗器烧的 Key 同样要跟着换机，只查 providers 会漏掉它。"""
    from scripts.migrate import export_package

    root = tmp_path / "src"
    _populate(root)
    (root / "config" / "burner.yaml").write_text(
        "model: sensenova-6.8-flash-lite\nonly:\n  - BURN_KEY_A\n  - BURN_KEY_B\n",
        encoding="utf-8",
    )
    root.joinpath(".env").write_text("ZKAI_API_TOKEN=t\nBURN_KEY_A=x\n", encoding="utf-8")
    monkeypatch.delenv("BURN_KEY_A", raising=False)
    monkeypatch.delenv("BURN_KEY_B", raising=False)

    export_package(tmp_path / "pkg.zip", "pw", root=root)

    out = capsys.readouterr().out
    assert "BURN_KEY_B" in out
    assert "burner.yaml:only" in out
    assert "BURN_KEY_A" not in out, "在 .env 里的不该被点名"


def test_commented_and_indirection_lines_do_not_invent_names(tmp_path) -> None:
    """注释里的 env: 和 ``env: ${...}`` 都不能变成假名字——假警告比没有更糟。"""
    from scripts.migrate import _credential_env_refs

    root = tmp_path / "src"
    root.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "providers.yaml").write_text(
        "providers:\n"
        "  - id: p\n"
        "    type: openai\n"
        "    base_url: https://example.invalid/v1\n"
        "    credentials:\n"
        "      # - id: ghost\n"
        "      #   env: GHOST_KEY\n"
        "      - id: real\n"
        "        env: ${REAL_KEY}\n",
        encoding="utf-8",
    )

    assert _credential_env_refs(root) == {"REAL_KEY": ["providers.yaml:real"]}


def test_the_scan_agrees_with_the_config_layer_on_the_real_repo() -> None:
    """两个实现给出同一组名字，否则其中一个在说谎而没人知道。

    migrate 用的是文本扫描（配置坏了也要能导出），app 层用的是
    ``CredentialConfig.env_reference()``（真解析）。约束在这儿钉住。
    """
    from app.core.config import PROJECT_ROOT, credential_env_gaps, load_app_config
    from scripts.migrate import _credential_env_refs

    root = Path(PROJECT_ROOT)
    if not (root / "config" / "providers.yaml").is_file():
        pytest.skip("no local config/providers.yaml")

    scanned = set(_credential_env_refs(root))
    parsed = set(credential_env_gaps(load_app_config(), root / ".env"))
    parsed |= {
        c.env_reference().removeprefix("${").removesuffix("}")
        for p in load_app_config().providers.values()
        for c in p.credentials
        if c.env_reference()
    }

    assert scanned == parsed, "文本扫描与真解析不一致——有一侧已经看不懂现在的配置了"


def test_two_exports_at_once_do_not_corrupt_each_other(tmp_path) -> None:
    """固定路径的 .migrate_tmp 会让并发导出互删中间文件（2026-09-29 实测）。

    症状不是报错，而是导入端 ``BadZipFile: File is not a zip file``——
    看起来像密码错了。用两个真线程并发导出，两次的包都必须能重新解开。
    """
    import threading
    import zipfile

    src = tmp_path / "src"
    _populate(src)
    archives = [tmp_path / "a.zip", tmp_path / "b.zip"]
    failures: list[BaseException] = []

    def run(target: Path) -> None:
        try:
            migrate.export_package(target, "pw", root=src)
        except BaseException as exc:  # 收集给主线程断言
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(a,)) for a in archives]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not failures, f"并发导出失败：{failures}"
    for archive in archives:
        assert archive.stat().st_size > 0
        with zipfile.ZipFile(io.BytesIO(migrate.decrypt_blob(archive.read_bytes(), "pw"))) as zf:
            assert zf.testzip() is None
            assert migrate._MANIFEST in zf.namelist()
    assert not list(src.parent.glob(".migrate_tmp*")), "临时目录必须清干净"
