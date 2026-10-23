"""网关一键重启：systemd / 托盘，按部署形态二选一。

与消耗器重启（``burner_service.restart``）同一套判据，目标换成 ``zkai`` 网关：
  服务器 systemd：走 polkit（bare systemctl，**不碰 sudo**——服务进程带
    NoNewPrivileges，sudo 是 setuid 必被拦）。网关重启自己时用延迟 + 分离
    进程，先把 HTTP 响应还回去再 SIGTERM。
  本机 Windows 托盘：写 ``data/gateway_restart.request``，托盘心跳看到就
    kill 子进程再 spawn——网关自己不能重启自己。
  都检测不到：返回 manual 模式 + 可抄的命令，界面原样展示。

polkit 规则见 ``deploy-server/50-zkai.rules``（覆盖 zkai + zkai-burner）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app.services.systemd_ctl import run_unit_action, unit_loaded

#: 服务器上网关的 systemd unit 名。
_SYSTEMD_UNIT = "zkai"
_REQUEST_NAME = "gateway_restart.request"

#: 网关重启自己的延迟秒数——要让 HTTP 响应写完再 SIGTERM。
_SELF_RESTART_DELAY = 1.0


def request_file(data_dir: Path) -> Path:
    path = data_dir / _REQUEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
    return path


def pending(data_dir: Path) -> bool:
    return (data_dir / _REQUEST_NAME).exists()


def detect_mode() -> str:
    """'systemd' | 'tray' | 'manual'。"""
    if unit_loaded(_SYSTEMD_UNIT):
        return "systemd"
    if os.name == "nt":
        return "tray"
    return "manual"


def restart_gateway(data_dir: Path | None = None) -> dict[str, Any]:
    """按部署形态请求重启网关，返回 {mode, ok, message, command?}。"""
    root = data_dir or Path(os.environ.get("ZKAI_DATA_DIR", "data"))
    mode = detect_mode()
    if mode == "systemd":
        # 自重启必须分离 + 延迟：同步跑会把当前处理请求的进程先杀了
        return run_unit_action(
            _SYSTEMD_UNIT, "restart", detached_delay=_SELF_RESTART_DELAY
        )
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
