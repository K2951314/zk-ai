"""``mcp_call`` 工具：让模型按需调用白名单里的 MCP 工具。

与 ``delegate_cli`` 的分工：
- ``delegate_cli`` 是「把一整段活交给某个执行体」，执行体自己决定怎么干；
- ``mcp_call`` 是「直接调一个具体工具」，参数由模型给。

为什么需要它：编辑器是**有状态**的（create/open 返回 ``file_id``，后续读写都要带着，
最后 save/close），一个执行体只绑一个工具根本编排不了。所以给模型一个通用入口，
但用**白名单**限制它能碰哪些工具——写类工具必须人工列进配置，等于一份权限清单。

安全边界：参数里的路径字段必须落在 ``.taskspaces/`` 之内，越界直接拒绝，
不让写操作碰到主仓库。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.agent import taskspace
from app.services.agent.cli import mcp_client
from app.services.agent.cli.registry import CliRegistry, CliToolConfig
from app.services.agent.tools import ToolBox, ToolError, ToolOutcome

TOOL_NAME = "mcp_call"

#: 参数里可能出现的路径字段：必须落在任务空间根之内。
_PATH_KEYS = ("file_path", "path", "image", "image_path")


def _servers(registry: CliRegistry) -> dict[str, tuple[CliToolConfig, list[str]]]:
    """收集所有开了 ``mcp_tools`` 白名单的 MCP 执行体。"""
    found: dict[str, tuple[CliToolConfig, list[str]]] = {}
    for name, config in registry.tools.items():
        if config.backend == "mcp" and config.mcp_tools:
            found[name] = (config, list(config.mcp_tools))
    return found


def build_schema(registry: CliRegistry) -> dict[str, Any] | None:
    """按白名单生成 schema；没有可用服务时返回 None（工具不出现）。"""
    servers = _servers(registry)
    if not servers:
        return None
    tools: list[str] = []
    for _, names in servers.values():
        tools.extend(names)
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "直接调用一个本地编辑器工具（表格/文档/幻灯片）。适合有状态的多步操作。"
                "正确顺序：① create_* 生成空文件，或 open_file 打开已有文件，拿到 file_id；"
                "② **create_* 之后必须再 open_file 一次**（带 wait，会自动补），编辑器才真正加载；"
                "③ 用 file_id 读/写；④ save_file 保存到任务文件夹；⑤ close_file 关闭。"
                "有改动时先 save_file 再 close_file，否则改动会丢。"
                "写操作只能作用于当前任务文件夹（.taskspaces 下），越界会被拒绝。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "enum": sorted(servers),
                        "description": "调用哪个 MCP 服务。",
                    },
                    "tool": {
                        "type": "string",
                        "enum": sorted(set(tools)),
                        "description": "要调用的工具名，必须在白名单里。",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "工具参数（JSON 对象），按工具 schema 给。",
                    },
                },
                "required": ["server", "tool", "arguments"],
            },
        },
    }


def _resolve(registry: CliRegistry, server: str, tool: str) -> CliToolConfig:
    pair = _servers(registry).get(server)
    if pair is None:
        raise ToolError(f"没有这个 MCP 服务：{server!r}")
    config, names = pair
    if tool not in names:
        raise ToolError(f"工具 {tool!r} 不在 {server} 的白名单里，拒绝调用")
    return config


def _check_paths(arguments: dict[str, Any], workspace: Path) -> None:
    """写操作只能落在任务空间根内；越界直接拒绝。"""
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        target = Path(value)
        if not target.is_absolute():
            target = workspace / target
        if not taskspace.is_inside(workspace, target):
            raise ToolError(
                f"参数 {key} 指向任务文件夹之外（{value!r}），为安全起见拒绝执行。"
                "请把文件放到当前任务的 outputs/ 或 work/ 目录下。"
            )


def _with_defaults(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """给已知工具补默认参数。

    ``open_file`` 默认只是「开始加载」就返回，模型拿到 ``open started`` 马上去读会报
    ``No workbook open``；实测传 ``wait=true`` 会阻塞到真正打开完成。模型几乎总是
    紧接着就要读/写，所以这里替它补上，不让它记这个坑。
    """
    if tool == "open_file" and "wait" not in arguments:
        return {**arguments, "wait": True}
    return arguments


def _parse_args(args: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    server = str(args.get("server") or "").strip()
    tool = str(args.get("tool") or "").strip()
    raw = args.get("arguments")
    if not isinstance(raw, dict):
        raise ToolError("arguments 必须是 JSON 对象")
    if not server or not tool:
        raise ToolError("server 和 tool 都不能为空")
    return server, tool, raw


def prepare(
    registry: CliRegistry, toolbox: ToolBox, args: dict[str, Any]
) -> ToolOutcome:
    """审批前：校验服务/工具/路径，渲染预览，不做任何调用。"""
    server, tool, arguments = _parse_args(args)
    config = _resolve(registry, server, tool)
    arguments = _with_defaults(tool, arguments)
    _check_paths(arguments, toolbox.workspace)
    preview = (
        f"调用 MCP 工具 {tool}（服务 {server}）\n"
        f"地址：{config.mcp_url}\n"
        f"参数：{json.dumps(arguments, ensure_ascii=False)[:500]}"
    )
    return ToolOutcome(
        needs_approval=True,
        display={"kind": TOOL_NAME, "command": preview, "tool": tool, "server": server},
    )


async def perform(
    registry: CliRegistry, toolbox: ToolBox, args: dict[str, Any]
) -> ToolOutcome:
    """批准后：真正调用工具。"""
    server, tool, arguments = _parse_args(args)
    config = _resolve(registry, server, tool)
    arguments = _with_defaults(tool, arguments)
    _check_paths(arguments, toolbox.workspace)
    try:
        output = await mcp_client.call_tool(
            config.mcp_url, tool, arguments, timeout=config.timeout
        )
    except mcp_client.McpError as exc:
        return ToolOutcome(ok=False, output=f"MCP 调用失败：{exc}")
    return ToolOutcome(ok=True, output=output)
