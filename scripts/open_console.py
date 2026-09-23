"""Open the gateway console with the admin token pre-filled.

The console is a browser page talking to ``/admin/*`` with an
``X-Admin-Token`` header. Requiring an operator to find that token in ``.env``
and paste it into a dialog every time (and especially after a ``.env`` edit, when
the stored value silently 401s) is the single biggest piece of friction in this
project. The page already accepts ``?token=`` in the URL, so the fix is to hand
it the token on the way in - the same way a local dev tool would.

This module holds the URL-building and wait-for-port logic shared by the .cmd
launcher and the tray menu, so both paths behave identically.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
#: Belt-and-braces no-console flags (see the note in tray_launcher).
_NO_WINDOW = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS


def env_from_file(name: str, default: str | None = None) -> str | None:
    """Read one variable out of ``.env`` without importing pydantic-settings."""
    path = _ROOT / ".env"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().removeprefix("export ").strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value or default
    return default


def console_url(*, path: str = "ui", port: int = 8317, token: str | None = None) -> str:
    """The console URL, with ``token`` in the query when we know one.

    The token is passed as a parameter rather than via the header for one reason
    only: it is a *local* tool, the page caches it in localStorage anyway, and the
    alternative is an operator typing it in by hand. ``ZKAI_API_TOKEN`` (the
    inference surface) is never involved here - /admin/* only.
    """
    url = f"http://127.0.0.1:{port}/{path.lstrip('/')}"
    if token:
        url += f"?token={token}"
    return url


def wait_for_port(port: int, timeout: float = 30.0) -> bool:
    """Poll the console's own page until the gateway answers (no dependency)."""
    probe = f"http://127.0.0.1:{port}/ui"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(probe, timeout=2) as response:
                if response.status < 500:
                    return True
        except urllib.error.HTTPError as exc:  # e.g. 401 without a token: still up
            if exc.code < 500:
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def open_console(*, path: str = "ui", port: int | None = None, token: str | None = None,
                 wait: float = 30.0) -> bool:
    """Wait for the gateway, then open the console URL. False when it never came up."""
    resolved_port = port or int(env_from_file("ZKAI_PORT", "8317") or 8317)
    resolved_token = token or env_from_file("ZKAI_ADMIN_TOKEN")
    if not wait_for_port(resolved_port, wait):
        return False
    webbrowser.open(console_url(path=path, port=resolved_port, token=resolved_token or None))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打开网关控制台页面")
    parser.add_argument("--path", default="ui", help="控制台子路径，如 ui 或 ui/agent")
    parser.add_argument("--port", type=int, default=None, help="覆盖自动解析出的端口")
    parser.add_argument("--token", default=None, help="管理令牌（默认从 .env 读取）")
    parser.add_argument("--wait", type=float, default=30.0, help="等待端口就绪的秒数")
    parser.add_argument("--detach", action="store_true",
                        help="隐式重新拉起本脚本并立即返回（供 .cmd 调用）")
    args = parser.parse_args(argv)

    if args.detach:
        child = [sys.executable, str(Path(__file__).resolve()),
                 "--path", args.path, "--wait", str(args.wait)]
        if args.port is not None:
            child += ["--port", str(args.port)]
        if args.token:
            child += ["--token", args.token]
        subprocess.Popen(  # noqa: S603 - our own fixed argv
            child, cwd=str(_ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW, env={**os.environ},
        )
        print("网关就绪后会自动打开控制台页面")
        return 0

    if open_console(path=args.path, port=args.port, token=args.token, wait=args.wait):
        print("控制台已打开")
        return 0
    print(
        "网关迟迟没有响应——请手动打开 http://127.0.0.1:"
        f"{args.port or env_from_file('ZKAI_PORT', '8317')}/{args.path}，"
        "或查看 data/gateway.log 里的日志"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
