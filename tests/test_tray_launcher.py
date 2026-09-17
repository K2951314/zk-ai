"""Tray launcher: launch-arg construction, icon colors, and heartbeat logic."""

from __future__ import annotations

import time
from pathlib import Path

from scripts.tray_launcher import (
    _BLUE,
    _GREEN,
    TrayLauncher,
    _env_or_default,
    _icon_set,
    _launch_args,
)


def test_launch_args_gateway() -> None:
    args = _launch_args("gateway", ["--port", "9999"])
    assert args[0].endswith("python.exe")
    assert args[1:5] == ["-m", "uvicorn", "app.main:app", "--host"]
    assert ("--port" in args and "8317" in args) or "9999" in args


def test_launch_args_burner() -> None:
    args = _launch_args("burner", ["--concurrency", "8"])
    assert args[0].endswith("python.exe")
    assert args[1] == "scripts/burn_sensenova.py"
    assert args[2:] == ["--concurrency", "8"]


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
