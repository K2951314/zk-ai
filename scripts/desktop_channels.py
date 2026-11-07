"""桌面版通道盘点（只读，不派任务）。

分两层：
- A 层：桌面应用已经开着的本地服务（WorkBuddy / CodeBuddy 桌面版、Qoder 桌面端）。
- B 层：可以用命令行协议接入的 agent 工具（ACP / MCP / 本地服务等）。

三条红线：
1. 只读。不向任何桌面版会话发消息，不发 POST，不写文件、不改配置。
2. 端口与凭据一律动态读取，不写死，也不打印出来。
3. 只做「能不能连上、有哪些入口」的判断，能不能派任务留给后面的接入层决定。

地址来源：
- WorkBuddy / CodeBuddy：优先 ``CODEBUDDY_SERVICE_PROXY_URL``，回落
  ``SERVER__HOST`` + ``SERVER__PORT``；凭据取 ``CODEBUDDY_GATEWAY_PASSWORD``。
- Qoder：``~/.qoder/mcp-router.json`` 的 ``baseUrl`` 与 ``apiKey``。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

#: 单次探活默认超时（秒）。桌面服务都在本机，连不上要快速给结论。
_DEFAULT_TIMEOUT = 4.0

#: B 层各工具的协议能力（2026-11-06 逐个查 --help 得到）。
#: 这里只记「有哪些门」，不在这里真去调它们。
_B_PROTOCOLS: dict[str, tuple[str, ...]] = {
    "codebuddy": ("ACP", "本地服务", "MCP", "远程控制", "IDE 连接", "worktree"),
    "claude": ("MCP", "远程控制", "IDE 连接", "worktree", "结构化输出"),
    "codex": ("应用服务", "MCP", "worktree", "JSON 输出"),
    "gemini": ("ACP", "MCP", "worktree", "JSON 输出"),
    "hermes": ("ACP", "网关", "MCP", "worktree", "用量文件"),
    "catpawai": ("MCP 注入", "编辑器扩展"),
    "qodercli": ("无头打印", "模型清单"),
}

#: A 层 WorkBuddy / CodeBuddy 桌面版的只读入口。
_WB_PATHS = ("/health", "/api/v1/health", "/api/v1/status", "/api/v1/sessions")
#: A 层 Qoder 桌面端的只读入口（服务形态未定，逐个试）。
_QODER_PATHS = ("/", "/health", "/mcp")


def workbuddy_endpoint() -> tuple[str, str] | None:
    """返回桌面版服务地址与来源说明；找不到返回 None。"""
    raw = os.environ.get("CODEBUDDY_SERVICE_PROXY_URL", "").strip()
    if raw:
        try:
            parts = urlsplit(raw)
            if parts.scheme and parts.hostname and parts.port:
                base = f"{parts.scheme}://{parts.hostname}:{parts.port}"
                return base, "服务地址环境变量"
        except ValueError:
            pass
    host = os.environ.get("SERVER__HOST", "").strip()
    port = os.environ.get("SERVER__PORT", "").strip()
    if host and port.isdigit():
        return f"http://{host}:{port}", "主机端口环境变量"
    return None


def qoder_endpoint() -> tuple[str, str, str] | None:
    """返回 Qoder 桌面端地址、凭据与来源说明；找不到返回 None。"""
    path = Path.home() / ".qoder" / "mcp-router.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    base = str(data.get("baseUrl") or "").strip().rstrip("/")
    key = str(data.get("apiKey") or "").strip()
    if not base:
        return None
    return base, key, "路由配置文件"


def http_get(base: str, path: str, token: str, timeout: float) -> tuple[int | None, str]:
    """带 Bearer 凭据做一次只读 GET；返回状态码与正文片段。"""
    url = base + path
    # 只放行 http/https，挡掉 file: 之类的自定义协议；地址来自本机配置文件。
    if urlsplit(url).scheme not in ("http", "https"):
        return None, "不支持的地址"
    req = urllib.request.Request(  # noqa: S310 - 协议已在上一步白名单里限制
        url,
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 同上
            body = resp.read(2048).decode("utf-8", "replace")
            return int(resp.status), body
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""
    except (OSError, ValueError) as exc:
        return None, type(exc).__name__


def _detail(path: str, status: int | None, body: str) -> str:
    """把返回正文压成一句中文说明，不回显任何凭据或会话内容。"""
    if status != 200:
        return "未连通" if status is None else f"返回 {status}"
    try:
        data = json.loads(body)
    except ValueError:
        return "正常"
    payload = data.get("data") if isinstance(data, dict) else None
    if path.endswith("/sessions") and isinstance(payload, dict):
        items = payload.get("sessions")
        if isinstance(items, list):
            return f"正常（{len(items)} 条会话）"
    if path.endswith("/status") and isinstance(payload, dict):
        return "正常（正忙）" if payload.get("busy") else "正常（空闲）"
    return "正常"


def _probe_group(
    name: str,
    base: str,
    token: str,
    source: str,
    paths: tuple[str, ...],
    timeout: float,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for path in paths:
        status, body = http_get(base, path, token, timeout)
        checks.append(
            {
                "path": path,
                "status": status,
                "detail": _detail(path, status, body),
            }
        )
    return {
        "name": name,
        "ok": any(item["status"] == 200 for item in checks),
        "base": base,
        "source": source,
        "credential": "已配置" if token else "未配置",
        "checks": checks,
    }


def scan_a(timeout: float = _DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """A 层：桌面版本地服务只读探活。"""
    results: list[dict[str, Any]] = []
    wb = workbuddy_endpoint()
    if wb is None:
        results.append(
            {
                "name": "WorkBuddy / CodeBuddy 桌面版",
                "ok": False,
                "reason": "未找到服务地址",
            }
        )
    else:
        base, source = wb
        token = os.environ.get("CODEBUDDY_GATEWAY_PASSWORD", "").strip()
        results.append(
            _probe_group(
                "WorkBuddy / CodeBuddy 桌面版",
                base,
                token,
                source,
                _WB_PATHS,
                timeout,
            )
        )
    qd = qoder_endpoint()
    if qd is None:
        results.append(
            {"name": "Qoder 桌面端", "ok": False, "reason": "未找到路由配置文件"}
        )
    else:
        base, key, source = qd
        results.append(
            _probe_group("Qoder 桌面端", base, key, source, _QODER_PATHS, timeout)
        )
    return results


def _which_loose(name: str) -> str | None:
    """找一个命令，兼容 Windows 的带后缀入口与 Git Bash 的无后缀入口。

    ``shutil.which`` 在 Windows 上按 PATHEXT 拼后缀，Git Bash 的 npm/venv
    shim 是没有扩展名的文件，会被漏掉（hermes 就是这样）。
    """
    found = shutil.which(name)
    if found:
        return found
    for suffix in (".exe", ".cmd", ".bat"):
        found = shutil.which(name + suffix)
        if found:
            return found
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return None


def scan_b() -> list[dict[str, Any]]:
    """B 层：只检查命令是否存在与协议能力，不启动任何工具。"""
    results: list[dict[str, Any]] = []
    for name, protocols in _B_PROTOCOLS.items():
        found = _which_loose(name)
        results.append(
            {
                "name": name,
                "ok": found is not None,
                "protocols": list(protocols),
                "reason": "" if found else "命令不在路径中",
            }
        )
    return results


def _print_report(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> None:
    print("=== 桌面版通道盘点（只读）===")
    print()
    print("A 层 · 已有本地服务的桌面版")
    for item in a:
        mark = "可用" if item["ok"] else "不可用"
        print(f"  {item['name']}：{mark}")
        if "base" in item:
            print(f"    地址：{item['base']}（来源：{item['source']}）")
            print(f"    凭据：{item['credential']}（不显示内容）")
            for check in item["checks"]:
                print(f"    {check['path']}：{check['detail']}")
        else:
            print(f"    原因：{item['reason']}")
    print()
    print("B 层 · 可用命令行协议接入")
    for item in b:
        mark = "已安装" if item["ok"] else "未安装"
        suffix = "" if item["ok"] else f"（{item['reason']}）"
        print(f"  {item['name']}：{mark}{suffix}")
        if item["ok"]:
            print(f"    可用入口：{'、'.join(item['protocols'])}")
    print()
    print("B 层只表示命令存在，不代表已经能派任务；实际可用性由接入层逐个验证。")
    print("本次只做只读探活，没有向任何会话发送任务，也没有改动配置。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="盘点本机桌面版与命令行通道（只读）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出，便于程序读取")
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT,
        help="单次探活超时秒数，默认 4 秒",
    )
    args = parser.parse_args(argv)
    a = scan_a(timeout=max(0.5, args.timeout))
    b = scan_b()
    if args.json:
        print(json.dumps({"a": a, "b": b}, ensure_ascii=False, indent=2))
    else:
        _print_report(a, b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
