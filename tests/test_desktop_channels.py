"""桌面通道盘点的离线测试：不发真实请求、不碰用户真实凭据。"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

from scripts import desktop_channels as dc


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def read(self, _size: int) -> bytes:
        return self._body


def test_workbuddy_endpoint_from_proxy_url(monkeypatch: Any) -> None:
    monkeypatch.setenv(
        "CODEBUDDY_SERVICE_PROXY_URL",
        "http://127.0.0.1:52921/internal/hooks/services/invoke",
    )
    assert dc.workbuddy_endpoint() == ("http://127.0.0.1:52921", "服务地址环境变量")


def test_workbuddy_endpoint_fallback(monkeypatch: Any) -> None:
    monkeypatch.delenv("CODEBUDDY_SERVICE_PROXY_URL", raising=False)
    monkeypatch.setenv("SERVER__HOST", "127.0.0.1")
    monkeypatch.setenv("SERVER__PORT", "8317")
    assert dc.workbuddy_endpoint() == ("http://127.0.0.1:8317", "主机端口环境变量")


def test_workbuddy_endpoint_missing(monkeypatch: Any) -> None:
    monkeypatch.delenv("CODEBUDDY_SERVICE_PROXY_URL", raising=False)
    monkeypatch.delenv("SERVER__HOST", raising=False)
    monkeypatch.delenv("SERVER__PORT", raising=False)
    assert dc.workbuddy_endpoint() is None


def test_qoder_endpoint_reads_router(monkeypatch: Any, tmp_path: Path) -> None:
    folder = tmp_path / ".qoder"
    folder.mkdir()
    (folder / "mcp-router.json").write_text(
        json.dumps({"baseUrl": "http://127.0.0.1:58501/", "apiKey": "占位"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    assert dc.qoder_endpoint() == (
        "http://127.0.0.1:58501",
        "占位",
        "路由配置文件",
    )


def test_qoder_endpoint_missing_file(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    assert dc.qoder_endpoint() is None


def test_http_get_sends_bearer(monkeypatch: Any) -> None:
    seen: list[Any] = []

    def fake_urlopen(req: Any, timeout: float) -> _FakeResponse:
        seen.append(req)
        return _FakeResponse(200, b'{"status":"UP"}')

    monkeypatch.setattr(dc.urllib.request, "urlopen", fake_urlopen)
    status, body = dc.http_get("http://127.0.0.1:1", "/health", "占位凭据", 1.0)
    assert status == 200
    assert body == '{"status":"UP"}'
    assert seen[0].get_header("Authorization") == "Bearer 占位凭据"


def test_http_get_rejects_file_scheme() -> None:
    assert dc.http_get("file:///etc", "/passwd", "占位", 1.0)[0] is None


def test_http_get_http_error(monkeypatch: Any) -> None:
    def fake_urlopen(_req: Any, timeout: float) -> _FakeResponse:
        raise urllib.error.HTTPError("http://x", 502, "Bad Gateway", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(dc.urllib.request, "urlopen", fake_urlopen)
    assert dc.http_get("http://127.0.0.1:1", "/health", "占位", 1.0) == (502, "")


def test_detail_sessions_and_busy() -> None:
    payload = json.dumps({"data": {"sessions": [{"id": "1"}, {"id": "2"}]}})
    assert dc._detail("/api/v1/sessions", 200, payload) == "正常（2 条会话）"
    assert dc._detail("/api/v1/status", 200, '{"data":{"busy":true}}') == "正常（正忙）"
    assert dc._detail("/api/v1/status", 200, '{"data":{"busy":false}}') == "正常（空闲）"


def test_detail_non_200_is_chinese() -> None:
    assert dc._detail("/health", None, "") == "未连通"
    assert dc._detail("/health", 401, "") == "返回 401"


def test_scan_a_does_not_leak_credential(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("CODEBUDDY_GATEWAY_PASSWORD", "绝不能外泄的占位值")
    monkeypatch.setattr(
        dc, "workbuddy_endpoint", lambda: ("http://127.0.0.1:1", "服务地址环境变量")
    )
    monkeypatch.setattr(dc, "qoder_endpoint", lambda: None)
    monkeypatch.setattr(dc, "http_get", lambda *args: (200, '{"status":"UP"}'))
    items = dc.scan_a(timeout=1.0)
    assert items[0]["ok"] is True
    assert items[1]["ok"] is False
    dc._print_report(items, [])
    out = capsys.readouterr().out
    assert "绝不能外泄的占位值" not in out
    assert "凭据：已配置" in out


def test_scan_b_finds_extensionless_entry(monkeypatch: Any, tmp_path: Path) -> None:
    (tmp_path / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(dc.shutil, "which", lambda _name: None)
    monkeypatch.setenv("PATH", str(tmp_path))
    found = dc._which_loose("hermes")
    assert found is not None
    assert found.endswith("hermes")


def test_main_json_output(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(
        dc,
        "scan_a",
        lambda timeout: [{"name": "示例桌面版", "ok": True, "checks": []}],
    )
    monkeypatch.setattr(
        dc, "scan_b", lambda: [{"name": "示例工具", "ok": True, "protocols": []}]
    )
    assert dc.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["a"][0]["ok"] is True
    assert data["b"][0]["name"] == "示例工具"


def test_main_report_is_chinese(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(dc, "scan_a", lambda timeout: [])
    monkeypatch.setattr(dc, "scan_b", lambda: [])
    assert dc.main([]) == 0
    out = capsys.readouterr().out
    assert "A 层" in out
    assert "B 层" in out
    assert "只读探活" in out
