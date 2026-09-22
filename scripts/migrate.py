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
import socket
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

#: Process exit codes. The batch wrappers branch on these, so they are part of
#: the tool's contract: ``import_machine.cmd`` only offers the ``--overwrite``
#: retry for :data:`_EXIT_CONFLICTS`, and never for a wrong passphrase.
_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_BAD_PASSPHRASE = 2
_EXIT_CONFLICTS = 3


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


#: SQLite side-car files, which belong to whatever database generation is
#: currently on disk and are deliberately NOT part of the payload (export takes
#: a clean snapshot through the backup API, so there is nothing to replay).
#:
#: They still have to be *dealt with* on the way in: restoring ``data/zkai.db``
#: while an old ``-wal`` sits next to it makes the next open replay frames from
#: a different database onto the new one. That is not a warning, it is
#: corruption - it cost this project its ``requests`` / ``usage_records``
#: history when an archive was re-imported over a live checkout. Import
#: therefore moves them into the backup folder and removes them.
_DB_SIDECARS = ("data/zkai.db-wal", "data/zkai.db-shm")


def _config_files() -> list[Path]:
    return [
        Path("config/config.yaml"),
        Path("config/providers.yaml"),
        Path("config/models.yaml"),
        # ChatGPT / Codex 桌面版期望配置：导入后自动写入新机 ~/.codex，
        # 「换机后上来就能用」的客户端一环（见 _apply_chatgpt_client）。
        Path("config/chatgpt.yaml"),
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


def _apply_chatgpt_client(root: Path, restored: list[str]) -> None:
    """Reconfigure the ChatGPT/Codex client on the NEW machine.

    The point of a machine move is "it just works when you sit down": the
    package carries ``config/chatgpt.yaml`` (the *desired* client config, never
    the app-owned file itself), and we re-materialise it here as a surgical
    patch of ``~/.codex/config.toml`` - old file backed up as ``.bak-<stamp>``,
    mcp_servers/plugins/projects sections untouched. On a machine without the
    app installed yet, the minimal config is created so the first app start
    already points at this gateway.
    """
    if "config/chatgpt.yaml" not in restored:
        return
    from app.services import chatgpt_service

    try:
        desired = chatgpt_service.load_desired(root / "config" / "chatgpt.yaml")
    except ValueError as exc:
        print(f"  [警告] config/chatgpt.yaml 读不了，跳过客户端自动配置：{exc}")
        return
    if desired is None:  # pragma: no cover - the file was in the manifest
        return
    errors = chatgpt_service.validate(desired)
    if errors:
        print("  [警告] chatgpt.yaml 期望配置有问题，未写入客户端：" + "；".join(errors))
        return
    cfg_file = chatgpt_service.config_toml_path()
    try:
        applied = chatgpt_service.apply_config(cfg_file, desired)
    except OSError as exc:
        print(f"  [警告] 写 {cfg_file} 失败（不影响网关本身）：{exc}")
        return
    if applied.no_op:
        print(f"  ChatGPT/Codex 客户端配置已是最新：{cfg_file}")
        return
    print(f"  ChatGPT/Codex 客户端已自动配置：{cfg_file}")
    if desired.mode == "official":
        print(f"    mode=official model={desired.effective_model()}（已切回官方模型）")
    else:
        print(f"    model={desired.effective_model()} base_url={desired.base_url}")
        print(f"    wire_api={desired.wire_api}")
    if applied.backup:
        print(f"    旧文件已备份：{applied.backup}")


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
            # A database in the payload drags its side-cars along: whatever
            # -wal/-shm is on disk right now describes the database we are
            # about to replace, so it must go - see ``_DB_SIDECARS``.
            db_is_restored = any(rel.as_posix() == "data/zkai.db" for rel in to_restore)
            stale_sidecars = (
                [Path(rel) for rel in _DB_SIDECARS if (root / rel).exists()]
                if db_is_restored
                else []
            )

            conflicts = [rel for rel in to_restore if (root / rel).exists()]
            if conflicts and not overwrite:
                listing = "\n  ".join(str(c) for c in conflicts)
                # Exit code 3, not 1: import_machine.cmd tells "the target
                # already has these files" apart from a hard refusal (gateway
                # running, wrong passphrase) and offers the --overwrite retry
                # only for this case - asking it after a wrong passphrase would
                # just be noise.
                print(
                    "目标机器上已有这些文件，直接覆盖有风险：\n  " + listing +
                    "\n\n确认要覆盖就加 --overwrite 再跑一次（旧文件会先备份）。"
                )
                raise SystemExit(_EXIT_CONFLICTS)

            backup = (
                _backup_existing(root, [*to_restore, *stale_sidecars])
                if (conflicts or stale_sidecars)
                else None
            )
            # Drop the side-cars *before* the new database lands: for the whole
            # window in between, an open of data/zkai.db would pair the fresh
            # file with the old WAL. Backup first, then unlink.
            for rel in stale_sidecars:
                (root / rel).unlink()
                print(f"  已清除旧数据库的残留 {rel}（原文件在备份目录里）")
            restored: list[str] = []
            for rel in to_restore:
                dst = root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(zf.read(rel.as_posix()))
                restored.append(str(rel))
            # New machine: the ChatGPT/Codex client is configured from the
            # package's desired config (file-level only - the OS env var is the
            # CLI layer's job, see _provision_chatgpt_env).
            _apply_chatgpt_client(root, [r.replace("\\", "/") for r in restored])
            return restored, backup
    finally:
        for junk in tmp.glob("*"):
            with contextlib.suppress(OSError):
                junk.unlink()
        with contextlib.suppress(OSError):
            tmp.rmdir()


def _resolve_env_key_name(root: Path) -> str:
    """桌面版实际引用的环境变量名：磁盘 config.toml > chatgpt.yaml > 默认。

    磁盘上的 ``~/.codex/config.toml`` 是真相——它可能是本工具 patch 的，也
    可能是操作员从旧机器整个手抄过来的（没有 chatgpt.yaml 时唯一的事实来源，
    2026-09-22 换机事故正是漏了这条路径：provision 被 chatgpt.yaml 门控，
    源机没存过它，手抄配置的 env_key 就没人管）。
    """
    from app.services import chatgpt_service

    disk = chatgpt_service.read_disk_state(chatgpt_service.config_toml_path())
    if disk.env_key:
        return disk.env_key
    try:
        desired = chatgpt_service.load_desired(root / "config" / "chatgpt.yaml")
    except ValueError:
        desired = None
    if desired is not None and desired.env_key:
        return desired.env_key
    return "ZKAI_API_TOKEN"


def _provision_chatgpt_env(root: Path, restored: list[str]) -> None:
    """Provision the user-level env var the ChatGPT desktop app reads.

    The desktop app launches from explorer: it inherits neither shell variables
    nor ``.env``, so the ``env_key`` named in config.toml must exist in the
    *user* environment (HKCU\\Environment) or the client fails with
    "Missing environment variable: <NAME>". This is the last link of "it just
    works after a machine move". The old value is backed up, the restore
    command is printed, and the write is verified by reading it back - nothing
    silent. Gated on ``.env`` (not chatgpt.yaml): a hand-copied ``~/.codex``
    needs this just as much as a console-saved desired config does.
    """
    if ".env" not in restored:
        return
    from app.services import chatgpt_service

    name = _resolve_env_key_name(root)
    token = _env_value(name, root=root)
    if not token:
        print(f'  [警告] .env 里没有 {name}——桌面版 config.toml 引用它，打开 app 会报')
        print(f'  "Missing environment variable: {name}"。二选一：')
        print(f"    a) 在 .env 里加 {name}=<网关令牌>（和 ZKAI_API_TOKEN 同值）后重跑导入")
        print(f'    b) 手动 setx {name} "<令牌>"，然后重启桌面版')
        return
    written, old, note = chatgpt_service.write_user_env_var(name, token)
    if not written:
        if note:
            print(f"  （{note}）")
        return
    if old == token:
        print(f"  用户级环境变量 {name} 已是目标值，未改动")
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = root / "imports_backup" / stamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "chatgpt_user_env.json").write_text(
            json.dumps({name: old}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  已写入用户级环境变量 {name}（HKCU\\Environment，explorer 启动的桌面版也读得到）")
        print(f"    旧值备份：{backup_dir / 'chatgpt_user_env.json'}")
        print(f"    还原命令：{chatgpt_service.env_restore_hint(name, old)}")
    # 回读校验：注册表里真的是这个值吗（写成功但读不到=桌面版照样报错）
    visible = chatgpt_service.read_user_env_var(name)
    if visible == token:
        print(f"  校验通过：{name} 已可被 explorer 启动的新进程读到")
    else:
        print(f"  [警告] 回读校验失败：{name} 读出的值不符合预期")
        print(f'    手动修复：setx {name} "<令牌>"，然后重启 ChatGPT 桌面版')
    print(f"  注意：ChatGPT 桌面版若已经开着，退出（含托盘图标）后重开才会读到 {name}")


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


def _env_value(name: str, root: Path | None = None) -> str | None:
    """Read one variable out of ``.env`` (a double-click exports nothing).

    ``root`` is resolved at call time rather than defaulting to ``_ROOT`` in
    the signature, so a caller (or a test) that redirects ``_ROOT`` actually
    gets the ``.env`` it asked for.
    """
    base = root or _ROOT
    try:
        lines = (base / ".env").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("#") or not line.startswith(f"{name}="):
            continue
        return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def _resolve_port(default: int = 8317) -> int:
    """The port this checkout uses: environment, then ``.env``, then the default.

    A double-click exports neither, and a hardcoded 8317 would probe the wrong
    socket for anyone who moved the gateway.
    """
    raw = os.environ.get("ZKAI_PORT") or _env_value("ZKAI_PORT") or ""
    try:
        return int(raw)
    except ValueError:
        return default


def _gateway_is_listening(port: int = 0, timeout: float = 1.0) -> bool:
    """True when something already answers on the gateway's port.

    Importing onto a running gateway is the one way to genuinely wreck the
    database: SQLite keeps the old file open through ``-wal``/``-shm``, and
    replacing that file underneath it leaves a write-ahead log that belongs to
    nothing. The CLI refuses before it touches a byte; the library function
    stays probe-free so tests (and scripted restores) keep full control.
    """
    try:
        with socket.create_connection(("127.0.0.1", port or _resolve_port()), timeout=timeout):
            return True
    except OSError:
        return False


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
        return _EXIT_OK

    if args.cmd == "import":
        archive = Path(args.archive)
        if not archive.exists():
            print(f"找不到迁移包: {archive}")
            return _EXIT_ERROR
        # Refuse before asking for the passphrase: making the operator type a
        # secret only to be told "stop the gateway first" is a bad trade.
        if _gateway_is_listening():
            print(f"网关正在端口 {_resolve_port()} 上运行 - 导入前必须先停掉它。")
            print("托盘右键「退出」（或 scripts\\port_guard.py），否则覆盖数据库会留下对不上的 WAL，")
            print("下次启动就是 database disk image is malformed。这一步不能省。")
            return _EXIT_ERROR
        pw = args.passphrase or _ask_passphrase(confirm=False)
        try:
            restored, backup = import_package(archive, pw, overwrite=args.overwrite)
        except ValueError as exc:
            # Wrong passphrase / not one of our archives. Without this the
            # operator gets a raw traceback where the manual promises a sentence.
            print(f"导入失败：{exc}")
            print("什么都没改动（校验不过就不落盘）。密码确认无误还报这条，就是 zip 拷坏了，重拷一次。")
            return _EXIT_BAD_PASSPHRASE
        print()
        print("导入完成。")
        for name in restored:
            print(f"    + {name}")
        if backup:
            print(f"  被覆盖的旧文件已备份到: {backup}")
        names = [name.replace("\\", "/") for name in restored]
        _provision_chatgpt_env(_ROOT, names)
        if any("chatgpt.yaml" in name for name in names):
            print()
            print("  ChatGPT / Codex 桌面版已指向本机网关——装好 app 打开即用。")
        else:
            from app.services import chatgpt_service as _cg

            if _cg.config_toml_path().exists():
                print()
                print("  包里没有 chatgpt.yaml：桌面版沿用你现有的 ~/.codex/config.toml；")
                print("  若它是从旧机器手抄的，上面的用户级环境变量就是它缺的那一环。")
        print()
        print("下一步：")
        print("  1. 双击 scripts\\start_gateway.cmd 启动（缺 .venv 会自动重建）")
        print("  2. 浏览器打开 http://127.0.0.1:8317/health 看到 healthy 就成了")
        return _EXIT_OK

    return _EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
