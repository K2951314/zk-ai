"""Export / import everything that makes this machine "the" ZK-AI machine.

Moving to a new computer is a recurring, high-stakes operation: the pieces that
must travel together (every API key in ``.env``, the operator-tuned
``config/*.yaml``) are deliberately untracked by git, and forgetting one of
them leaves the new machine with a gateway that starts but cannot call anyone.
The database (usage history, agent sessions, rate-limit ledger) is optional but
usually wanted - it is the biggest file and sometimes the point of the move.

Design:

* A transfer file is a plain zip archive (``.zip``) holding a small
  ``manifest.json`` plus the tracked content files. Plain zip because the
  only things a *target* machine can be assumed to have are this repo and a
  working ``.venv`` - no 7-Zip, no extra dependencies, and the venv's stdlib
  ``zipfile`` is enough.
* Secrets travel **encrypted at rest**: before zipping, every byte is wrapped
  in a self-contained XOR-obfuscation layer keyed by a passphrase the operator
  types (never stored). The cipher is intentionally simple and dependency-free
  - its only job is to stop the archive from being a plaintext bag of keys if
  a USB stick / cloud-sync folder leaks. It is NOT a substitute for real disk
  encryption on the machine itself.
* Export NEVER deletes anything and NEVER stops the gateway - it is a pure
  snapshot. Import backs up anything it is about to overwrite into
  ``imports_backup/<timestamp>/`` first, so a mistake is reversible.

Batch wrappers (``scripts/export_machine.cmd`` / ``scripts/import_machine.cmd``)
stay pure-ASCII: all Chinese text lives here.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

#: Manifest lives at this path inside every transfer archive.
_MANIFEST = "manifest.json"
#: Byte stream prepended to the payload so we can tell an encrypted archive
#: from a plain one (and refuse the wrong tool early, not after a corrupt import).
_MAGIC = b"ZKAI-MIGRATE\x00\x01"
#: KDF work factor for the passphrase. Deliberately moderate - the threat model
#: is "lost USB stick", not a nation state; the real lock is the passphrase.
_KDF_ROUNDS = 120_000


# --------------------------------------------------------------------------
# payload layout
# --------------------------------------------------------------------------

def _data_files() -> list[Path]:
    """Live data files worth migrating, relative to the data dir."""
    return [
        Path("data/zkai.db"),
        Path("data/burn_state.json"),
        Path("data/rate_limits.json"),
    ]


def _config_files() -> list[Path]:
    return [
        Path("config/config.yaml"),
        Path("config/providers.yaml"),
        Path("config/models.yaml"),
    ]


def _secrets_files() -> list[Path]:
    return [Path(".env")]


def _collect(root: Path) -> tuple[dict[str, Path], list[str]]:
    """Map archive-name -> absolute path for everything that exists.

    Returns (found, missing) where *missing* is human-readable notes.
    """
    found: dict[str, Path] = {}
    missing: list[str] = []
    for rel in [*_secrets_files(), *_config_files(), *_data_files()]:
        src = root / rel
        if src.exists():
            # Archive names are always forward-slash, always relative to root.
            found[rel.as_posix()] = src
        else:
            missing.append(str(rel))
    return found, missing


# --------------------------------------------------------------------------
# passphrase crypto (self-contained, dependency-free)
# --------------------------------------------------------------------------

def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, _KDF_ROUNDS, dklen=32
    )


def _keystream(key: bytes, length: int) -> bytes:
    """A deterministic byte stream from *key* - XOR mask for the payload."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(key + counter.to_bytes(8, "little")).digest())
        counter += 1
    return bytes(out[:length])


def encrypt_blob(plain: bytes, passphrase: str) -> bytes:
    """Wrap *plain* so a leaked archive is not a plaintext bag of keys.

    Layout: MAGIC | salt(16) | sha256(plain)(32) | cipher. The checksum makes
    a wrong passphrase fail loudly instead of decrypting to garbage.
    """
    salt = os.urandom(16)
    key = _derive_key(passphrase, salt)
    mask = _keystream(key, len(plain))
    cipher = bytes(a ^ b for a, b in zip(plain, mask, strict=True))
    digest = hashlib.sha256(plain).digest()
    return _MAGIC + salt + digest + cipher


def decrypt_blob(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(_MAGIC):
        raise ValueError("不是 ZK-AI 迁移包（缺少文件头）")
    offset = len(_MAGIC)
    salt = blob[offset: offset + 16]
    digest = blob[offset + 16: offset + 48]
    cipher = blob[offset + 48:]
    key = _derive_key(passphrase, salt)
    mask = _keystream(key, len(cipher))
    plain = bytes(a ^ b for a, b in zip(cipher, mask, strict=True))
    if hashlib.sha256(plain).digest() != digest:
        raise ValueError("密码不对，或迁移包已损坏（校验失败）。")
    return plain


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def _snapshot_database(root: Path, workdir: Path) -> Path | None:
    """Crash-consistent copy of the SQLite DB into *workdir*.

    Uses SQLite's own online backup so a gateway that is writing at this exact
    moment cannot hand us a half-committed page. Returns None when there is no
    database.
    """
    src = root / "data" / "zkai.db"
    if not src.exists():
        return None
    dst = workdir / "zkai.db"
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dst


def export_package(out_path: Path, passphrase: str, root: Path = _ROOT) -> dict:
    """Build the encrypted transfer archive. Returns the manifest."""
    found, missing = _collect(root)
    if ".env" not in found:
        raise SystemExit(
            "没找到 .env - 这台机器上没有要搬走的密钥。\n"
            "如果你只想搬配置，先随便建一个 .env 再导出，或手动拷 config/。"
        )

    tmp = _ROOT / ".migrate_tmp"
    tmp.mkdir(exist_ok=True)
    try:
        db_snapshot = _snapshot_database(root, tmp)
        files: dict[str, int] = {}
        manifest: dict[str, object] = {
            "tool": "zk-ai-migrate",
            "version": 1,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_machine": os.environ.get("COMPUTERNAME", "unknown"),
            "files": files,
            "missing": missing,
        }
        # Build the plaintext zip in memory-then-temp, then encrypt the bytes.
        plain_zip = tmp / "payload.zip"
        with zipfile.ZipFile(plain_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, src in found.items():
                if arcname == "data/zkai.db" and db_snapshot is not None:
                    zf.write(db_snapshot, arcname)
                else:
                    zf.write(src, arcname)
                files[arcname] = src.stat().st_size
            # manifest travels INSIDE the zip so the importer can verify the
            # payload and list what it is about to restore.
            zf.writestr(_MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        blob = encrypt_blob(plain_zip.read_bytes(), passphrase)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(blob)
        return manifest
    finally:
        # Never leave plaintext key material on disk longer than needed.
        for junk in tmp.glob("*"):
            with contextlib.suppress(OSError):
                junk.unlink()
        with contextlib.suppress(OSError):
            tmp.rmdir()


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

def _backup_existing(root: Path, targets: list[Path]) -> Path:
    """Copy everything we are about to overwrite into a timestamped folder."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = root / "imports_backup" / stamp
    for rel in targets:
        src = root / rel
        if src.exists():
            dst = backup / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    return backup


def import_package(
    archive: Path,
    passphrase: str,
    root: Path = _ROOT,
    *,
    overwrite: bool = False,
) -> tuple[list[str], Path | None]:
    """Restore an export archive into *root*.

    Returns (restored, backup_dir). With *overwrite* False, refuses to touch
    any file that already exists - first-import onto a fresh clone is the
    normal case, and overwriting an operator's live config should be explicit.
    """
    blob = archive.read_bytes()
    plain = decrypt_blob(blob, passphrase)

    tmp = _ROOT / ".migrate_tmp"
    tmp.mkdir(exist_ok=True)
    try:
        payload = tmp / "payload.zip"
        payload.write_bytes(plain)
        with zipfile.ZipFile(payload) as zf:
            names = set(zf.namelist())
            if _MANIFEST not in names:
                raise SystemExit("迁移包里缺 manifest.json - 文件可能损坏。")
            manifest = json.loads(zf.read(_MANIFEST).decode("utf-8"))
            to_restore = [
                Path(n) for n in manifest["files"]
                if n != _MANIFEST and not n.startswith("imports_backup/")
            ]

            conflicts = [rel for rel in to_restore if (root / rel).exists()]
            if conflicts and not overwrite:
                listing = "\n  ".join(str(c) for c in conflicts)
                raise SystemExit(
                    "目标机器上已有这些文件，直接覆盖有风险：\n  " + listing +
                    "\n\n确认要覆盖就加 --overwrite 再跑一次（旧文件会先备份）。"
                )

            backup = _backup_existing(root, to_restore) if conflicts else None
            restored: list[str] = []
            for rel in to_restore:
                dst = root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(zf.read(rel.as_posix()))
                restored.append(str(rel))
            return restored, backup
    finally:
        for junk in tmp.glob("*"):
            with contextlib.suppress(OSError):
                junk.unlink()
        with contextlib.suppress(OSError):
            tmp.rmdir()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _ask_passphrase(confirm: bool) -> str:
    """Prompt without echoing. Never accept an empty passphrase."""
    while True:
        pw = getpass.getpass("迁移密码（输入时不显示，回车结束）: ")
        if not pw:
            print("密码不能为空 - 这是保护你所有 Key 的唯一防线。")
            continue
        if confirm:
            again = getpass.getpass("再输一次确认: ")
            if pw != again:
                print("两次不一致，重来。")
                continue
        return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ZK-AI 一键换机：导出/导入全部密钥与配置",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="把本机的 Key/配置/数据打包成一个加密文件")
    p_exp.add_argument("--out", default=None,
                       help="输出路径（默认 exports/zkai-machine-时间戳.zip）")
    p_exp.add_argument("--passphrase", default=None,
                       help="迁移密码（不给则交互输入；脚本调用时才用）")

    p_imp = sub.add_parser("import", help="在新电脑上还原一个迁移包")
    p_imp.add_argument("archive", help="迁移包路径（.zip）")
    p_imp.add_argument("--passphrase", default=None)
    p_imp.add_argument("--overwrite", action="store_true",
                       help="允许覆盖已存在的文件（旧文件先备份到 imports_backup/）")

    args = parser.parse_args(argv)

    if args.cmd == "export":
        pw = args.passphrase or _ask_passphrase(confirm=True)
        if args.out:
            out = Path(args.out)
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out = _ROOT / "exports" / f"zkai-machine-{stamp}.zip"
        manifest = export_package(out, pw)
        print()
        print("导出完成。")
        print(f"  文件: {out}")
        print(f"  大小: {out.stat().st_size / 1024:.0f} KB（已加密）")
        print(f"  装了 {len(manifest['files'])} 个文件：")
        for name, size in manifest["files"].items():
            print(f"    - {name} ({size / 1024:.0f} KB)")
        if manifest["missing"]:
            print("  这些没找到（不影响，列出来让你心里有数）：")
            for name in manifest["missing"]:
                print(f"    - {name}")
        print()
        print("下一步：")
        print("  1. 把这个 .zip 拷到新电脑（U盘/网盘/局域网都行）")
        print("  2. 新电脑：git pull（或拷源码）→ 双击 scripts\\import_machine.cmd")
        print("  3. 把这个 .zip 拖进黑窗口，输入刚才的密码")
        return 0

    if args.cmd == "import":
        pw = args.passphrase or _ask_passphrase(confirm=False)
        archive = Path(args.archive)
        if not archive.exists():
            print(f"找不到迁移包: {archive}")
            return 1
        restored, backup = import_package(archive, pw, overwrite=args.overwrite)
        print()
        print("导入完成。")
        for name in restored:
            print(f"    + {name}")
        if backup:
            print(f"  被覆盖的旧文件已备份到: {backup}")
        print()
        print("下一步：")
        print("  1. 双击 scripts\\start_gateway.cmd 启动（缺 .venv 会自动重建）")
        print("  2. 浏览器打开 http://127.0.0.1:8317/health 看到 healthy 就成了")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
