"""重启逻辑：托盘文件 vs systemd，按部署形态二选一。

拆自 burner_service.py。同一个「保存配置并重启」动作，本机与服务器走完全
不同的通道：
  本机（Windows 托盘）：burner 由托盘 spawn，网关碰不到它。写一个
    data/burner_restart.request，托盘心跳（3s）看到就重启再删文件。
  服务器（systemd）：zkai-burner 是 systemd unit，托盘不存在，restart
    request 文件写一万年也没人看。此时直接 systemctl restart。

判据刻意保守：只有看到 systemctl 能找到 zkai-burner 这个 unit 时才走
systemd，否则一律退回托盘文件——误判成 systemd 会在本机执行一个必然失败
的命令，而托盘那条路是本机唯一正确的路。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

#: 服务器上 burner 的 systemd unit 名（deploy-server/zkai-burner.service）。
_SYSTEMD_UNIT = "zkai-burner"


def request_restart(data_dir: Path) -> Path:
    path = data_dir / "burner_restart.request"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
    return path


def restart_pending(data_dir: Path) -> bool:
    return (data_dir / "burner_restart.request").exists()


def _systemctl() -> str | None:
    """systemctl 的绝对路径；找不到（Windows）返回 None。

    用绝对路径而不是裸 "systemctl"：既满足 S607，也避免 PATH 被污染时
    误调到一个同名的假命令。
    """
    return shutil.which("systemctl")


def _systemd_unit_active() -> bool:
    """unit 存在且我们能查到它（不一定 active——load 不到就是没这个 unit）。"""
    systemctl = _systemctl()
    if systemctl is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 固定命令+常量 unit 名，无外部输入
            [systemctl, "status", _SYSTEMD_UNIT, "--no-pager", "-n", "0"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # status 对 active/inactive/failed 都返回非 0/0 各异，但「没有这个 unit」会明确
    # 打到 stderr 的 "could not be found"；用返回码 3/4 之外的判据不可靠，
    # 所以直接看 stdout 里有没有 Loaded: 行。
    return "Loaded:" in proc.stdout


def detect_restart_mode(data_dir: Path) -> str:
    """'systemd' | 'tray' | 'manual'。"""
    if _systemd_unit_active():
        return "systemd"
    if os.name == "nt":
        return "tray"
    return "manual"


def _run_systemctl_action(action: str) -> dict[str, Any]:
    """执行 systemctl <action> zkai-burner，自动尝试 sudo -n 免密。

    网关进程通常以非 root 用户运行，裸 ``systemctl stop`` 会因 PolicyKit
    交互认证失败。先试裸命令（本机或有 polkit 授权时直接成功），失败后
    试 ``sudo -n``（服务器配了免密 sudoers 就能过），都失败才回 manual。

    ``action`` 只接受 ``stop`` / ``start`` / ``restart`` 三个常量。
    """
    systemctl = _systemctl()
    if systemctl is None:
        return {"mode": "manual", "ok": False,
                "message": "找不到 systemctl",
                "command": f"sudo systemctl {action} {_SYSTEMD_UNIT}"}
    for cmd_prefix in ([systemctl], ["sudo", "-n", systemctl]):
        try:
            proc = subprocess.run(  # noqa: S603 固定命令+常量，无外部输入
                [*cmd_prefix, action, _SYSTEMD_UNIT],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return {"mode": "systemd", "ok": True,
                    "message": f"已通过 systemd {action} {_SYSTEMD_UNIT}"}
        # 裸 systemctl 失败可能是权限不够，继续试 sudo -n；
        # sudo -n 也失败就真的不行了。
        if cmd_prefix[0] == systemctl:
            continue
        return {"mode": "manual", "ok": False,
                "message": f"systemctl 退出码 {proc.returncode}：{proc.stderr.strip()[:200]}",
                "command": f"sudo systemctl {action} {_SYSTEMD_UNIT}"}
    # 两种方式都因异常跳过了
    return {"mode": "manual", "ok": False,
            "message": f"systemctl {action} 执行失败（权限或路径问题）",
            "command": f"sudo systemctl {action} {_SYSTEMD_UNIT}"}


def request_restart_ex(data_dir: Path) -> dict[str, Any]:
    """按部署形态请求重启，返回 {mode, message, command, ok}。

    只有真正把重启发出去（或把信号文件写好）才 ok=True；manual 模式下
    ok=False，界面据此把手动命令显示给运营者。
    """
    mode = detect_restart_mode(data_dir)
    if mode == "systemd":
        return _run_systemctl_action("restart")
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
        return _run_systemctl_action("stop")
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
        return _run_systemctl_action("start")
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
    """消耗器是否在运行。systemd 看 active 状态；托盘模式看进程或最近的日志。"""
    if _systemd_unit_active():
        systemctl = _systemctl()
        if systemctl is None:
            return False
        try:
            proc = subprocess.run(  # noqa: S603
                [systemctl, "is-active", _SYSTEMD_UNIT],
                capture_output=True, text=True, timeout=10,
            )
            return proc.returncode == 0 and proc.stdout.strip() == "active"
        except (OSError, subprocess.SubprocessError):
            return False
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
