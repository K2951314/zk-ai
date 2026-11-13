"""delegate_cli 工具实现：把任务委派给外部 CLI 执行体。

定位（与 tools.py 六个工作区工具并列的第七个工具）：
- 危险工具：走与 write_file/run_command 相同的两阶段审批
  （先渲染预览给操作者，批准后才真正拉起子进程）
- 工作目录复用 ToolBox.resolve 的工作区防护，越界直接拒
- 工具存在与否取决于注册表：没配 cli_tools.yaml 时 schema 不出现，
  模型根本不知道有这个东西（与 spawn_subagent 对子会话的隐藏同款思路）
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.services.agent import usage_ledger
from app.services.agent.cli.base import build_adapter
from app.services.agent.cli.registry import CliRegistry, CliToolConfig
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


def escalated(config: CliToolConfig, *, allow_bypass: bool) -> CliToolConfig:
    """把执行体的审批档提到 bypass（若允许）。**预览与执行必须共用这一份**。

    2026-11-13 修：提权原来只在 ``runner.run_delegation`` 里做，而审批卡用的是
    未提权的 config 渲染——运营者批准的是「只读分析」，真正跑起来却是
    ``--dangerously-skip-permissions``。批准和执行必须是同一个配置对象，
    否则审批卡就成了一句不成立的承诺。
    """
    if allow_bypass and config.approval_mode != "bypass":
        return replace(config, approval_mode="bypass")
    return config


def prepare(
    registry: CliRegistry,
    toolbox: ToolBox,
    args: dict[str, Any],
    *,
    allow_bypass: bool = False,
) -> ToolOutcome:
    """审批前：解析角色与工作目录，渲染预览，不做任何执行。

    ``allow_bypass`` 必须与随后传给 :func:`perform` 的值一致——预览要展示
    **真正将要执行**的命令，不是配置里的名义档位。
    """
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
    effective = escalated(config, allow_bypass=allow_bypass)
    preview = build_adapter(effective).preview(task, str(cwd))
    return ToolOutcome(
        needs_approval=True,
        display={
            "kind": TOOL_NAME,
            "command": preview,
            "role": role,
            "tool": effective.name,
            "cost_tier": effective.cost_tier,
            "approval_mode": effective.approval_mode,
        },
    )


def budget_block(config: CliToolConfig) -> str:
    """检查今天的用量是否已到上限；超了返回中文说明，没超返回空串。

    为什么必须在**事前**（2026-11-13）：现在的唯一防线是撞了 429 之后
    ``classify_failure`` 把它归成 QUOTA——那是事后解释，额度已经烧掉了。
    Gemini 免费档实测 40 分钟就能打空，CodeBuddy 的免费池也会静默耗尽。

    阈值来自 ``cli_tools.yaml`` 的 ``daily_calls`` / ``daily_cost_usd``，
    **0 = 不限**（默认），所以不配就完全回到旧行为。
    读账本失败一律放行——不能因为账本坏了把功能整个锁死。
    """
    if config.daily_calls <= 0 and config.daily_cost_usd <= 0:
        return ""
    try:
        calls = usage_ledger.calls_today(config.name)
        if config.daily_calls > 0 and calls >= config.daily_calls:
            return (
                f"执行体 {config.name} 今天已派 {calls} 次，达到上限 "
                f"{config.daily_calls} 次。请改用其他角色，或等明天额度重置；"
                "确实需要更多次数就去 config/cli_tools.yaml 调高 daily_calls。"
            )
        if config.daily_cost_usd > 0:
            spent = usage_ledger.cost_today(config.name)
            if spent >= config.daily_cost_usd:
                return (
                    f"执行体 {config.name} 今天已花 ${spent:.2f}，达到上限 "
                    f"${config.daily_cost_usd:.2f}。请改用其他角色，或等明天；"
                    "注意只有执行体自报成本的调用才计入。"
                )
    except Exception:  # 账本读不出来不能把委派锁死
        return ""
    return ""


async def perform(
    registry: CliRegistry,
    toolbox: ToolBox,
    args: dict[str, Any],
    *,
    allow_bypass: bool = False,
    session_id: str = "",
    task_space: str = "",
    cancel: Any = None,
    on_line: Any = None,
    idle_timeout: float | None = None,
    resume_ref: str = "",
) -> ToolOutcome:
    """批准后：真正拉起外部执行体跑任务，把结果（或分类后的失败）回填。

    ``allow_bypass`` 由服务层按「cwd 是否在任务空间内」判定；这里只负责透传，
    不自己判断——边界判断只应有一个来源。

    ``session_id`` / ``task_space`` 只用于记账（谁在哪个任务里烧了多少）；
    缺省空串表示调用方没传，账本照记但不带归属。

    ``cancel`` / ``on_line`` / ``idle_timeout`` 于 2026-11-13 透传：模型的
    delegate_cli 之前一个都没接，于是「停止」按钮对它无效（子进程继续跑到底、
    继续烧额度），实时日志也看不到。执行句柄要归调用链最上层所有。
    """
    role = str(args.get("role") or "").strip()
    task = str(args.get("task") or "").strip()
    config = registry.tool_for_role(role)
    if config is None:
        raise ToolError(f"角色 {role!r} 已不可用（配置可能在审批后被改动）")
    blocked = budget_block(config)
    if blocked:
        return ToolOutcome(ok=False, output=blocked)
    cwd = toolbox.resolve(str(args.get("cwd") or "."))
    if not cwd.is_dir():
        raise ToolError(f"工作目录不存在或不是目录：{args.get("cwd")!r}")
    result = await run_delegation(
        config,
        task=task,
        cwd=cwd,
        allow_bypass=allow_bypass,
        cancel=cancel,
        on_line=on_line,
        idle_timeout=idle_timeout,
        resume_ref=resume_ref,
    )
    # 记账失败不影响主流程（usage_ledger 自己吞异常）。
    # CLI 没有价目表，这条记录就是「实测价格」的唯一来源。
    usage_ledger.record_delegation(
        tool=config.name,
        model=config.model,
        role=role,
        session_id=session_id,
        task_space=task_space,
        ok=result.ok,
        elapsed=result.elapsed,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=result.cost_usd,
        session_ref=result.session_ref,
        error="" if result.ok else result.output[:200],
    )
    # 后缀只在成功时是「信息」；失败时把「耗时」挂在错误后面会稀释失败信号，
    # 而且退出码是排查的第一手线索（timeout/launcher 分支为 None，如实写「未知」）。
    if result.ok:
        suffix = f"\n（执行体 {config.name} 耗时 {result.elapsed:.1f}s）"
    else:
        code = result.exit_code if result.exit_code is not None else "未知"
        suffix = f"\n（执行体 {config.name} 失败于 {result.elapsed:.1f}s，退出码 {code}）"
    return ToolOutcome(
        ok=result.ok,
        output=result.output + suffix,
        # 把 ref 带上去，供服务层落进任务空间、下次派单时续接（B-4）。
        session_ref=result.session_ref if result.ok else "",
    )
