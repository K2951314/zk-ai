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


def test_import_refuses_to_clobber_without_flag(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _populate(src)
    _populate(dst)
    archive = tmp_path / "pkg.zip"
    migrate.export_package(archive, "pw", root=src)

    with pytest.raises(SystemExit, match="--overwrite"):
        migrate.import_package(archive, "pw", root=dst, overwrite=False)


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
