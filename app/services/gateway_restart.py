"""网关一键重启：systemd / 托盘，按部署形态二选一。

与消耗器重启（``burner_service.restart``）同一套判据，目标换成 ``zkai`` 网关：
  服务器 systemd：``systemctl restart zkai``（先裸命令，失败再 ``sudo -n``）。
  本机 Windows 托盘：写 ``data/gateway_restart.request``，托盘心跳看到就
    kill 子进程再 spawn——网关自己不能重启自己。
  都检测不到：返回 manual 模式 + 可抄的命令，界面原样展示。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

#: 服务器上网关的 systemd unit 名。
_SYSTEMD_UNIT = "zkai"
_REQUEST_NAME = "gateway_restart.request"


def request_file(data_dir: Path) -> Path:
    path = data_dir / _REQUEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
    return path


def pending(data_dir: Path) -> bool:
    return (data_dir / _REQUEST_NAME).exists()


def _systemctl() -> str | None:
    return shutil.which("systemctl")


def _systemd_unit_active() -> bool:
    systemctl = _systemctl()
    if systemctl is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 固定命令+常量 unit 名
            [systemctl, "status", _SYSTEMD_UNIT, "--no-pager", "-n", "0"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "Loaded:" in proc.stdout


def detect_mode() -> str:
    """'systemd' | 'tray' | 'manual'。"""
    if _systemd_unit_active():
        return "systemd"
    if os.name == "nt":
        return "tray"
    return "manual"


def _run_systemctl_restart() -> dict[str, Any]:
    systemctl = _systemctl()
    if systemctl is None:
        return {
            "mode": "manual", "ok": False,
            "message": "找不到 systemctl",
            "command": f"sudo systemctl restart {_SYSTEMD_UNIT}",
        }
    for cmd_prefix in ([systemctl], ["sudo", "-n", systemctl]):
        try:
            proc = subprocess.run(  # noqa: S603 固定命令+常量
                [*cmd_prefix, "restart", _SYSTEMD_UNIT],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return {
                "mode": "systemd", "ok": True,
                "message": f"已通过 systemd restart {_SYSTEMD_UNIT}，约 2 秒后恢复",
            }
        if cmd_prefix[0] == systemctl:
            continue
        return {
            "mode": "manual", "ok": False,
            "message": f"systemctl 退出码 {proc.returncode}：{proc.stderr.strip()[:200]}",
            "command": f"sudo systemctl restart {_SYSTEMD_UNIT}",
        }
    return {
        "mode": "manual", "ok": False,
        "message": "systemctl restart 执行失败（权限或路径问题）",
        "command": f"sudo systemctl restart {_SYSTEMD_UNIT}",
    }


def restart_gateway(data_dir: Path | None = None) -> dict[str, Any]:
    """按部署形态请求重启网关，返回 {mode, ok, message, command?}。"""
    root = data_dir or Path(os.environ.get("ZKAI_DATA_DIR", "data"))
    mode = detect_mode()
    if mode == "systemd":
        return _run_systemctl_restart()
    if mode == "tray":
        path = request_file(root)
        return {
            "mode": "tray",
            "ok": True,
            "message": "已留下重启请求，托盘约 3 秒内重启网关",
            "command": "",
            "request_file": str(path),
        }
    return {
        "mode": "manual",
        "ok": False,
        "message": "检测不到 systemd 也检测不到托盘，请手动重启网关",
        "command": "scripts/start_gateway.cmd",
    }
