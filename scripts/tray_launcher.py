"""System-tray wrapper for the gateway and the SenseNova credit burner.

Replaces the always-visible minimized console window with a quiet icon in the
system tray (the clock-corner overflow area). Pure-python, no extra build steps:
the icon is drawn at runtime so there are no PNG assets to maintain.

Two modes, chosen by the first CLI arg:
    python scripts/tray_launcher.py gateway [extra uvicorn args]
    python scripts/tray_launcher.py burner [extra burn args]

The launcher spawns the real process with CREATE_NO_WINDOW | DETACHED_PROCESS so
no console window appears at all, redirects its output to a plain log file, and
watches two signals: process aliveness (``poll()``) and the log file's mtime (a
process that is logging is doing work). Green icon = alive and logging recently,
blue = process exited or silent for too long.

The tray menu (right-click):
    Open console  -> gateway's /ui in the default browser (gateway only)
    View log      -> notepad on the log file
    Restart       -> kill child, wait, spawn again
    Quit          -> kill child, remove icon
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from scripts.open_console import open_console

logger = logging.getLogger("tray_launcher")

_ROOT = Path(__file__).resolve().parent.parent
_VENV_SCRIPTS = _ROOT / ".venv" / "Scripts"
#: Belt-and-braces no-console flags. Not sufficient on their own for uv venvs:
#: ``.venv\Scripts\python(w).exe`` is a trampoline that re-executes the base
#: interpreter without forwarding these flags, so the innermost process would
#: allocate a fresh console window (the "python.exe stuck in the taskbar").
#: The real fix is launching a GUI-subsystem interpreter directly - see
#: :func:`_child_interpreter`.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS


def _venv_home() -> Path | None:
    """The base-Python directory recorded in ``.venv/pyvenv.cfg`` (uv writes it)."""
    try:
        for line in (_ROOT / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "home":
                return Path(value.strip())
    except OSError:
        return None
    return None


def _child_interpreter() -> tuple[Path, dict[str, str]]:
    """Interpreter + extra env for spawning a child that can NEVER own a console.

    A uv venv's ``Scripts\\python(w).exe`` is a *trampoline*: it re-executes the
    base interpreter, and the creation flags we pass do not survive that hop - the
    innermost process (a console-subsystem python.exe) then allocates its own
    console window, which is the stray "python.exe" that shows up in the taskbar.
    Launching the base **GUI-subsystem** ``pythonw.exe`` directly makes a console
    window structurally impossible; ``__PYVENV_LAUNCHER__`` keeps the venv active
    (site-packages resolution) even though the venv launcher is bypassed.
    """
    venv_pyw = _VENV_SCRIPTS / "pythonw.exe"
    home = _venv_home()
    if home is not None:
        real_pyw = home / "pythonw.exe"
        if real_pyw.exists():
            return real_pyw, {"__PYVENV_LAUNCHER__": str(venv_pyw)}
    if venv_pyw.exists():
        # Non-uv venv: Scripts\pythonw.exe is a genuine GUI-subsystem interpreter.
        return venv_pyw, {}
    logger.warning("no pythonw.exe found - falling back to python.exe; "
                   "a console window may appear in the taskbar")
    return _VENV_SCRIPTS / "python.exe", {}


_GREEN = (46, 160, 67, 255)   # running
_BLUE = (58, 100, 220, 255)   # stopped / silent

#: Seconds without a log write before we treat the child as silent/stalled.
_SILENT_AFTER = 90.0
_HEARTBEAT_EVERY = 3.0


def _icon_image(color: tuple[int, int, int, int], size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(1, size // 16)
    draw.ellipse((pad, pad, size - pad - 1, size - pad - 1), fill=color)
    return img


def _icon_set(color: tuple[int, int, int, int]) -> Image.Image:
    return _icon_image(color, 64)


class _ChildProcess:
    """Manages one detached child process + its log file."""

    def __init__(self, mode: str, extra: list[str]) -> None:
        self._mode = mode
        self._extra = extra
        self._interpreter, self._env_extra = _child_interpreter()
        self._args = _launch_args(mode, extra, self._interpreter)
        #: Log the user reads (menu) and the heartbeat's freshness signal.
        self.view_log = _view_log(mode)
        #: Where the child's stdout/stderr is captured (kept separate from the
        #: burner's own log to avoid every line being written twice).
        self.capture_file = _capture_file(mode)
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_handle: IO[bytes] | None = None
        self._lock = threading.Lock()

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._env_extra)
        return env

    def _guard_port(self) -> None:
        """Reclaim the gateway's port from our own stale instance before spawning.

        Mirrors ``start_gateway.cmd``: without this, a zombie uvicorn would make the
        new process fail to bind and the tray would just show "exited".
        """
        if self._mode != "gateway":
            return
        subprocess.run(  # noqa: S603 - fixed script, no user input
            [str(self._interpreter), "scripts/port_guard.py"],
            cwd=_ROOT,
            env=self._child_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
            timeout=30,
            check=False,
        )

    def start(self) -> None:
        with self._lock:
            self.stop()
            self._guard_port()
            self.capture_file.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.capture_file.open("ab", buffering=0)
            self._proc = subprocess.Popen(  # noqa: S603 - args come from our own constants
                self._args,
                cwd=_ROOT,
                env=self._child_env(),
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )

    def stop(self) -> None:
        proc = self._proc
        handle = self._log_handle
        self._proc = None
        self._log_handle = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if handle:
            with contextlib.suppress(OSError):
                handle.close()

    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def log_age(self) -> float:
        """Seconds since the *readable* log was last written (inf when no file).

        For the burner this is its own ``burn_sensenova.log`` (it logs a summary
        every minute), so silence genuinely means stalled; the gateway only logs
        on requests, which is why it is judged by liveness alone.
        """
        try:
            return time.time() - self.view_log.stat().st_mtime
        except OSError:
            return float("inf")


def _env_or_default(name: str, default: str) -> str:
    """Environment first (set by the launcher or the OS), then .env, then default."""
    value = os.environ.get(name)
    if value:
        return value
    env_file = _ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return default


def _launch_args(mode: str, extra: list[str], interpreter: Path) -> list[str]:
    # The interpreter is always a GUI-subsystem ``pythonw.exe`` (see
    # :func:`_child_interpreter`), so no console window can ever be created.
    exe = str(interpreter)
    if mode == "gateway":
        return [
            exe, "-m", "uvicorn", "app.main:app",
            "--host", _env_or_default("ZKAI_HOST", "127.0.0.1"),
            "--port", _env_or_default("ZKAI_PORT", "8317"),
            "--log-level", "info",
            *extra,
        ]
    return [exe, "scripts/burn_sensenova.py", *extra]


def _view_log(mode: str) -> Path:
    """The log a user wants to read (also the heartbeat's freshness signal)."""
    return _ROOT / "data" / ("gateway.log" if mode == "gateway" else "burn_sensenova.log")


def _capture_file(mode: str) -> Path:
    """Where the child's stdout/stderr goes.

    The burner writes its own structured log (``burn_sensenova.log``), so its raw
    stdout is captured separately - pointing both at the same file would duplicate
    every line. The gateway's logs *are* stdout, so it captures into its own log.
    """
    return _view_log(mode) if mode == "gateway" else _ROOT / "data" / "burn_sensenova.console.log"


def _mode_title(mode: str) -> str:
    return "ZK-AI 网关" if mode == "gateway" else "ZK-AI 消耗器"


def _mode_detail(mode: str) -> str:
    return "http://127.0.0.1:8317" if mode == "gateway" else "商汤积分持续消耗"


class TrayLauncher:
    """Runs the tray icon + heartbeat thread for one mode."""

    def __init__(self, mode: str, extra: list[str]) -> None:
        self._mode = mode
        self._extra = extra
        self._child = _ChildProcess(mode, extra)
        self._running = True
        self._color = _GREEN
        self._icon = self._build_icon()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)

    # ------------------------------------------------------------------ #
    def _status_text(self) -> str:
        if not self._child.alive():
            return "已退出"
        if self._mode == "gateway":
            return "运行中"
        age = self._child.log_age()
        if age > _SILENT_AFTER:
            return f"无输出 {int(age)}s"
        return "运行中"

    def _current_color(self) -> tuple[int, int, int, int]:
        alive = self._child.alive()
        if self._mode == "gateway":
            # HTTP server only logs on requests; silence is normal.
            return _GREEN if alive else _BLUE
        return _GREEN if (alive and self._child.log_age() <= _SILENT_AFTER) else _BLUE

    def _build_icon(self) -> Icon:
        def show_console(icon, item) -> None:
            if self._mode == "gateway":
                # open_console waits for the port and carries the admin token, so
                # "open the console" never lands on the token dialog (and follows
                # a port changed via .env or the launcher argument).
                open_console(path="ui", wait=5.0)

        def show_agent(icon, item) -> None:
            if self._mode == "gateway":
                open_console(path="ui/agent", wait=5.0)

        def view_log(icon, item) -> None:
            subprocess.Popen(  # noqa: S603 - notepad is a fixed Windows component
                [r"C:\Windows\System32\notepad.exe", str(self._child.view_log)],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

        def restart(icon, item) -> None:
            self._child.start()
            self._refresh()

        def quit_(icon, item) -> None:
            self._running = False
            self._child.stop()
            icon.stop()

        menu = Menu(
            MenuItem("状态：--", lambda: None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("打开控制台", show_console, visible=self._mode == "gateway"),
            MenuItem("打开 Agent", show_agent, visible=self._mode == "gateway"),
            MenuItem("查看日志", view_log),
            MenuItem("重启", restart),
            Menu.SEPARATOR,
            MenuItem("退出", quit_),
        )
        return Icon(
            f"zkai-{self._mode}",
            icon=_icon_set(self._color),
            title=f"{_mode_title(self._mode)} - {_mode_detail(self._mode)}",
            menu=menu,
        )

    def _refresh(self) -> None:
        self._color = self._current_color()
        self._icon.icon = _icon_set(self._color)
        status = self._status_text()
        self._icon.title = f"{_mode_title(self._mode)} - {status}"

    def _heartbeat_loop(self) -> None:
        while self._running:
            time.sleep(_HEARTBEAT_EVERY)
            try:
                self._refresh()
            except Exception:
                logger.exception("tray heartbeat failed for %s", self._mode)

    # ------------------------------------------------------------------ #
    def run(self) -> int:
        self._child.start()
        self._heartbeat.start()
        try:
            self._icon.run()  # blocks until icon.stop()
        finally:
            self._running = False
            self._child.stop()
        return 0


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "gateway"
    if mode not in {"gateway", "burner"}:
        print("usage: tray_launcher.py [gateway|burner] [extra args]", file=sys.stderr)
        return 2
    return TrayLauncher(mode, argv[1:]).run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
