"""重启逻辑：托盘文件 vs systemd，按部署形态二选一。

拆自 burner_service.py。同一个「保存配置并重启」动作，本机与服务器走完全
不同的通道：
  本机（Windows 托盘）：burner 由托盘 spawn，网关碰不到它。写一个
    data/burner_restart.request，托盘心跳（3s）看到就重启再删文件。
  服务器（systemd）：zkai-burner 是 systemd unit，托盘不存在，restart
    request 文件写一万年也没人看。此时走 polkit 通道（bare systemctl，
    **不碰 sudo**——服务进程带 NoNewPrivileges，sudo 必被拦）。

判据刻意保守：只有看到 systemctl 能找到 zkai-burner 这个 unit 时才走
systemd，否则一律退回托盘文件——误判成 systemd 会在本机执行一个必然失败
的命令，而托盘那条路是本机唯一正确的路。

polkit 规则见 ``deploy-server/50-zkai.rules``。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app.services.systemd_ctl import run_unit_action, unit_active, unit_loaded

#: 服务器上 burner 的 systemd unit 名（deploy-server/zkai-burner.service）。
_SYSTEMD_UNIT = "zkai-burner"


def request_restart(data_dir: Path) -> Path:
    path = data_dir / "burner_restart.request"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
    return path


def restart_pending(data_dir: Path) -> bool:
    return (data_dir / "burner_restart.request").exists()


def detect_restart_mode(data_dir: Path) -> str:
    """'systemd' | 'tray' | 'manual'。"""
    if unit_loaded(_SYSTEMD_UNIT):
        return "systemd"
    if os.name == "nt":
        return "tray"
    return "manual"


def request_restart_ex(data_dir: Path) -> dict[str, Any]:
    """按部署形态请求重启，返回 {mode, message, command, ok}。

    只有真正把重启发出去（或把信号文件写好）才 ok=True；manual 模式下
    ok=False，界面据此把手动命令显示给运营者。
    """
    mode = detect_restart_mode(data_dir)
    if mode == "systemd":
        return run_unit_action(_SYSTEMD_UNIT, "restart")
    if mode == "tray":
        path = request_restart(data_dir)
        return {
            "mode": "tray",
            "ok": True,
            "message": "已留下重启请求，托盘约 3 秒内重启消耗器",
            "command": "",
            "request_file": str(path),
        }
    return {
        "mode": "manual",
        "ok": False,
        "message": "检测不到托盘也检测不到 systemd unit，请手动重启消耗器",
        "command": "python scripts/burn_sensenova.py",
    }


def stop_burner(data_dir: Path) -> dict[str, Any]:
    """停止消耗器。systemd 模式直接 systemctl stop；托盘模式写 stop 文件。"""
    mode = detect_restart_mode(data_dir)
    if mode == "systemd":
        return run_unit_action(_SYSTEMD_UNIT, "stop")
    if mode == "tray":
        path = data_dir / "burner_stop.request"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
        return {"mode": "tray", "ok": True,
                "message": "已留下停止请求，托盘约 3 秒内停止消耗器"}
    return {"mode": "manual", "ok": False,
            "message": "检测不到托盘也检测不到 systemd unit，请手动停止消耗器",
            "command": "pkill -f burn_sensenova"}


def start_burner(data_dir: Path) -> dict[str, Any]:
    """启动消耗器。systemd 模式直接 systemctl start；托盘模式写 start 文件。"""
    mode = detect_restart_mode(data_dir)
    if mode == "systemd":
        return run_unit_action(_SYSTEMD_UNIT, "start")
    if mode == "tray":
        path = data_dir / "burner_start.request"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
        return {"mode": "tray", "ok": True,
                "message": "已留下启动请求，托盘约 3 秒内启动消耗器"}
    return {"mode": "manual", "ok": False,
            "message": "检测不到托盘也检测不到 systemd unit，请手动启动消耗器",
            "command": "python scripts/burn_sensenova.py"}


def is_burner_running(data_dir: Path) -> bool:
    """消耗器是否在运行。systemd 看 active 状态；托盘模式看心跳文件。"""
    if unit_loaded(_SYSTEMD_UNIT):
        return unit_active(_SYSTEMD_UNIT)
    # 托盘/本机模式：托盘心跳会写 burner_alive 心跳文件，3 秒内更新 = 活着
    marker = data_dir / "burner_alive"
    if not marker.exists():
        return False
    try:
        return time.time() - marker.stat().st_mtime < 10
    except OSError:
        return False


__all__ = [
    "detect_restart_mode",
    "is_burner_running",
    "request_restart",
    "request_restart_ex",
    "restart_pending",
    "start_burner",
    "stop_burner",
]
