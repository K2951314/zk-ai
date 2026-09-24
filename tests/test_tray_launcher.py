"""Tray launcher: launch-arg construction, icon colors, and heartbeat logic."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from scripts.tray_launcher import (
    _BLUE,
    _GREEN,
    TrayLauncher,
    _capture_file,
    _child_interpreter,
    _env_or_default,
    _icon_set,
    _launch_args,
    _venv_home,
    _view_log,
)

PYW = Path(r"C:\fake\pythonw.exe")


def test_launch_args_gateway() -> None:
    args = _launch_args("gateway", ["--port", "9999"], PYW)
    assert args[0].endswith("pythonw.exe"), "must be GUI subsystem: no console possible"
    assert args[1:5] == ["-m", "uvicorn", "app.main:app", "--host"]
    assert ("--port" in args and "8317" in args) or "9999" in args


def test_launch_args_burner() -> None:
    args = _launch_args("burner", ["--concurrency", "8"], PYW)
    assert args[0].endswith("pythonw.exe")
    assert args[1] == "scripts/burn_sensenova.py"
    assert args[2:] == ["--concurrency", "8"]


def test_venv_home_reads_pyvenv_cfg(tmp_path: Path, monkeypatch) -> None:
    from scripts import tray_launcher

    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(
        "implementation = CPython\nhome = C:\\base\\python\nversion_info = 3.13\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tray_launcher, "_ROOT", tmp_path)
    assert _venv_home() == Path(r"C:\base\python")


def test_venv_home_missing_file_is_none(tmp_path: Path, monkeypatch) -> None:
    from scripts import tray_launcher

    monkeypatch.setattr(tray_launcher, "_ROOT", tmp_path)
    assert _venv_home() is None


def test_child_interpreter_prefers_real_gui_pythonw(tmp_path: Path, monkeypatch) -> None:
    """uv venv -> base pythonw.exe + __PYVENV_LAUNCHER__ (the trampoline is bypassed)."""
    from scripts import tray_launcher

    base = tmp_path / "base"
    base.mkdir()
    (base / "pythonw.exe").write_bytes(b"")
    venv_scripts = tmp_path / ".venv" / "Scripts"
    venv_scripts.mkdir(parents=True)
    (venv_scripts / "pythonw.exe").write_bytes(b"")
    (tmp_path / ".venv" / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    monkeypatch.setattr(tray_launcher, "_ROOT", tmp_path)
    monkeypatch.setattr(tray_launcher, "_VENV_SCRIPTS", venv_scripts)

    exe, env = _child_interpreter()
    assert exe == base / "pythonw.exe"
    assert env["__PYVENV_LAUNCHER__"] == str(venv_scripts / "pythonw.exe")


def test_child_interpreter_falls_back_to_venv_pythonw(tmp_path: Path, monkeypatch) -> None:
    """No pyvenv.cfg -> the venv's own pythonw.exe (genuine GUI interpreter)."""
    from scripts import tray_launcher

    venv_scripts = tmp_path / ".venv" / "Scripts"
    venv_scripts.mkdir(parents=True)
    (venv_scripts / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(tray_launcher, "_ROOT", tmp_path)
    monkeypatch.setattr(tray_launcher, "_VENV_SCRIPTS", venv_scripts)

    exe, env = _child_interpreter()
    assert exe == venv_scripts / "pythonw.exe"
    assert env == {}


def test_log_paths_are_split_for_burner() -> None:
    """The burner writes its own structured log; stdout must not double-write it."""
    assert _view_log("burner").name == "burn_sensenova.log"
    assert _capture_file("burner").name != _view_log("burner").name
    assert _capture_file("gateway") == _view_log("gateway")


def test_env_or_default_prefers_env(monkeypatch) -> None:
    monkeypatch.setenv("ZKAI_TEST_X", "from-env")
    assert _env_or_default("ZKAI_TEST_X", "fallback") == "from-env"


def test_env_or_default_falls_back_to_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ZKAI_TEST_Y", raising=False)
    (tmp_path / ".env").write_text("ZKAI_TEST_Y=from-file\n", encoding="utf-8")
    # _env_or_default reads _ROOT/.env, not tmp_path; patch the root
    from scripts import tray_launcher
    monkeypatch.setattr(tray_launcher, "_ROOT", tmp_path)
    assert _env_or_default("ZKAI_TEST_Y", "fallback") == "from-file"


def test_env_or_default_uses_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ZKAI_TEST_Z", raising=False)
    from scripts import tray_launcher
    monkeypatch.setattr(tray_launcher, "_ROOT", tmp_path)
    assert _env_or_default("ZKAI_TEST_Z", "fallback") == "fallback"


def test_icon_set_colors() -> None:
    green = _icon_set(_GREEN)
    blue = _icon_set(_BLUE)
    assert green.size == (64, 64) and green.mode == "RGBA"
    assert blue.size == (64, 64) and blue.mode == "RGBA"
    # centers differ
    assert green.getpixel((32, 32)) == _GREEN
    assert blue.getpixel((32, 32)) == _BLUE


class _FakeChild:
    def __init__(self, alive: bool = True, age: float = 0.0) -> None:
        self._alive = alive
        self._age = age

    def alive(self) -> bool:
        return self._alive

    def log_age(self) -> float:
        return self._age

    def start(self) -> None:  # pragma: no cover - stub
        self._alive = True

    def stop(self) -> None:  # pragma: no cover - stub
        self._alive = False


def _make(mode: str) -> TrayLauncher:
    launcher = TrayLauncher(mode, [])
    launcher._child = _FakeChild()
    return launcher


def test_color_green_when_gateway_alive() -> None:
    launcher = _make("gateway")
    launcher._child._alive = True
    assert launcher._current_color() == _GREEN


def test_color_blue_when_gateway_dead() -> None:
    launcher = _make("gateway")
    launcher._child._alive = False
    assert launcher._current_color() == _BLUE


def test_color_green_when_burner_alive_and_fresh() -> None:
    launcher = _make("burner")
    launcher._child._alive = True
    launcher._child._age = 5.0
    assert launcher._current_color() == _GREEN


def test_color_blue_when_burner_silent_too_long() -> None:
    launcher = _make("burner")
    launcher._child._alive = True
    launcher._child._age = time.time() + 1
    assert launcher._current_color() == _BLUE


def test_color_blue_when_burner_dead() -> None:
    launcher = _make("burner")
    launcher._child._alive = False
    assert launcher._current_color() == _BLUE


def test_status_text_gateway() -> None:
    launcher = _make("gateway")
    launcher._child._alive = True
    assert launcher._status_text() == "运行中"
    launcher._child._alive = False
    assert launcher._status_text() == "已退出"


def test_status_text_burner() -> None:
    launcher = _make("burner")
    launcher._child._alive = True
    launcher._child._age = 10.0
    assert launcher._status_text() == "运行中"
    launcher._child._age = 120.0
    assert "无输出" in launcher._status_text()
    launcher._child._alive = False
    assert launcher._status_text() == "已退出"


def test_restart_child_does_not_block_the_menu(monkeypatch) -> None:
    """「重启」 must return immediately - start() blocks on the port guard."""
    launcher = _make("gateway")
    entered = threading.Event()
    release = threading.Event()

    class _SlowChild:
        def start(self) -> None:
            entered.set()
            release.wait(timeout=5.0)

    launcher._child = _SlowChild()
    refreshed: list[bool] = []
    monkeypatch.setattr(launcher, "_refresh", lambda: refreshed.append(True))

    started = time.monotonic()
    launcher.restart_child()
    assert time.monotonic() - started < 0.5, "menu callback must not block on start()"
    assert entered.wait(timeout=2.0), "start() should run on a background thread"
    release.set()
    for _ in range(100):
        if refreshed:
            break
        time.sleep(0.05)
    assert refreshed, "the icon is refreshed once the new child is up"


# --------------------------------------------------------------------------- #
# 控制台「保存并重启」：信箱文件 → 托盘重启 burner
# --------------------------------------------------------------------------- #
class _CountingChild:
    """只关心 start() 被调了几次的心跳替身。"""

    def __init__(self) -> None:
        self.starts = 0

    def start(self) -> None:
        self.starts += 1

    def alive(self) -> bool:
        return True

    def log_age(self) -> float:
        return 0.0


def _tray(mode: str) -> TrayLauncher:
    """Build a launcher without touching pystray's real icon."""
    tray = object.__new__(TrayLauncher)
    tray._mode = mode
    tray._running = True
    tray._child = _CountingChild()
    return tray


def test_restart_request_restarts_the_burner(tmp_path: Path, monkeypatch) -> None:
    from scripts import tray_launcher

    req = tmp_path / "burner_restart.request"
    monkeypatch.setattr(tray_launcher, "RESTART_REQUEST", req)
    tray = _tray("burner")

    assert tray._consume_restart_request() is False, "没有请求就不该重启"
    assert tray._child.starts == 0

    req.write_text("2026-09-24 13:00:00\n", encoding="utf-8")
    assert tray._consume_restart_request() is True
    assert tray._child.starts == 1
    assert not req.exists(), "请求必须被消费掉，否则每次心跳都重启一次"


def test_restart_request_is_ignored_in_gateway_mode(tmp_path: Path, monkeypatch) -> None:
    from scripts import tray_launcher

    req = tmp_path / "burner_restart.request"
    req.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(tray_launcher, "RESTART_REQUEST", req)
    tray = _tray("gateway")

    # 心跳只在 burner 模式检查信箱；网关模式留着文件不动（可能属于另一台机器）
    assert tray._child.starts == 0
    assert req.exists()
