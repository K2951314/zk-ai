"""mcp_call 的离线测试：白名单、路径边界、失败回填。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.services.agent import mcp_call
from app.services.agent.cli import mcp_client
from app.services.agent.cli.registry import CliRegistry, CliToolConfig
from app.services.agent.tools import ToolBox, ToolError


def _registry(*, tools: list[str] | None = None) -> CliRegistry:
    config = CliToolConfig(
        name="editor-sdk",
        command="",
        backend="mcp",
        model="",
        cost_tier="free",
        timeout=30.0,
        approval_mode="plan",
        mcp_url="http://127.0.0.1:39099/mcp",
        mcp_tool="get_pool_status",
        mcp_tools=tools if tools is not None else ["get_pool_status", "save_file"],
    )
    return CliRegistry(tools={"editor-sdk": config}, roles={})


def test_schema_absent_without_whitelist() -> None:
    assert mcp_call.build_schema(_registry(tools=[])) is None


def test_schema_lists_whitelisted_tools() -> None:
    schema = mcp_call.build_schema(_registry())
    assert schema is not None
    params = schema["function"]["parameters"]["properties"]
    assert params["server"]["enum"] == ["editor-sdk"]
    assert params["tool"]["enum"] == ["get_pool_status", "save_file"]


def test_schema_includes_doc_and_slide_tools() -> None:
    schema = mcp_call.build_schema(
        _registry(tools=["doc_insert_markdown", "slide_add_slide"])
    )
    assert schema is not None
    enum = schema["function"]["parameters"]["properties"]["tool"]["enum"]
    assert set(enum) == {"doc_insert_markdown", "slide_add_slide"}


def test_description_documents_create_then_open() -> None:
    schema = mcp_call.build_schema(_registry())
    assert schema is not None
    description = schema["function"]["description"]
    assert "必须再 open_file" in description
    assert "先 save_file 再 close_file" in description


def test_prepare_rejects_tool_outside_whitelist(tmp_path: Path) -> None:
    box = ToolBox(tmp_path)
    with pytest.raises(ToolError):
        mcp_call.prepare(
            _registry(),
            box,
            {"server": "editor-sdk", "tool": "delete_everything", "arguments": {}},
        )


def test_prepare_rejects_unknown_server(tmp_path: Path) -> None:
    box = ToolBox(tmp_path)
    with pytest.raises(ToolError):
        mcp_call.prepare(
            _registry(),
            box,
            {"server": "不存在", "tool": "save_file", "arguments": {}},
        )


def test_prepare_rejects_path_outside_task_space(tmp_path: Path) -> None:
    box = ToolBox(tmp_path)
    with pytest.raises(ToolError):
        mcp_call.prepare(
            _registry(),
            box,
            {
                "server": "editor-sdk",
                "tool": "save_file",
                "arguments": {"file_path": str(tmp_path / "主仓库.xlsx")},
            },
        )


def test_prepare_allows_path_inside_task_space(tmp_path: Path) -> None:
    box = ToolBox(tmp_path)
    inside = tmp_path / ".taskspaces" / "t1" / "outputs" / "报表.xlsx"
    outcome = mcp_call.prepare(
        _registry(),
        box,
        {
            "server": "editor-sdk",
            "tool": "save_file",
            "arguments": {"file_path": str(inside)},
        },
    )
    assert outcome.needs_approval is True
    assert "save_file" in str((outcome.display or {}).get("command"))


def test_prepare_requires_dict_arguments(tmp_path: Path) -> None:
    box = ToolBox(tmp_path)
    with pytest.raises(ToolError):
        mcp_call.prepare(
            _registry(),
            box,
            {"server": "editor-sdk", "tool": "save_file", "arguments": "不是对象"},
        )


def test_perform_calls_tool(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def fake_call(url: str, tool: str, args: dict[str, Any], **kwargs: Any) -> str:
        calls.append((url, tool, args))
        return "已保存"

    monkeypatch.setattr(mcp_client, "call_tool", fake_call)
    box = ToolBox(tmp_path)
    inside = tmp_path / ".taskspaces" / "t1" / "outputs" / "a.xlsx"
    outcome = __import__("asyncio").run(
        mcp_call.perform(
            _registry(),
            box,
            {
                "server": "editor-sdk",
                "tool": "save_file",
                "arguments": {"file_path": str(inside)},
            },
        )
    )
    assert outcome.ok is True
    assert outcome.output == "已保存"
    assert calls[0][1] == "save_file"


def test_open_file_gets_wait_true_by_default(tmp_path: Path) -> None:
    box = ToolBox(tmp_path)
    inside = tmp_path / ".taskspaces" / "t1" / "outputs" / "a.xlsx"
    registry = _registry(tools=["open_file"])
    outcome = mcp_call.prepare(
        registry,
        box,
        {
            "server": "editor-sdk",
            "tool": "open_file",
            "arguments": {"file_path": str(inside)},
        },
    )
    assert outcome.needs_approval is True
    assert "wait" in str((outcome.display or {}).get("command"))


def test_open_file_keeps_explicit_wait_false(tmp_path: Path) -> None:
    box = ToolBox(tmp_path)
    inside = tmp_path / ".taskspaces" / "t1" / "outputs" / "a.xlsx"
    registry = _registry(tools=["open_file"])
    outcome = mcp_call.prepare(
        registry,
        box,
        {
            "server": "editor-sdk",
            "tool": "open_file",
            "arguments": {"file_path": str(inside), "wait": False},
        },
    )
    assert '"wait": false' in str((outcome.display or {}).get("command")).lower()


def test_perform_reports_mcp_error(monkeypatch: Any, tmp_path: Path) -> None:
    async def boom(*args: Any, **kwargs: Any) -> str:
        raise mcp_client.McpError("工具报错")

    monkeypatch.setattr(mcp_client, "call_tool", boom)
    box = ToolBox(tmp_path)
    inside = tmp_path / ".taskspaces" / "t1" / "outputs" / "a.xlsx"
    outcome = __import__("asyncio").run(
        mcp_call.perform(
            _registry(),
            box,
            {
                "server": "editor-sdk",
                "tool": "save_file",
                "arguments": {"file_path": str(inside)},
            },
        )
    )
    assert outcome.ok is False
    assert "MCP 调用失败" in outcome.output
