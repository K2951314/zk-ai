"""外部 CLI 执行体适配层的离线测试。

覆盖：
- 注册表：配置解析、非法项拦截（档位/后端/审批模式/命令不存在）、角色映射、
  文件缺失时空册行为
- 错误分类：未登录 / 额度耗尽 / 普通失败三类，AUTH 优先级
- 适配器：qwen 走网关与官方两种形态的 argv 构造；通用兜底
- 委派工具：schema 枚举随注册表生成、未知角色拒绝、预览含成本档位
全部离线：不拉起任何真实子进程，不读真实 cli_tools.yaml。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.agent.cli.adapters import GenericAdapter, QwenAdapter
from app.services.agent.cli.base import FailureKind, classify_failure, failure_output
from app.services.agent.cli.delegate import TOOL_NAME, build_schema, prepare
from app.services.agent.cli.registry import (
    CliRegistry,
    CliToolConfig,
    RoleConfig,
    _parse_roles,
    _parse_tool,
)
from app.services.agent.tools import ToolBox, ToolError

# --------------------------------------------------------------------------- #
# 注册表：单工具解析
# --------------------------------------------------------------------------- #

def _enabled(**over):
    base = {
        "enabled": True,
        "command": "sh",           # 一定存在于 PATH，绕开「命令不存在」拦截
        "backend": "gateway",
        "model": "zk-auto",
        "cost_tier": "free",
        "timeout": 120,
        "approval_mode": "default",
    }
    base.update(over)
    return base


def test_parse_tool_enabled_ok():
    warnings: list[str] = []
    cfg = _parse_tool("qwen", _enabled(), warnings)
    assert cfg is not None and cfg.name == "qwen"
    assert cfg.cost_tier == "free" and cfg.timeout == 120
    assert warnings == []


def test_parse_tool_disabled_silent():
    warnings: list[str] = []
    assert _parse_tool("qwen", {"enabled": False}, warnings) is None
    assert warnings == []  # 未启用不算错


@pytest.mark.parametrize(
    "over,frag",
    [
        ({"cost_tier": "luxury"}, "cost_tier"),
        ({"backend": "moon"}, "backend"),
        ({"approval_mode": "yolo"}, "approval_mode"),   # 自动同意模式必须被硬拦
        ({"command": ""}, "command"),
        ({"model": ""}, "model"),
        ({"command": "definitely-not-exists-cmd-xyz"}, "找不到命令"),
    ],
)
def test_parse_tool_rejects_illegal(over, frag):
    warnings: list[str] = []
    assert _parse_tool("qwen", _enabled(**over), warnings) is None
    assert warnings and frag in warnings[0]


def test_parse_tool_timeout_clamped():
    warnings: list[str] = []
    cfg = _parse_tool("qwen", _enabled(timeout=99999), warnings)
    assert cfg is not None and cfg.timeout == 1800
    cfg2 = _parse_tool("qwen", _enabled(timeout=1), warnings)
    assert cfg2 is not None and cfg2.timeout == 10


# --------------------------------------------------------------------------- #
# 注册表：角色映射与查询
# --------------------------------------------------------------------------- #

def _tool(name="qwen") -> CliToolConfig:
    return CliToolConfig(
        name=name, command="sh", backend="gateway", model="zk-auto",
        cost_tier="free", timeout=60, approval_mode="default",
    )


def test_roles_pointing_to_missing_tool_dropped():
    tools = {"qwen": _tool()}
    warnings: list[str] = []
    roles = _parse_roles(
        {"cli-coder": {"tool": "qwen", "hint": "写代码"},
         "cli-ghost": {"tool": "claude", "hint": "不存在的工具"}},
        tools, warnings,
    )
    assert sorted(roles) == ["cli-coder"]
    assert any("cli-ghost" in w for w in warnings)


def test_registry_queries():
    reg = CliRegistry(
        tools={"qwen": _tool()},
        roles={"cli-coder": RoleConfig(name="cli-coder", tool="qwen", hint="写代码")},
    )
    assert reg.known_roles() == ["cli-coder"]
    assert reg.tool_for_role("cli-coder").name == "qwen"
    assert reg.tool_for_role("nope") is None
    assert "免费" in reg.describe_roles()


# --------------------------------------------------------------------------- #
# 错误分类
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,kind",
    [
        ("No auth type is selected. Please configure an auth type", FailureKind.AUTH),
        ("401 Unauthorized", FailureKind.AUTH),
        ("429 Too Many Requests", FailureKind.QUOTA),
        ("insufficient_quota: rpm exhausted", FailureKind.QUOTA),
        ("something weird happened", FailureKind.FAILED),
        # AUTH 优先：401 与 quota 字样同时出现时按未登录报，别让模型白重试
        ("401 quota exceeded", FailureKind.AUTH),
    ],
)
def test_classify_failure(text, kind):
    assert classify_failure(text) is kind


def test_failure_output_contains_hint():
    out = failure_output(FailureKind.QUOTA, "429 boom")
    assert "额度耗尽" in out and "改派其他角色" in out


# --------------------------------------------------------------------------- #
# 适配器命令构造
# --------------------------------------------------------------------------- #

def test_qwen_gateway_argv():
    cfg = CliToolConfig(
        name="qwen", command="qwen", backend="gateway", model="zk-auto",
        cost_tier="free", timeout=60, approval_mode="plan",
        base_url="http://127.0.0.1:8318/v1", api_key="zk-local",
    )
    argv = QwenAdapter(cfg).build_command("回答 1+1", "/workspace")
    assert argv[:1] == ["qwen"]
    assert "--auth-type" in argv and "openai" in argv
    assert argv[argv.index("--openai-base-url") + 1] == "http://127.0.0.1:8318/v1"
    assert argv[argv.index("-m") + 1] == "zk-auto"
    assert argv[argv.index("--approval-mode") + 1] == "plan"
    assert argv[-1] == "回答 1+1"


def test_qwen_official_argv_no_openai_flags():
    cfg = CliToolConfig(
        name="qwen", command="qwen", backend="official", model="qwen3-coder",
        cost_tier="free", timeout=60, approval_mode="default",
    )
    argv = QwenAdapter(cfg).build_command("任务", "/workspace")
    assert "--auth-type" not in argv and "--openai-base-url" not in argv


def test_generic_adapter_fallback():
    cfg = CliToolConfig(
        name="whatever", command="whatever", backend="gateway", model="m",
        cost_tier="free", timeout=60, approval_mode="default",
    )
    assert GenericAdapter(cfg).build_command("干活", "/workspace") == ["whatever", "干活"]


# --------------------------------------------------------------------------- #
# 委派工具：schema 与 prepare
# --------------------------------------------------------------------------- #

def _reg() -> CliRegistry:
    return CliRegistry(
        tools={"qwen": _tool()},
        roles={"cli-coder": RoleConfig(name="cli-coder", tool="qwen", hint="写代码")},
    )


def test_schema_enum_follows_registry():
    schema = build_schema(_reg())
    params = schema["function"]["parameters"]["properties"]
    assert params["role"]["enum"] == ["cli-coder"]
    assert schema["function"]["name"] == TOOL_NAME
    assert "免费" in params["role"]["description"]


def test_prepare_renders_preview(tmp_path: Path):
    tb = ToolBox(tmp_path)
    outcome = prepare(_reg(), tb, {"role": "cli-coder", "task": "写个函数"})
    assert outcome.needs_approval
    preview = (outcome.display or {}).get("command", "")
    assert "委派给外部执行体 qwen" in preview
    assert "免费" in preview and "写个函数" in preview


def test_prepare_rejects_unknown_role(tmp_path: Path):
    tb = ToolBox(tmp_path)
    with pytest.raises(ToolError, match="未知或不可用角色"):
        prepare(_reg(), tb, {"role": "ghost", "task": "写个函数"})


def test_prepare_rejects_empty_task(tmp_path: Path):
    tb = ToolBox(tmp_path)
    with pytest.raises(ToolError, match="task 不能为空"):
        prepare(_reg(), tb, {"role": "cli-coder", "task": "  "})


def test_prepare_rejects_escaping_cwd(tmp_path: Path):
    tb = ToolBox(tmp_path)
    with pytest.raises(ToolError, match="越出工作区"):
        prepare(_reg(), tb, {"role": "cli-coder", "task": "x", "cwd": "../etc"})
