"""delegate_cli 工具实现：把任务委派给外部 CLI 执行体。

定位（与 tools.py 六个工作区工具并列的第七个工具）：
- 危险工具：走与 write_file/run_command 相同的两阶段审批
  （先渲染预览给操作者，批准后才真正拉起子进程）
- 工作目录复用 ToolBox.resolve 的工作区防护，越界直接拒
- 工具存在与否取决于注册表：没配 cli_tools.yaml 时 schema 不出现，
  模型根本不知道有这个东西（与 spawn_subagent 对子会话的隐藏同款思路）
"""
from __future__ import annotations

from typing import Any

from app.services.agent.cli.base import build_adapter
from app.services.agent.cli.registry import CliRegistry
from app.services.agent.cli.runner import run_delegation
from app.services.agent.tools import ToolBox, ToolError, ToolOutcome

TOOL_NAME = "delegate_cli"


def build_schema(registry: CliRegistry) -> dict[str, Any]:
    """按当前注册表生成工具 schema（角色清单写死进枚举，模型乱编直接 400）。"""
    roles = registry.known_roles()
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "把一段任务委派给本机的外部 AI 工具（独立命令行执行体）去做，"
                "完成后结果会回传给你。适合：需要另一种工具的专长，"
                "或这段活儿本身就该交给外部工具跑。"
                "委派需要用户批准；任务说明要自成一体（执行体看不到你的对话历史）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": roles,
                        "description": f"执行体角色。当前可用：{registry.describe_roles()}",
                    },
                    "task": {
                        "type": "string",
                        "description": "委派任务的完整说明，要脱离上下文也能读懂。",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "执行体的工作目录（相对工作区，默认工作区根目录）。",
                    },
                },
                "required": ["role", "task"],
            },
        },
    }


def prepare(registry: CliRegistry, toolbox: ToolBox, args: dict[str, Any]) -> ToolOutcome:
    """审批前：解析角色与工作目录，渲染预览，不做任何执行。"""
    role = str(args.get("role") or "").strip()
    task = str(args.get("task") or "").strip()
    if not task:
        raise ToolError("task 不能为空：执行体看不到你的对话历史，任务必须自带上下文。")
    config = registry.tool_for_role(role)
    if config is None:
        raise ToolError(
            f"未知或不可用角色 {role!r}。"
            f"可用角色：{registry.describe_roles() or "（空，检查 cli_tools.yaml）"}。"
            "请从清单里挑一个，不要自己编。"
        )
    cwd_raw = str(args.get("cwd") or ".")
    cwd = toolbox.resolve(cwd_raw)  # 越界在这里直接 ToolError
    if not cwd.is_dir():
        raise ToolError(f"工作目录不存在或不是目录：{cwd_raw!r}")
    preview = build_adapter(config).preview(task, str(cwd))
    return ToolOutcome(
        needs_approval=True,
        display={
            "kind": TOOL_NAME,
            "command": preview,
            "role": role,
            "tool": config.name,
            "cost_tier": config.cost_tier,
        },
    )


async def perform(registry: CliRegistry, toolbox: ToolBox, args: dict[str, Any]) -> ToolOutcome:
    """批准后：真正拉起外部执行体跑任务，把结果（或分类后的失败）回填。"""
    role = str(args.get("role") or "").strip()
    task = str(args.get("task") or "").strip()
    config = registry.tool_for_role(role)
    if config is None:
        raise ToolError(f"角色 {role!r} 已不可用（配置可能在审批后被改动）")
    cwd = toolbox.resolve(str(args.get("cwd") or "."))
    if not cwd.is_dir():
        raise ToolError(f"工作目录不存在或不是目录：{args.get("cwd")!r}")
    result = await run_delegation(config, task=task, cwd=cwd)
    suffix = f"\n（执行体 {config.name} 耗时 {result.elapsed:.1f}s）"
    return ToolOutcome(ok=result.ok, output=result.output + suffix)
