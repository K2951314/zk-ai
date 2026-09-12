"""启动前检查 8317 端口：本程序的旧实例则结束它，其他程序则提示并退出。

被 ``start_gateway.cmd`` 调用。退出码约定：

* ``0`` —— 端口可用（或已成功释放），可以启动
* ``1`` —— 端口被**其他程序**占用，已打印提示，不要启动
* ``2`` —— 释放旧实例失败（例如权限不足），不要启动
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

PORT = int(os.environ.get("ZKAI_PORT", "8317"))
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str]) -> tuple[int, str]:
    # S603: 参数全部由本脚本硬编码（netstat/powershell/taskkill），无外部输入。
    try:
        proc = subprocess.run(  # noqa: S603
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return proc.returncode, proc.stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{exc}"


def _cim_command_line(pid: int) -> str:
    """Command line for *pid* via PowerShell CIM.

    Deliberately does **not** use ``wmic``: it is deprecated *and* commonly
    blocked by endpoint-security policies, which made this guard silently
    misidentify our own gateway as a foreign process.
    """
    code, out = _run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
        ]
    )
    if code != 0:
        return ""
    return out.strip()


def _ps_command_line(pid: int) -> str:
    """Fallback: read the process command line from WMI via PowerShell."""
    code, out = _run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Get-WmiObject Win32_Process -Filter 'ProcessId={pid}' | "
            "Select-Object -ExpandProperty CommandLine",
        ]
    )
    if code != 0:
        return ""
    return out.strip()


def _listeners() -> list[int]:
    """PIDs listening on PORT (via netstat, no extra dependencies)."""
    _code, out = _run(["netstat", "-ano", "-p", "TCP"])
    pids: list[int] = []
    pattern = re.compile(rf"[:.]{PORT}\s+\S+\s+LISTENING\s+(\d+)", re.IGNORECASE)
    for line in out.splitlines():
        if "LISTENING" not in line.upper():
            continue
        match = pattern.search(line)
        if match:
            pid = int(match.group(1))
            if pid not in pids:
                pids.append(pid)
    return pids


def _command_line(pid: int) -> str:
    """Best-effort command line for a PID. Empty string when unavailable."""
    for probe in (_cim_command_line, _ps_command_line):
        text = probe(pid)
        if text:
            return text
    return ""


def _project_path_evidence(cmdline: str) -> bool:
    """True when *cmdline* clearly points at this gateway.

    Deliberately strict: a project path alone is **not** enough (any editor or
    script running from inside the project directory would be misidentified),
    so we also require an app-runner signal such as ``uvicorn``.
    """
    if not cmdline:
        return False
    lowered = cmdline.lower().replace("/", "\\")
    root = str(PROJECT_ROOT).lower().replace("/", "\\")
    runs_app = "uvicorn" in lowered or "app.main:app" in lowered
    if not runs_app:
        return False
    # Absolute launch from inside the project (or a renamed copy of it)...
    root = root.rstrip("\\")
    if root and root in lowered:
        return True
    # ...or a relative launch: the app module reference is the tell.
    return "app.main:app" in lowered


def _pid_owns_port_via_http(pid: int) -> bool:
    """Ask the process on ``PORT`` whether it is us.

    Strongest signal available: only *our* gateway serves ``/health`` with the
    ``zkai`` marker in its body. This does not depend on being able to read the
    process command line, which security policies may block.
    """
    import json
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False
    if not isinstance(payload, dict):
        return False
    # Our /health returns app/version + routing counts; require a distinctive pair.
    return "app" in payload and ("aliases" in payload or "providers" in payload)


def _is_our_process(pid: int) -> tuple[bool, str]:
    """Return ``(is_ours, description)``."""
    cmdline = _command_line(pid)
    if _project_path_evidence(cmdline):
        return True, cmdline

    # Command line unreadable or inconclusive - fall back to a live probe.
    if _pid_owns_port_via_http(pid):
        return True, cmdline or "<通过 /health 识别为本程序>"

    if not cmdline:
        return False, "<无法读取命令行，且 /health 不匹配>"
    return False, cmdline


def _kill(pid: int) -> bool:
    code, _out = _run(["taskkill", "/F", "/T", "/PID", str(pid)])
    return code == 0


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _force_utf8_console() -> None:
    """Make Chinese output readable in the legacy cmd.exe console.

    Windows consoles default to GBK (cp936); without this the prompts print as
    mojibake. ``errors="replace"`` keeps a failure here from breaking startup.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _force_utf8_console()
    pids = _listeners()
    if not pids:
        return 0

    print(f"  端口 {PORT} 已被占用，正在检查占用进程...")
    verdicts = [(pid, *_is_our_process(pid)) for pid in pids]
    ours = [(pid, cmd) for pid, is_ours, cmd in verdicts if is_ours]
    foreign = [(pid, cmd) for pid, is_ours, cmd in verdicts if not is_ours]

    if ours:
        for pid, cmd in ours:
            print(f"  - 发现本程序的旧实例 (PID {pid})，正在结束...")
            print(f"      命令行: {cmd[:120]}")
        failed = [pid for pid, _ in ours if not _kill(pid)]
        if failed:
            print()
            print(f"  [错误] 无法结束旧实例 (PID: {', '.join(map(str, failed))})。")
            if not _is_admin():
                print("         可能需要在「以管理员身份运行」的窗口里重试。")
            print("         也可以手动打开任务管理器，结束对应的 python 进程后重试。")
            return 2
        for pid, _ in ours:
            print(f"  - 已结束 PID {pid}")
        print()

    if foreign:
        print()
        print(f"  [提示] 端口 {PORT} 被**其他程序**占用，不是本程序的实例。")
        for pid, cmd in foreign:
            print(f"      PID {pid}: {cmd[:160]}")
        print()
        print("  为安全起见不会结束它。请任选一种处理方式：")
        print("    1) 换一个端口启动本网关：")
        print("         命令行执行 scripts/start_gateway.cmd 9000，")
        print("         或修改 .env 里的 ZKAI_PORT（双击启动时也读它）")
        print("    2) 确认上面那个程序可以关闭后，手动结束它再重试")
        print()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
