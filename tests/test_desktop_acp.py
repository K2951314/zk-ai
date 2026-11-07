"""桌面版 ACP 客户端的离线测试：不发真实请求、不碰真实凭据。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from scripts import desktop_acp as da


class _FakeResp:
    def __init__(self, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.text = text
        self.headers = headers or {}

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _FakeClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.posts: list[dict[str, Any]] = []
        self.post_text = ""

    def stream(self, method: str, url: str, headers: dict[str, str]) -> _FakeResp:
        return _FakeResp("", {"acp-connection-id": "占位连接编号"})

    def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> _FakeResp:
        self.posts.append(json)
        return _FakeResp(self.post_text)

    def get(self, url: str, headers: dict[str, str]) -> _FakeResp:
        if url.endswith("/status"):
            return _FakeResp('{"data":{"busy":false,"activeSessionId":"a"}}')
        return _FakeResp(
            '{"data":{"sessions":[{"id":"a","name":"示例会话","messageCount":3}]}}'
        )

    def close(self) -> None:
        pass


def _install_fake(monkeypatch: Any) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(da.httpx, "Client", lambda *args, **kwargs: fake)
    return fake


def _endpoint() -> da.DesktopEndpoint:
    return da.DesktopEndpoint("http://127.0.0.1:1", "1", "a", "快照")


def test_discover_picks_latest(monkeypatch: Any, tmp_path: Path) -> None:
    (tmp_path / "1.json").write_text(
        json.dumps({"url": "http://127.0.0.1:1", "lastHeartbeat": 10, "pid": 1}),
        encoding="utf-8",
    )
    (tmp_path / "2.json").write_text(
        json.dumps(
            {
                "endpoint": "http://127.0.0.1:2",
                "lastHeartbeat": 99,
                "pid": 2,
                "sessionId": "s2",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(da, "_SESSION_DIR", tmp_path)
    found = da.discover()
    assert found is not None
    assert found.base == "http://127.0.0.1:2"
    assert found.session_id == "s2"


def test_discover_skips_bad_files(monkeypatch: Any, tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("不是 JSON", encoding="utf-8")
    (tmp_path / "nourl.json").write_text('{"pid":1}', encoding="utf-8")
    monkeypatch.setattr(da, "_SESSION_DIR", tmp_path)
    assert da.discover() is None


def test_discover_missing_dir(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(da, "_SESSION_DIR", tmp_path / "不存在")
    assert da.discover() is None


def test_credential(monkeypatch: Any) -> None:
    monkeypatch.delenv(da._CREDENTIAL_ENV, raising=False)
    assert da.credential() == ""
    monkeypatch.setenv(da._CREDENTIAL_ENV, " 占位 ")
    assert da.credential() == "占位"


def test_rpc_parses_streamed_result(monkeypatch: Any) -> None:
    _install_fake(monkeypatch)
    with da.AcpClient(_endpoint(), "占位") as client:
        assert client._conn_id == "占位连接编号"
        client._client.post_text = (  # type: ignore[union-attr]
            ':ok\nevent: message\ndata: {"jsonrpc":"2.0","id":1,'
            '"result":{"agentCapabilities":{"loadSession":true}}}\n'
        )
        result = client.initialize()
        assert result["result"]["agentCapabilities"]["loadSession"] is True


def test_rpc_timeout_is_pending(monkeypatch: Any) -> None:
    _install_fake(monkeypatch)

    def boom(*args: Any, **kwargs: Any) -> _FakeResp:
        raise httpx.ReadTimeout("超时")

    with da.AcpClient(_endpoint(), "占位") as client:
        monkeypatch.setattr(client._client, "post", boom)  # type: ignore[arg-type]
        assert client.prompt("a", "你好").get("pending") is True


def test_readonly_helpers(monkeypatch: Any) -> None:
    _install_fake(monkeypatch)
    with da.AcpClient(_endpoint(), "占位") as client:
        assert client.status()["data"]["busy"] is False
        items = client.sessions()
        assert items[0]["name"] == "示例会话"


def test_brief_messages_are_chinese() -> None:
    assert "握手成功" in da._brief(
        {"result": {"agentCapabilities": {"loadSession": True}}}
    )
    assert "失败" in da._brief(
        {"error": {"message": "被占用", "data": {"reason": "writer_occupied"}}}
    )
    assert "已提交" in da._brief({"pending": True})


def test_main_requires_credential(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(da, "discover", lambda: _endpoint())
    monkeypatch.delenv(da._CREDENTIAL_ENV, raising=False)
    assert da.main([]) == 1
    out = capsys.readouterr().out
    assert "读不到桌面版凭据" in out
    assert "桥接程序" in out


def test_main_readonly_does_not_prompt(monkeypatch: Any, capsys: Any) -> None:
    _install_fake(monkeypatch)
    monkeypatch.setattr(da, "discover", lambda: _endpoint())
    monkeypatch.setenv(da._CREDENTIAL_ENV, "占位")
    assert da.main([]) == 0
    out = capsys.readouterr().out
    assert "只读查看" in out
    assert "没有改动任何会话" in out
