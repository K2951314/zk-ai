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
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
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
        # 积分消耗器配置：烧哪些 Key、每账号 5h/周锚点、费率与预算熔断线。
        # 不带它，新机的消耗器就只会用代码默认值——费率会退回旧的低估 7 倍的
        # 默认（120/360），锚点全丢，重新烧穿专属池只是时间问题。
        # 模板 burner.example.yaml 不需要搬（新机有仓库里的那份）。
        Path("config/burner.yaml"),
    ]


def _secrets_files() -> list[Path]:
    return [Path(".env")]


def _env_newline_problem(raw: bytes) -> tuple[int, int]:
    """Count (doubled-CR, total LF) in a .env's raw bytes.

    ``\\r\\r\\n`` means every line's *content* ends with a stray CR: the value
    keeps it through pydantic-settings (``str.splitlines()`` only eats the
    terminator), and it shows up as wildly misleading downstream errors — the
    2026-09-24 case surfaced as ``getaddrinfo failed`` on ``socket.bind``.
    """
    return raw.count(b"\r\r"), raw.count(b"\n")


def _check_env_health(root: Path, *, stage: str) -> list[str]:
    """Warn when .env has the doubled-CR sickness. Returns the problems found.

    Called from BOTH sides of a transfer, which is the whole point: the damage
    is done by whatever wrote the file on the *source* machine, so checking at
    export time is what stops it from ever reaching a new machine. Checking at
    import time too catches a hand-copied file.

    Damage history, stated correctly: it happened **once** for real (2026-09-24,
    after the machine swap - 115 lines all ``\\r\\r\\n``). On 2026-09-26 the file
    was overwritten with a 38-byte UTF-16LE stub by a test script, which is a
    different failure (not this pattern, and not a recurrence). Either way a bad
    .env reaches a new machine through this path, so the check stays.
    """
    env = root / ".env"
    if not env.exists():
        return []
    raw = env.read_bytes()
    doubled, lf = _env_newline_problem(raw)
    if doubled == 0:
        return []
    problem = (
        f".env 有 {doubled}/{lf} 行是 \\r\\r\\n（行尾多一个 CR）"
        f"——{stage}会把这个损坏带过去"
    )
    return [problem]


#: ``env: NAME`` / ``env_var: NAME`` — the line form a credential uses to name
#: the variable it reads. The value is resolved straight from ``os.environ`` by
#: CredentialPool (app/credentials/pool.py), never from ``.env``. The ``${NAME}``
#: spelling is accepted too so this scan cannot drift from
#: ``CredentialConfig.env_reference()`` (which normalises both to the same
#: thing); ``tests/test_migrate.py`` pins the two against the real config.
_ENV_LINE = re.compile(
    r"^\s*-?\s*env(?:_var)?\s*:\s*(?:\$\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))\s*$"
)
#: ``- id: sensenova-01`` — nearest preceding label, used to say *which*
#: credential is affected instead of only naming a variable.
_ID_LINE = re.compile(r"^\s*-?\s*id\s*:\s*['\"]?([A-Za-z0-9_.\-]+)['\"]?\s*$")
#: burner.yaml's ``only:`` block names the keys the burner may burn.
_ONLY_HEAD = re.compile(r"^\s*only\s*:\s*($|#)")
_ONLY_ITEM = re.compile(r"^\s*-\s*([A-Z][A-Z0-9_]{2,})\s*$")


def _credential_env_refs(root: Path) -> dict[str, list[str]]:
    """{环境变量名: 读它的地方}，例如 ``{"SENSENOVA_API_KEY": ["providers.yaml:sensenova-01"]}``。

    Deliberately a **text scan**, not ``load_app_config()``: export has to keep
    working when the config is broken, because a broken config is exactly when
    the operator wants a backup. Parsing would trade a loud failure for a
    silent one. The trade is that it tracks the two shapes actually in use
    (``env:`` in providers.yaml credentials, the ``only:`` list in burner.yaml)
    and stays quiet about everything else — a warning tool only needs to be
    right about what it claims.
    """
    refs: dict[str, list[str]] = {}
    providers = root / "config" / "providers.yaml"
    if providers.is_file():
        label = ""
        for line in providers.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            who = _ID_LINE.match(line)
            if who:
                label = who.group(1)
                continue
            what = _ENV_LINE.match(line)
            if what:
                name = what.group(1) or what.group(2)
                where = f"providers.yaml:{label or '?'}"
                refs.setdefault(name, [])
                if where not in refs[name]:
                    refs[name].append(where)
    burner = root / "config" / "burner.yaml"
    if burner.is_file():
        inside = False
        for line in burner.read_text(encoding="utf-8", errors="replace").splitlines():
            if _ONLY_HEAD.match(line):
                inside = True
                continue
            if not inside:
                continue
            item = _ONLY_ITEM.match(line)
            if item:
                refs.setdefault(item.group(1), []).append("burner.yaml:only")
                continue
            if line.strip() and not line.lstrip().startswith(("#", "-")):
                inside = False
    return refs


def _check_credential_envs(root: Path, *, stage: str) -> dict[str, str]:
    """Warn about credential keys that will NOT travel with the package.

    ``.env`` is the only secret the transfer carries (see ``_secrets_files``),
    but credentials resolve their names from the *process* environment. A name
    that lives only in the OS environment (HKCU\\Environment or Machine) is
    invisible to the package: the source machine works, the new machine comes
    up with that credential DISABLED, and nothing says why it used to work.

    Found for real on 2026-09-29: ``SENSENOVA_API_KEY`` — ``sensenova-01``,
    priority 100, the account-A key — is set at the *user* level on this
    machine and absent from ``.env``, which carries ``SENSENOVA_API_KEY_02``
    through ``_10`` only. One machine move and the gateway's most important
    credential silently disappears.

    Returns ``{变量名: 问题描述}`` so the caller can print one remediation
    block per name without re-deriving which name went with which problem.
    """
    problems: dict[str, str] = {}
    for name, where in sorted(_credential_env_refs(root).items()):
        if _env_value(name, root=root):
            continue  # in .env -> travels with the package
        who = "、".join(where)
        if os.environ.get(name):
            problems[name] = (
                f"{who} 要读 {name}，但它不在 .env 里（只存在于本机 OS 环境变量）"
                f"——{stage}会漏掉它，新机器上这个凭据 DISABLED"
            )
        else:
            problems[name] = (
                f"{who} 要读 {name}，.env 和 OS 环境变量里都没有"
                f"（这台机器上它已经是 DISABLED）——{stage}帮不了它"
            )
    return problems


def _print_credential_env_fix(names: list[str]) -> None:
    """One remediation block for a batch of problems (no value is ever shown)."""
    print("    影响：迁移包只带 .env 这一份密钥载体，OS 环境变量带不走。")
    print("    修复（不重设值，从 OS 环境变量里取，全程不回显；改 .env 前先备份）：")
    for name in names:
        print(f'      $v=[Environment]::GetEnvironmentVariable("{name}","User")')
        print(f'      if (-not $v) {{ $v=[Environment]::GetEnvironmentVariable("{name}","Machine") }}')
        print(f'      Add-Content -Path .env -Value "{name}=$v"')
    print("    然后重跑本脚本。")


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

    # Stop the transfer from carrying a known landmine. Fixing it here costs one
    # sed; not fixing it means the new machine fails in a way that looks like a
    # DNS problem (see the .env/CR entry in AGENTS.md).
    for problem in _check_env_health(root, stage="导出前：这份 .env 的损坏"):
        print(f"  [警告] {problem}")
        print("    症状：ZKAI_HOST='0.0.0.0\\r' 之类，bind 时报 getaddrinfo failed，")
        print("    但日志里 Application startup complete 之后紧跟着报错，一眼就知道不是 DNS。")
        print("    修复：把开头的 \\r\\r\\n 全替换成 \\r\\n（改前先备份，改后逐行比对）。")
        print("    判定：python -c \"raw=open('.env','rb').read();"
              " print(raw.count(b'\\r\\r'), raw.count(b'\\r\\n'))\" —— 两数相等就是全文件损坏")

    # Keys that only exist in the OS environment do not travel: the package
    # carries .env and nothing else. This is the same class of bug as the CR
    # damage above (a .env problem that only shows up on the *new* machine),
    # but it is found by comparing the names credentials read against the names
    # .env declares - and it is common, because Windows tools routinely set
    # ANTHROPIC_*/OPENAI_* at the user level for their own use.
    cred_problems = _check_credential_envs(root, stage="导出")
    if cred_problems:
        print(f"  [警告] {len(cred_problems)} 个凭据要读的环境变量不在 .env 里"
              "——照现在导出，新机器会缺 Key")
        for problem in cred_problems.values():
            print(f"    - {problem}")
        _print_credential_env_fix(sorted(cred_problems))

    # A *unique* temp dir, not the old fixed ``.migrate_tmp``: the cleanup below
    # empties the directory, so two exports at once (an operator double-clicking
    # export while a test suite runs) used to delete each other's in-flight
    # payload and produce an archive that fails with ``BadZipFile`` on import —
    # a corrupt package that looks like a wrong passphrase. Seen for real on
    # 2026-09-29 while this file was being reviewed.
    tmp = Path(tempfile.mkdtemp(prefix=".migrate_tmp-", dir=_ROOT))
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
        # Never leave plaintext key material on disk longer than needed. This
        # dir is ours alone, so removing it whole is both simpler and safe.
        shutil.rmtree(tmp, ignore_errors=True)


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
    except ValueError as exc:
        # 坏文件/手术验证不过：chatgpt_service 已保证原文件未动，告知后继续导入
        print(f"  [警告] 未写入客户端配置：{exc}")
        return
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
            # Second line of defence: the source machine may have had a healthy
            # .env but the package could still have been built before this check
            # existed. Fail loudly here rather than letting the operator find out
            # on first launch as "bind failed / getaddrinfo failed".
            for problem in _check_env_health(root, stage="导入后：刚恢复的 .env"):
                print(f"  [警告] {problem}")
                print("    这台机器上的其它一切看起来都正常，但一启动就会 bind 失败。")
                print("    修复：把开头的 \\r\\r\\n 全替换成 \\r\\n（改前先备份，改后逐行比对）。")
            # Same check on the receiving end. Here it means something stronger
            # than "this machine is missing a key": the package could not have
            # carried it, so no amount of re-importing will help - the operator
            # has to go back to the old machine and add it to .env first.
            cred_problems = _check_credential_envs(root, stage="导入")
            if cred_problems:
                print(f"  [警告] {len(cred_problems)} 个凭据的环境变量不在刚恢复的 .env 里")
                for problem in cred_problems.values():
                    print(f"    - {problem}")
                print("    这不是这台机器的问题：迁移包只带 .env，源机器上漏了的 Key "
                      "重跑导入也拿不回来。")
                print("    回源机器补进 .env（见导出时的提示）后重新导出导入。")
            # New machine: the ChatGPT/Codex client is configured from the
            # package's desired config (file-level only - the OS env var is the
            # CLI layer's job, see _provision_chatgpt_env).
            _apply_chatgpt_client(root, [r.replace("\\", "/") for r in restored])
            # 消耗器只在启动时读配置：导入换了 burner.yaml / burn_state.json 之后，
            # 运行中的实例仍在用旧值。留一个重启请求，托盘心跳会替我们重启它。
            if any(r.replace("\\", "/").startswith(("config/burner.yaml", "data/burn_state.json"))
                   for r in restored):
                with contextlib.suppress(OSError):
                    (root / "data").mkdir(parents=True, exist_ok=True)
                    (root / "data" / "burner_restart.request").write_text(
                        time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
                print("  已请求重启积分消耗器（托盘会在数秒内重启它，新配置才会生效）")
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
    try:
        written, old, note = chatgpt_service.write_user_env_var(name, token)
    except OSError as exc:
        # 注册表写失败不该让整个导入带上 traceback（文件已恢复完了）
        print(f"  [警告] 写用户级环境变量 {name} 失败（不影响网关本身）：{exc}")
        print(f'    手动修复：setx {name} "<令牌>"，然后重启 ChatGPT 桌面版')
        return
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
