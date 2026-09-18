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

Usage: ``python scripts/launch_hidden.py gateway [extra args]``
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:  # allow "python scripts/launch_hidden.py" from anywhere
    sys.path.insert(0, str(_ROOT))

from scripts.tray_launcher import _NO_WINDOW, _child_interpreter, _venv_home  # noqa: E402


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
    argv_out = [str(interpreter), "scripts/tray_launcher.py", mode, *argv[1:]]
    print(f"starting tray ({mode}) with {interpreter.name}, no console window")
    # No-console flags *and* a GUI-subsystem interpreter: belt and braces. The
    # child is fully detached, so it survives this helper (and the .cmd) exiting.
    subprocess.Popen(  # noqa: S603 - our own fixed argv
        argv_out,
        cwd=_ROOT,
        env={**os.environ, **env_extra},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
