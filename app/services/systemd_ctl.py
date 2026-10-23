"""systemctl 执行与报错解读：网关/消耗器重启共用的一层薄封装。

为什么要这层：
  1. 裸 ``systemctl`` 走 D-Bus + polkit，**不经过 sudo**，是 ``NoNewPrivileges=true``
     的服务进程里唯一能走通的路（sudo 是 setuid，会被 no_new_privs 拦死）。
  2. polkit 未放行时的报错（"Interactive authentication required"）和
     NoNewPrivileges 的报错长得不一样，但对运营者是同一件事——缺
     ``deploy-server/50-zkai.rules``。统一翻译成人话 + 给安装命令。
  3. 网关重启**自己**时必须 ``--no-block`` + 延迟分离，否则进程会在 HTTP 响应
     写完前被 SIGTERM，前端只看到连接断开。
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

#: polkit 规则文件（仓库路径），报错时告诉运营者装它。
POLKIT_RULE_HINT = "deploy-server/50-zkai.rules"


def find_systemctl() -> str | None:
    """systemctl 的绝对路径；找不到（Windows）返回 None。"""
    return shutil.which("systemctl")


def unit_loaded(unit: str) -> bool:
    """unit 存在且能被 systemctl 查到（不一定 active）。"""
    systemctl = find_systemctl()
    if systemctl is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 固定命令+常量 unit 名
            [systemctl, "status", unit, "--no-pager", "-n", "0"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # 「没有这个 unit」会在 stderr 打 "could not be found"，但返回码因版本而异；
    # 直接看 stdout 有没有 Loaded: 行最稳。
    return "Loaded:" in proc.stdout


def unit_active(unit: str) -> bool:
    systemctl = find_systemctl()
    if systemctl is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            [systemctl, "is-active", unit],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "active"


def _interpret_failure(stderr: str, unit: str, verb: str) -> str:
    """把 systemctl/sudo 的报错翻译成运营者看得懂的下一步。"""
    low = stderr.lower()
    if "no new privileges" in low or "no_new_privs" in low:
        return (
            f"服务进程带 NoNewPrivileges，sudo 提不了权。正确姿势是 polkit 放行"
            f"（不走 sudo）——请安装 {POLKIT_RULE_HINT} 后 "
            f"`sudo systemctl restart polkit`"
        )
    if (
        "interactive authentication required" in low
        or "access denied" in low
        or "not authorized" in low
    ):
        return (
            f"polkit 未放行 www-data 管理 {unit}。请安装 {POLKIT_RULE_HINT} 后 "
            f"`sudo systemctl restart polkit`"
        )
    return f"systemctl {verb} {unit} 退出码非 0：{stderr.strip()[:200]}"


def run_unit_action(
    unit: str,
    verb: str,
    *,
    detached_delay: float = 0.0,
) -> dict[str, Any]:
    """执行 ``systemctl <verb> <unit>``，返回 ``{mode, ok, message, command?}``。

    - 默认同步执行：适用于重启**别的**服务（如网关重启消耗器）。
    - ``detached_delay > 0``：延迟 + 分离进程执行，适用于网关重启**自己**——
      否则进程会在 HTTP 响应写完前被 SIGTERM。此时立即返回 ok=True，
      真正的 restart 由分离进程在延迟后发出。
    - 只认 bare systemctl（polkit 通道）。不再尝试 ``sudo -n``——在
      ``NoNewPrivileges=true`` 下必然失败，还把真正的报错盖掉。
    """
    if verb not in {"start", "stop", "restart", "try-restart", "reload"}:
        return {
            "mode": "manual", "ok": False,
            "message": f"不支持的动作：{verb}",
            "command": f"sudo systemctl restart {unit}",
        }
    systemctl = find_systemctl()
    if systemctl is None:
        return {
            "mode": "manual", "ok": False,
            "message": "找不到 systemctl",
            "command": f"sudo systemctl {verb} {unit}",
        }

    # 网关重启自己：延迟 + 分离，先把 HTTP 响应还回去
    if detached_delay > 0:
        try:
            sh = shutil.which("sh") or "/bin/sh"
            shell_cmd = f"sleep {detached_delay} && {systemctl} --no-block {verb} {unit}"
            subprocess.Popen(  # noqa: S603 固定 sh + systemctl 绝对路径
                [sh, "-c", shell_cmd],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd="/",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "mode": "manual", "ok": False,
                "message": f"分离进程启动失败：{exc}",
                "command": f"sudo systemctl {verb} {unit}",
            }
        return {
            "mode": "systemd", "ok": True,
            "message": f"已发出 systemd {verb} {unit}（延迟 {detached_delay:.0f}s，约 2–5 秒后恢复）",
        }

    try:
        proc = subprocess.run(  # noqa: S603 固定命令+常量
            [systemctl, verb, unit],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "mode": "manual", "ok": False,
            "message": f"systemctl 执行异常：{exc}",
            "command": f"sudo systemctl {verb} {unit}",
        }
    if proc.returncode == 0:
        return {
            "mode": "systemd", "ok": True,
            "message": f"已通过 systemd {verb} {unit}",
        }
    return {
        "mode": "manual", "ok": False,
        "message": _interpret_failure(proc.stderr, unit, verb),
        "command": f"sudo systemctl {verb} {unit}",
    }


__all__ = [
    "POLKIT_RULE_HINT",
    "find_systemctl",
    "run_unit_action",
    "unit_active",
    "unit_loaded",
]
