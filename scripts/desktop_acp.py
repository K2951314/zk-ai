"""桌面版 ACP 客户端（网关统领全局的共用积木）。

桌面版本机开着一个 ACP 端点，协议实测（2026-11-07）：
1. ``GET /api/v1/acp`` 带 ``Accept: text/event-stream`` 建立 SSE 连接，
   响应头给 ``acp-connection-id``（另有一个会话令牌，本模块不使用、不打印）。
2. POST JSON-RPC 时把该编号放进 ``acp-connection-id`` 头。
3. ``initialize`` 回报 ``loadSession: true``，即**支持加载已有会话**。
4. ``session/load`` 能加载非活跃会话；当前会话会报 ``writer_occupied``
   （桌面版自己占着写入，属正常独占保护）。
5. ``session/prompt`` 会阻塞到 agent 回完，调用方必须能容忍超时。

⚠ 凭据约束：桌面版网关密码是每次启动随机生成、只通过环境变量传给自己启动的
子进程，**不落盘**。所以这个模块必须在桌面版环境（或其子进程）里运行；
网关独立进程直连拿不到密码，正确做法是「桌面版跑桥接程序、主动向网关领任务」。

安全约定：默认只读。派任务要显式传 ``--yes``，且永不回显任何凭据或会话令牌。
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

#: 桌面版写下的会话快照目录：进程号.json 里有服务地址与当前会话编号。
_SESSION_DIR = Path.home() / ".workbuddy-ai" / "sessions"
_CREDENTIAL_ENV = "CODEBUDDY_GATEWAY_PASSWORD"


@dataclass(frozen=True)
class DesktopEndpoint:
    """一台正在运行的桌面版。"""

    base: str
    pid: str
    session_id: str
    source: str


def discover() -> DesktopEndpoint | None:
    """从会话快照里找心跳最新的桌面版地址；找不到返回 None。"""
    if not _SESSION_DIR.is_dir():
        return None
    best: tuple[float, DesktopEndpoint] | None = None
    for path in _SESSION_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        url = str(data.get("url") or data.get("endpoint") or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            continue
        try:
            heartbeat = float(data.get("lastHeartbeat") or 0)
        except (TypeError, ValueError):
            heartbeat = 0.0
        item = DesktopEndpoint(
            base=url,
            pid=str(data.get("pid") or path.stem),
            session_id=str(data.get("sessionId") or ""),
            source=str(path),
        )
        if best is None or heartbeat > best[0]:
            best = (heartbeat, item)
    return best[1] if best else None


def credential() -> str:
    """读桌面版注入的网关密码；没有就是空串（调用方给出中文提示）。"""
    return os.environ.get(_CREDENTIAL_ENV, "").strip()


class AcpClient:
    """一次 SSE 连接上的 ACP 会话；用 ``with`` 保持连接。"""

    def __init__(self, endpoint: DesktopEndpoint, token: str, timeout: float = 8.0) -> None:
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self._stream: Any = None
        self._conn_id = ""
        self._seq = 0

    def __enter__(self) -> AcpClient:
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "text/event-stream",
        }
        client = httpx.Client(timeout=self.timeout, trust_env=False)
        stream = client.stream("GET", self.endpoint.base + "/api/v1/acp", headers=headers)
        resp = stream.__enter__()
        self._client = client
        self._stream = stream
        self._conn_id = resp.headers.get("acp-connection-id", "")
        if not self._conn_id:
            stream.__exit__(None, None, None)
            client.close()
            raise RuntimeError("桌面版没有返回连接编号")
        return self

    def __exit__(self, *args: Any) -> None:
        if self._stream is not None:
            self._stream.__exit__(*args)
        if self._client is not None:
            self._client.close()

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """发一条 JSON-RPC，并从返回的 SSE 文本里挑出对应编号的结果。"""
        assert self._client is not None
        self._seq += 1
        rid = self._seq
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "acp-connection-id": self._conn_id,
        }
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        try:
            resp = self._client.post(
                self.endpoint.base + "/api/v1/acp", headers=headers, json=body
            )
        except httpx.ReadTimeout:
            return {"pending": True, "note": "已提交，未等待结果"}
        for line in resp.text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                payload = json.loads(line[6:])
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("id") == rid:
                return payload
        return {"pending": True, "note": "响应在事件流里，未取到同编号结果"}

    def initialize(self) -> dict[str, Any]:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "zk-ai-gateway", "version": "0.1"},
            },
        )

    def load_session(self, session_id: str, cwd: str) -> dict[str, Any]:
        return self._rpc(
            "session/load",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
        )

    def prompt(self, session_id: str, text: str) -> dict[str, Any]:
        """派任务。⚠ 这是写操作：会往目标会话里加消息。"""
        return self._rpc(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
        )

    def status(self) -> dict[str, Any]:
        """只读：桌面版当前状态。"""
        assert self._client is not None
        resp = self._client.get(
            self.endpoint.base + "/api/v1/status",
            headers={"Authorization": "Bearer " + self.token},
        )
        return json.loads(resp.text)

    def sessions(self) -> list[dict[str, Any]]:
        """只读：已有会话清单（只返回编号、标题、消息数）。"""
        assert self._client is not None
        resp = self._client.get(
            self.endpoint.base + "/api/v1/sessions",
            headers={"Authorization": "Bearer " + self.token},
        )
        raw = json.loads(resp.text).get("data", {}).get("sessions", [])
        return [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "messages": item.get("messageCount"),
            }
            for item in raw
            if isinstance(item, dict)
        ]


def _brief(result: dict[str, Any]) -> str:
    """把 ACP 结果压成一句中文，不回显凭据或正文。"""
    if result.get("pending"):
        return "已提交（未等待结果）"
    error = result.get("error")
    if isinstance(error, dict):
        reason = str(error.get("data", {}).get("reason") or "") if isinstance(
            error.get("data"), dict
        ) else ""
        return "失败：" + str(error.get("message") or "未知原因") + (
            f"（{reason}）" if reason else ""
        )
    caps = result.get("result", {}).get("agentCapabilities", {}) if isinstance(
        result.get("result"), dict
    ) else {}
    if caps:
        return "握手成功，支持加载已有会话" if caps.get("loadSession") else "握手成功"
    return "已返回"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查看桌面版通道与已有会话（默认只读）")
    parser.add_argument("--timeout", type=float, default=8.0, help="单次请求超时秒数")
    parser.add_argument("--session", default="", help="指定要操作的已有会话编号")
    parser.add_argument("--text", default="", help="要派发的任务文本（配合 --yes 使用）")
    parser.add_argument("--yes", action="store_true", help="确认向已有会话派发任务（写操作）")
    args = parser.parse_args(argv)

    endpoint = discover()
    if endpoint is None:
        print("没有找到正在运行的桌面版会话快照。")
        return 1
    token = credential()
    if not token:
        print("读不到桌面版凭据：这个程序需要在桌面版环境或它的子进程里运行。")
        print("网关独立进程直连拿不到密码，应改由桌面版跑桥接程序主动领任务。")
        return 1

    print(f"桌面版地址：{endpoint.base}（来源：会话快照，进程 {endpoint.pid}）")
    print("凭据：已配置（不显示内容）")
    try:
        with AcpClient(endpoint, token, timeout=max(1.0, args.timeout)) as client:
            print("连接：已建立")
            print("握手：" + _brief(client.initialize()))
            status = client.status().get("data", {})
            print(f"状态：{'正忙' if status.get('busy') else '空闲'}")
            items = client.sessions()
            print(f"已有会话：{len(items)} 条")
            for item in items[:8]:
                mark = "（当前）" if item["id"] == endpoint.session_id else ""
                print(f"  {item['id'][:12]}… {item['name'][:26]} 消息 {item['messages']}{mark}")
            target = args.session or endpoint.session_id
            if args.yes and args.text and target:
                print(f"加载会话：{_brief(client.load_session(target, os.getcwd()))}")
                print(f"派发结果：{_brief(client.prompt(target, args.text))}")
            else:
                print("本次为只读查看，没有改动任何会话。")
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        print("连接失败：" + type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
