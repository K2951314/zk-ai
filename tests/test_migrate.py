"""Tests for scripts/migrate.py - the machine-transfer packer.

The two failure modes that actually hurt:

* forgetting a file that has to travel (silent: gateway starts, no keys work);
* overwriting a live config on the target without a way back.

So the suite pins the payload list, the round-trip, the wrong-password path,
and the overwrite/backup guard.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from scripts import migrate


def _populate(root: Path, *, with_db: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / ".env").write_text("ZKAI_API_TOKEN=t\nSENSENOVA_API_KEY=sk-x\n", encoding="utf-8")
    (root / "config" / "providers.yaml").write_text("providers: []\n", encoding="utf-8")
    (root / "config" / "models.yaml").write_text("models: []\n", encoding="utf-8")
    (root / "config" / "config.yaml").write_text("app: {}\n", encoding="utf-8")
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

