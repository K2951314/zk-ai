"""Start the tray proxy fully hidden - called by the .cmd launchers.

Why a helper instead of `start ... pythonw.exe` in the batch file:

* ``start`` inside a batch inherits the batch console, and because uv's venv
  ``pythonw.exe`` is a trampoline that re-executes the real interpreter (a
  console-subsystem ``python.exe``), the tray process ends up owning a console
  window - minimized, but stuck in the taskbar.
* ``start /min`` only minimizes; it does not prevent the console at all.
* A VBScript helper would work, but .vbs is parsed with the system ANSI code
  page, so any non-ASCII byte (an em-dash in a comment is enough) breaks it.

This script runs *inside* the console the .cmd already opened (no new window),
spawns the tray with the real GUI-subsystem ``pythonw.exe`` and no-console
creation flags, then exits immediately so the batch window can close.

The tray's own stdout/stderr goes to ``data/tray_<mode>.log`` rather than
DEVNULL, and we give it a moment before declaring success. Both exist because
of a real incident: ``.venv`` had never been re-synced after Pillow/pystray
became dependencies, so the tray died on ``from PIL import ...`` before it
could even draw an icon. With DEVNULL that failure was invisible - the .cmd
window vanished and the taskbar stayed empty, which reads as "the gateway
crashed" when in fact the *watcher* never started. The log plus a non-zero
exit code turn that into an actionable message.

Usage: ``python scripts/launch_hidden.py gateway [extra args]``
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:  # allow "python scripts/launch_hidden.py" from anywhere
    sys.path.insert(0, str(_ROOT))

from scripts.tray_launcher import _NO_WINDOW, _child_interpreter, _venv_home  # noqa: E402

#: How long a gracefully-asked tray gets to exit before it is killed outright.
_EXIT_GRACE = 5.0

#: How long the freshly-spawned tray gets to prove it is still alive. A tray
#: that crashes on import (missing dependency, broken interpreter) is already
#: gone by then, so the check is reliable; the cost is that every successful
#: launch waits this long before the .cmd window is allowed to close.
_SETTLE_SECONDS = 1.5

#: Lines of the tray log printed back to the operator when it dies.
_TAIL_LINES = 15

#: Flag for the short-lived probe processes (powershell/taskkill).
#: Deliberately NOT the tray's ``CREATE_NO_WINDOW | DETACHED_PROCESS``: with
#: DETACHED_PROCESS, powershell's CIM queries answer with empty stdout (rc 0),
#: which silently disabled every command-line probe - port_guard only survived
#: it because it falls back to an HTTP identity check.
_PROBE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _tray_pattern(mode: str) -> str:
    """Regex fragment matching ``tray_launcher.py <mode>`` command lines."""
    return rf"tray_launcher\.py\s+{re.escape(mode)}"


def _capture(args: list[str]) -> tuple[int, str]:
    """Run *args* hidden and capture stdout (empty string on failure)."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no user input
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=_PROBE_FLAGS,
        )
        return proc.returncode, proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _still_running(pid: int) -> bool:
    """True when *pid* still exists (limited-query handle, no admin needed)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def _existing_tray_pids(mode: str) -> list[int]:
    """PIDs of running tray processes for *mode* (empty when none / unreadable).

    Deliberately avoids ``-Filter`` (its double quotes are one more thing to
    survive argv round-tripping) and retries once: CIM occasionally answers
    with an empty result under load, and a false negative here resurrects the
    double-tray bug this module exists to prevent.
    """
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -in 'python.exe','pythonw.exe' "
        f"-and $_.CommandLine -match '{_tray_pattern(mode)}' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    for attempt in range(3):
        code, out = _capture(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        )
        pids = [int(token) for token in out.split() if token.isdigit()] if code == 0 else []
        if pids or attempt == 2:
            return pids
        time.sleep(0.5)
    return []


def stop_existing_trays(mode: str) -> list[int]:
    """Stop a same-mode tray so a .cmd double-click cannot spawn a second one.

    Without this the .cmd path starts a *second* tray while the first still
    runs: the new tray's port guard kills the old tray's gateway child, and the
    old tray - alive but childless - turns its icon blue and lingers as a ghost
    in the notification area. Graceful ``taskkill`` (WM_CLOSE) first so pystray
    removes its own icon; hard kill only if it overstays the grace period.
    """
    pids = _existing_tray_pids(mode)
    for pid in pids:
        _capture(["taskkill", "/PID", str(pid)])
    deadline = time.monotonic() + _EXIT_GRACE
    while time.monotonic() < deadline:
        if not any(_still_running(pid) for pid in pids):
            break
        time.sleep(0.3)
    for pid in pids:
        if _still_running(pid):
            _capture(["taskkill", "/F", "/T", "/PID", str(pid)])
    return pids


def _tray_log(mode: str) -> Path:
    """Where the tray process' own stdout/stderr lands."""
    return _ROOT / "data" / f"tray_{mode}.log"


def _tail(path: Path, lines: int) -> str:
    """Last *lines* lines of *path* (empty string when unreadable)."""
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _tray_exit_code(proc: subprocess.Popen[bytes] | None) -> int | None:
    """The tray's exit code once it has died inside the settle window, else None."""
    time.sleep(_SETTLE_SECONDS)
    return proc.poll() if proc is not None else None


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "gateway"
    if mode not in {"gateway", "burner"}:
        print(f"usage: launch_hidden.py [gateway|burner] [extra args]; got {mode!r}")
        return 2
    interpreter, env_extra = _child_interpreter()
    if not interpreter.exists():
        print(f"ERROR: interpreter not found: {interpreter}")
        print(f"       (.venv/pyvenv.cfg home = {_venv_home()})")
        return 1
    with contextlib.suppress(Exception):
        # Never leave two trays of the same mode running (blue ghost icon).
        stopped = stop_existing_trays(mode)
        if stopped:
            print(f"stopped previous tray ({mode}): PID {', '.join(map(str, stopped))}")
    argv_out = [str(interpreter), "scripts/tray_launcher.py", mode, *argv[1:]]
    print(f"starting tray ({mode}) with {interpreter.name}, no console window")
    log = _tray_log(mode)
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with log.open("ab", buffering=0) as handle:
        handle.write(f"\n===== {stamp} launching tray: {mode} =====\n".encode())
        # No-console flags *and* a GUI-subsystem interpreter: belt and braces. The
        # child is fully detached, so it survives this helper (and the .cmd) exiting.
        proc = subprocess.Popen(  # noqa: S603 - our own fixed argv
            argv_out,
            cwd=_ROOT,
            env={**os.environ, **env_extra},
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    if (code := _tray_exit_code(proc)) is not None:
        print(f"ERROR: the tray exited immediately (code {code}) - nothing will show in the taskbar.")
        print(f"       Its own output: {log.relative_to(_ROOT)}")
        detail = _tail(log, _TAIL_LINES)
        if detail:
            print("       last lines:")
            for line in detail.splitlines():
                print(f"       | {line}")
        print("       Most often this is a stale .venv - run: uv sync")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
