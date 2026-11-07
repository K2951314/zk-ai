"""MCP（Model Context Protocol）HTTP 客户端：只做 initialize 与 tools/call。

为什么单独一条路：外部 CLI 是「拉起子进程、读 stdout」，而 MCP 是
「POST JSON-RPC、读 SSE/JSON 响应」，没有进程可等。所以 runner 里按
``backend == "mcp"`` 分支到本模块，其余（角色、审批、超时、失败分类）
全部复用现有链路。

协议要点（2026-11-07 对 editor-sdk 1.0.0 实测）：
- ``POST <url>``，``Accept: application/json, text/event-stream``
- ``initialize`` 返回 ``serverInfo`` / ``protocolVersion``，不需要额外发通知
- ``tools/call`` 返回 ``result.content`` 数组，正文在 ``type == "text"`` 的项里
- 响应可能是纯 JSON，也可能是 ``event: message`` + ``data: {...}`` 的 SSE

安全：地址只允许 http/https（挡掉 file: 等自定义协议）。
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx

#: 与 editor-sdk 实测一致的协议版本；其它 server 若不认，会在 initialize 报错。
_PROTOCOL_VERSION = "2024-11-05"
#: MCP 要求客户端声明可接受的响应类型（SSE 优先）。
_ACCEPT = "application/json, text/event-stream"


class McpError(RuntimeError):
    """MCP 调用失败（地址非法、协议错误、工具报错）。"""


def _payload(text: str, rid: int) -> dict[str, Any] | None:
    """从纯 JSON 或 SSE 文本里挑出指定编号的 JSON-RPC 响应。"""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("id") in (rid, None):
            return data
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[6:])
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("id") == rid:
            return data
    return None


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise McpError(f"MCP 地址不合法：{url!r}")


def _extract_text(result: dict[str, Any]) -> str:
    """把 tools/call 的 result 压成文本；非 text 内容用 JSON 兜底。"""
    content = result.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        if parts:
            return "\n".join(parts)
    if result:
        return json.dumps(result, ensure_ascii=False)
    return ""


async def call_tool(
    url: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    timeout: float,  # noqa: ASYNC109 - 超时由调用方 runner 统一控制
    client_info: tuple[str, str] = ("zk-ai-gateway", "0.1"),
) -> str:
    """连一个 MCP server，初始化后调用一个工具，返回正文。"""
    _check_url(url)
    headers = {"Accept": _ACCEPT, "Content-Type": "application/json"}
    name, version = client_info
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": name, "version": version},
        },
    }
    call_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            init_resp = await client.post(url, json=init_body, headers=headers)
            init_data = _payload(init_resp.text, 1)
            if init_data is None:
                raise McpError(f"初始化没有返回可解析结果（HTTP {init_resp.status_code}）")
            if init_data.get("error"):
                raise McpError(_error_text(init_data["error"]))
            call_resp = await client.post(url, json=call_body, headers=headers)
            data = _payload(call_resp.text, 2)
            if data is None:
                raise McpError(f"工具调用没有返回可解析结果（HTTP {call_resp.status_code}）")
            if data.get("error"):
                raise McpError(_error_text(data["error"]))
            result = data.get("result")
            if not isinstance(result, dict):
                raise McpError("工具返回结构里没有 result")
            text = _extract_text(result)
            if result.get("isError") is True:
                raise McpError(text or "工具标记为失败")
            return text or "（工具执行成功，无文本输出）"
    except httpx.HTTPError as exc:
        raise McpError(f"连接失败：{type(exc).__name__}") from exc


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        message = str(error.get("message") or "未知错误")
        data = error.get("data")
        if isinstance(data, dict) and data.get("reason"):
            return f"{message}（{data['reason']}）"
        return message
    return str(error)
