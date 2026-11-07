"""MCP 执行体的离线测试：不连真实服务、不发网络请求。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.services.agent.cli import mcp_client, runner
from app.services.agent.cli.base import build_adapter
from app.services.agent.cli.registry import CliToolConfig, _parse_tool


def _cfg(**overrides: Any) -> CliToolConfig:
    base: dict[str, Any] = {
        "name": "editor-sdk",
        "command": "",
        "backend": "mcp",
        "model": "",
        "cost_tier": "free",
        "timeout": 30.0,
        "approval_mode": "plan",
        "mcp_url": "http://127.0.0.1:39099/mcp",
        "mcp_tool": "get_pool_status",
    }
    base.update(overrides)
    return CliToolConfig(**base)


def test_mcp_tool_does_not_need_command() -> None:
    warnings: list[str] = []
    cfg = _parse_tool(
        "editor-sdk",
        {
            "enabled": True,
            "command": "",
            "backend": "mcp",
            "mcp_url": "http://127.0.0.1:39099/mcp",
            "mcp_tool": "get_pool_status",
            "cost_tier": "free",
        },
        warnings,
    )
    assert cfg is not None
    assert cfg.backend == "mcp"
    assert cfg.mcp_tool == "get_pool_status"
    assert warnings == []


def test_mcp_tool_requires_url_and_tool() -> None:
    warnings: list[str] = []
    cfg = _parse_tool(
        "editor-sdk",
        {"enabled": True, "backend": "mcp", "cost_tier": "free"},
        warnings,
    )
    assert cfg is None
    assert any("mcp_url" in item for item in warnings)


def test_mcp_url_must_be_http() -> None:
    warnings: list[str] = []
    cfg = _parse_tool(
        "editor-sdk",
        {
            "enabled": True,
            "backend": "mcp",
            "mcp_url": "file:///etc/passwd",
            "mcp_tool": "get_pool_status",
            "cost_tier": "free",
        },
        warnings,
    )
    assert cfg is None
    assert any("http" in item for item in warnings)


def test_task_arguments_empty_means_no_params() -> None:
    assert runner._task_arguments(_cfg(), "整理表格") == {}


def test_task_arguments_substitutes_nested_task() -> None:
    cfg = _cfg(mcp_args={"text": "{task}", "meta": {"note": "前缀 {task}"}})
    assert runner._task_arguments(cfg, "整理表格") == {
        "text": "整理表格",
        "meta": {"note": "前缀 整理表格"},
    }


def test_run_mcp_success(monkeypatch: Any) -> None:
    async def fake_call(*args: Any, **kwargs: Any) -> str:
        return "编辑器池：2 个空闲"

    monkeypatch.setattr(runner, "call_tool", fake_call)
    result = asyncio.run(runner._run_mcp(_cfg(), task="看看", cwd=Path(".")))
    assert result.ok is True
    assert "空闲" in result.output


def test_run_mcp_timeout(monkeypatch: Any) -> None:
    async def slow_call(*args: Any, **kwargs: Any) -> str:
        await asyncio.sleep(2)
        return "太慢"

    monkeypatch.setattr(runner, "call_tool", slow_call)
    result = asyncio.run(
        runner._run_mcp(_cfg(timeout=0.05), task="看看", cwd=Path("."))
    )
    assert result.ok is False
    assert "超时" in result.output


def test_run_mcp_classifies_quota(monkeypatch: Any) -> None:
    async def boom(*args: Any, **kwargs: Any) -> str:
        raise mcp_client.McpError("429 too many requests")

    monkeypatch.setattr(runner, "call_tool", boom)
    result = asyncio.run(runner._run_mcp(_cfg(), task="看看", cwd=Path(".")))
    assert result.ok is False
    assert "额度耗尽" in result.output


def test_payload_parses_plain_json() -> None:
    assert mcp_client._payload('{"id": 1, "result": {}}', 1) == {"id": 1, "result": {}}


def test_payload_parses_sse() -> None:
    text = 'event: message\ndata: {"id": 2, "result": {"content": []}}\n'
    assert mcp_client._payload(text, 2) == {"id": 2, "result": {"content": []}}


def test_check_url_rejects_non_http() -> None:
    with pytest.raises(mcp_client.McpError):
        mcp_client._check_url("file:///etc/passwd")


def test_extract_text_joins_text_blocks() -> None:
    result = {"content": [{"type": "text", "text": "第一行"}, {"type": "text", "text": "第二行"}]}
    assert mcp_client._extract_text(result) == "第一行\n第二行"


def test_build_adapter_returns_mcp_adapter() -> None:
    adapter = build_adapter(_cfg())
    assert type(adapter).__name__ == "McpAdapter"
    preview = adapter.preview("整理表格", "D:/工作区")
    assert "MCP 执行体" in preview
    assert "get_pool_status" in preview
    assert "整理表格" in preview
