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

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.agent.cli import launcher, runner
from app.services.agent.cli.adapters import (
    ClaudeAdapter,
    CodeBuddyAdapter,
    CodexAdapter,
    GeminiAdapter,
    GenericAdapter,
    QoderAdapter,
    QwenAdapter,
)
from app.services.agent.cli.base import FailureKind, classify_failure, failure_output
from app.services.agent.cli.delegate import TOOL_NAME, build_schema, prepare
from app.services.agent.cli.models import parse_codebuddy_models, parse_qoder_models
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
        # 用解释器本身当「一定存在」的命令：`sh` 只在装了 Git-Bash 的机器上
        # 才在 PATH 里，Windows 原生环境会让这一组解析测试整体误报。
        "command": sys.executable,
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


def test_parse_tool_empty_model_means_cli_default():
    """空模型 = 用 CLI 自配模型（Claude Code 靠 CC Switch 选模型）。"""
    warnings: list[str] = []
    cfg = _parse_tool("claude", _enabled(model=""), warnings)
    assert cfg is not None and cfg.model == ""
    assert warnings == []


def test_parse_tool_bypass_mode_is_allowed():
    """bypass 是给 Claude Code 开的例外（它没有无头可用的只读档）。"""
    warnings: list[str] = []
    cfg = _parse_tool("claude", _enabled(approval_mode="bypass"), warnings)
    assert cfg is not None and cfg.approval_mode == "bypass"


@pytest.mark.parametrize(
    "over,frag",
    [
        ({"cost_tier": "luxury"}, "cost_tier"),
        ({"backend": "moon"}, "backend"),
        ({"approval_mode": "yolo"}, "approval_mode"),   # 自动同意模式必须被硬拦
        ({"command": ""}, "command"),
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


def test_parse_tool_effort_and_context_window():
    warnings: list[str] = []
    cfg = _parse_tool("qoder", _enabled(effort="MAX", context_window=1_000_000), warnings)
    assert cfg is not None
    assert cfg.effort == "max"  # 大小写归一
    assert cfg.context_window == 1_000_000
    assert warnings == []


def test_parse_tool_unknown_effort_warns_but_keeps_tool():
    """档位写错不该让整个执行体消失——按工具默认跑，并留下告警。"""
    warnings: list[str] = []
    cfg = _parse_tool("qoder", _enabled(effort="ultra"), warnings)
    assert cfg is not None and cfg.effort == ""
    assert any("effort" in w for w in warnings)


def test_parse_tool_context_window_sanitized():
    warnings: list[str] = []
    assert _parse_tool("qoder", _enabled(context_window=9_999_999), warnings).context_window == 2_000_000
    assert _parse_tool("qoder", _enabled(context_window=-5), warnings).context_window == 0
    assert _parse_tool("qoder", _enabled(context_window="abc"), warnings).context_window == 0


def test_parse_tool_models_and_free_models():
    cfg = _parse_tool(
        "codebuddy",
        _enabled(models=["deepseek-v4.1-flash", " glm-5.3 "], free_models=["deepseek-v4.1-flash"]),
        [],
    )
    assert cfg is not None
    assert cfg.models == ["deepseek-v4.1-flash", "glm-5.3"]
    assert cfg.free_models == ["deepseek-v4.1-flash"]


def test_parse_tool_models_not_a_list_is_ignored_with_warning():
    """写错类型不该让执行体消失，只是清单为空。"""
    warnings: list[str] = []
    cfg = _parse_tool("codebuddy", _enabled(models="a,b"), warnings)
    assert cfg is not None and cfg.models == []
    assert any("models" in w for w in warnings)


# --------------------------------------------------------------------------- #
# 模型发现：能用 CLI 自己的口径就别用手抄清单
# --------------------------------------------------------------------------- #

def test_parse_qoder_models_skips_header_and_blank():
    text = "MODEL\nAuto\nDeepSeek-Flash\n\nstep-5-preview (qoder-custom-xxx)\n"
    assert parse_qoder_models(text) == [
        "Auto",
        "DeepSeek-Flash",
        "step-5-preview (qoder-custom-xxx)",
    ]


def test_parse_codebuddy_models_from_help_text():
    text = (
        "--model <model>  Model for the current session. Please provide the model ID. "
        "Currently supported: (default-model, fast-model, glm-5.3, deepseek-v4.1-flash)"
    )
    assert parse_codebuddy_models(text) == [
        "default-model", "fast-model", "glm-5.3", "deepseek-v4.1-flash",
    ]


def test_parse_codebuddy_models_missing_returns_empty():
    assert parse_codebuddy_models("没有任何清单") == []


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
        # 2026-10-25 真实输出：CodeBuddy / Qoder 的未登录原文，必须按 AUTH 报
        ("Authentication required. Please use /login command to sign in to your account", FailureKind.AUTH),
        ("Not logged in. Run `qodercli login` to authenticate.", FailureKind.AUTH),
        # 2026-10-26 实测：Gemini CLI 登录态过期时只弹这一句（stdin=DEVNULL 后照旧退出 1）
        ("Opening authentication page in your browser. Do you want to continue? [Y/n]:",
         FailureKind.AUTH),
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


def test_codebuddy_auth_text_is_detected_even_with_exit_zero():
    """CodeBuddy 2.176.0：未登录时退出码 0、只吐一行纯文本。

    2026-10-26 实测：控制台把这种委派显示成「成功」，正文却是登录提示，
    父模型会把它当成任务结果。钩子只认它自己的登录原文，避免把正常回答
    （里面也可能出现 401 之类字样）误判成失败。
    """
    adapter = CodeBuddyAdapter(_cli("codebuddy", "codebuddy", "glm-5.2", "plan"))
    text = "Authentication required. Please use /login command to sign in to your account"
    assert adapter.fatal_output(text, "") is FailureKind.AUTH
    assert adapter.fatal_output('[{"type":"result","result":"pong"}]', "") is None
    assert adapter.fatal_output("", "") is None


class _FakeReader:
    def __init__(self, data: bytes) -> None:
        self._data, self._pos = data, 0

    async def read(self, size: int) -> bytes:
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


class _FakeProc:
    def __init__(self, out: bytes, code: int = 0) -> None:
        self.stdout, self.stderr = _FakeReader(out), _FakeReader(b"")
        self.returncode = code

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


async def test_runner_treats_auth_text_with_exit_zero_as_failure(monkeypatch):
    """接线测试：适配器认出登录提示还不够，runner 必须真的记成失败。"""
    cfg = _cli("codebuddy", "codebuddy", "glm-5.2", "plan")
    proc = _FakeProc(b"Authentication required. Please use /login command to sign in")

    async def fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(runner, "resolve_command", lambda argv: argv)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_exec)
    result = await runner.run_delegation(cfg, task="pong", cwd=Path("."))
    assert result.ok is False
    assert result.failure is FailureKind.AUTH
    assert "未登录" in result.output


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


def _cli(name: str, command: str, model: str, mode: str) -> CliToolConfig:
    return CliToolConfig(
        name=name, command=command, backend="official", model=model,
        cost_tier="free", timeout=60, approval_mode=mode,
    )


def test_qoder_plan_mode_never_passes_default():
    """plan 必须映射成 dont_ask：透传 default 会在无头场景等审批等到超时。"""
    argv = QoderAdapter(_cli("qoder", "qoder", "auto", "plan")).build_command("整理清单", "/workspace")
    assert argv[argv.index("--permission-mode") + 1] == "dont_ask"
    assert argv[argv.index("--model") + 1] == "auto"
    assert argv[-1] == "整理清单" and "--print" in argv


def test_qoder_default_mode_passthrough():
    argv = QoderAdapter(_cli("qoder", "qoder", "auto", "default")).build_command("x", "/workspace")
    assert argv[argv.index("--permission-mode") + 1] == "default"


def test_qoder_effort_and_context_window_flags():
    cfg = replace(_cli("qoder", "qoder", "DeepSeek-Flash", "plan"), effort="max", context_window=1_000_000)
    argv = QoderAdapter(cfg).build_command("整理", "/workspace")
    assert argv[argv.index("--reasoning-effort") + 1] == "max"
    assert argv[argv.index("--context-window") + 1] == "1000000"


def test_codebuddy_effort_and_autocompact_flags():
    cfg = replace(
        _cli("codebuddy", "codebuddy", "deepseek-v4.1-flash", "plan"),
        effort="max",
        context_window=1_000_000,
    )
    argv = CodeBuddyAdapter(cfg).build_command("整理", "/workspace")
    assert argv[argv.index("--effort") + 1] == "max"
    assert argv[argv.index("--autocompact") + 1] == "1000000"


def test_effort_and_context_default_to_tool_defaults():
    """不填 = 不传参数，交给工具自己的默认档。"""
    argv = QoderAdapter(_cli("qoder", "qoder", "DeepSeek-Flash", "plan")).build_command("x", "/w")
    assert "--reasoning-effort" not in argv and "--context-window" not in argv
    argv = CodeBuddyAdapter(_cli("codebuddy", "codebuddy", "glm-5.2", "plan")).build_command("x", "/w")
    assert "--effort" not in argv and "--autocompact" not in argv


def test_codebuddy_argv_uses_json_output_and_plan():
    argv = CodeBuddyAdapter(_cli("codebuddy", "codebuddy", "glm-5.2", "plan")).build_command(
        "整理清单", "/workspace"
    )
    assert argv[:1] == ["codebuddy"]
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "glm-5.2"
    assert argv[-1] == "整理清单"


def test_codebuddy_parse_output_extracts_result_and_usage():
    adapter = CodeBuddyAdapter(_cli("codebuddy", "codebuddy", "glm-5.2", "plan"))
    payload = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "pong",
            "usage": {"input_tokens": 12, "output_tokens": 3},
            "total_cost_usd": 0.0012,
        }
    )
    out = adapter.parse_output(payload, "")
    assert "pong" in out
    assert "入 12" in out and "出 3" in out
    assert "$0.0012" in out


def test_codebuddy_parse_output_falls_back_to_raw_text():
    adapter = CodeBuddyAdapter(_cli("codebuddy", "codebuddy", "glm-5.2", "plan"))
    assert adapter.parse_output("就是一段裸文本", "") == "就是一段裸文本"
    assert adapter.parse_output("{坏掉的 json", "") == "{坏掉的 json"


def test_codebuddy_event_array_yields_only_the_result():
    """2.175.0 的真实形态是事件数组，前面几条 message 事件带着完整 system prompt。"""
    adapter = CodeBuddyAdapter(_cli("codebuddy", "codebuddy", "glm-5.2", "plan"))
    payload = json.dumps(
        [
            {"type": "message", "content": [{"type": "input_text", "text": "AGENTS.md 全文…"}]},
            {"type": "reasoning", "text": "想想"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "pong",
                "total_cost_usd": 0,
                "usage": {"input_tokens": 46281, "output_tokens": 30},
            },
        ]
    )
    out = adapter.parse_output(payload, "")
    assert out.startswith("pong")
    assert "AGENTS.md" not in out
    assert "46281" in out and "$0.0000" in out


def test_codebuddy_parse_output_marks_upstream_error():
    adapter = CodeBuddyAdapter(_cli("codebuddy", "codebuddy", "glm-5.2", "plan"))
    out = adapter.parse_output(json.dumps({"result": "被拦了", "is_error": True}), "")
    assert "is_error=true" in out


def test_codex_plan_mode_uses_read_only_sandbox():
    argv = CodexAdapter(_cli("codex", "codex", "zk-auto", "plan")).build_command(
        "查一下配置", "/workspace"
    )
    assert argv[:2] == ["codex", "exec"]
    assert "--skip-git-repo-check" in argv     # 工作区不一定是 git 仓库
    assert argv[argv.index("-m") + 1] == "zk-auto"
    assert argv[argv.index("-s") + 1] == "read-only"   # plan -> 只读沙箱
    assert argv[-1] == "查一下配置"


def test_codex_default_mode_has_no_sandbox_flag():
    argv = CodexAdapter(_cli("codex", "codex", "zk-auto", "default")).build_command("x", "/w")
    assert "-s" not in argv


def test_claude_bypass_with_empty_model_omits_model_flag():
    """空模型 -> 不传 --model（用 CC Switch 选定的那个）；bypass 才带跳过审批。"""
    argv = ClaudeAdapter(_cli("claude", "claude", "", "bypass")).build_command("评审方案", "/w")
    assert argv[0] == "claude"
    assert "--dangerously-skip-permissions" in argv
    assert "-p" in argv and argv[-1] == "评审方案"
    assert "--model" not in argv


def test_claude_explicit_model_and_plan_stay_minimal():
    argv = ClaudeAdapter(_cli("claude", "claude", "some-model", "plan")).build_command("x", "/w")
    assert argv[argv.index("--model") + 1] == "some-model"
    assert "--dangerously-skip-permissions" not in argv


def test_gemini_argv_plan_mode_and_json():
    argv = GeminiAdapter(_cli("gemini", "gemini", "gemini-2.5-flash", "plan")).build_command(
        "整理清单", "/workspace"
    )
    assert argv[:2] == ["gemini", "-p"]
    assert argv[argv.index("-m") + 1] == "gemini-2.5-flash"
    assert argv[argv.index("--approval-mode") + 1] == "plan"
    assert argv[argv.index("-o") + 1] == "json"


def test_gemini_env_makes_node_proxy_and_trust_work():
    adapter = GeminiAdapter(_cli("gemini", "gemini", "gemini-2.5-flash", "plan"))
    env = adapter.build_env()
    assert env is not None
    # Node 的 fetch 默认不读 HTTPS_PROXY，跨境访问必须靠这个开关
    assert env["NODE_USE_ENV_PROXY"] == "1"
    # 无头模式不信任任何目录，缺了它只回一句 not running in a trusted directory
    assert env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"


def test_gemini_parse_output_extracts_response_and_tokens():
    adapter = GeminiAdapter(_cli("gemini", "gemini", "gemini-2.5-flash", "plan"))
    payload = json.dumps(
        {
            "response": "pong",
            "stats": {"models": {"gemini-2.5-flash": {"tokens": {"prompt": 10, "candidates": 2}}}},
        }
    )
    out = adapter.parse_output(payload, "")
    assert "pong" in out and "入 10" in out and "出 2" in out


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


# --------------------------------------------------------------------------- #
# 命令解析：Windows 下 npm 的 .cmd shim 不能直接 exec
# --------------------------------------------------------------------------- #

_SHIM = (
    "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\n"
    "SETLOCAL\r\nCALL :find_dp0\r\n\r\n"
    'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
    '"%dp0%\\node_modules\\@tencent-ai\\codebuddy-code\\bin\\codebuddy" %*\r\n'
)


def _fake_which(mapping: dict[str, str]):
    def which(name: str) -> str | None:
        return mapping.get(name)

    return which


def test_cmd_shim_resolved_to_node_entry(tmp_path: Path, monkeypatch):
    shim = tmp_path / "codebuddy.cmd"
    shim.write_text(_SHIM, encoding="utf-8", newline="")
    entry = tmp_path / "node_modules" / "@tencent-ai" / "codebuddy-code" / "bin" / "codebuddy"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    monkeypatch.setattr(launcher.sys, "platform", "win32")

    argv = launcher.resolve_command(
        ["codebuddy", "--print", "只回复 pong"],
        which=_fake_which({"codebuddy": str(shim), "node": "C:/fake/node.exe"}),
    )
    assert argv == ["C:/fake/node.exe", str(entry), "--print", "只回复 pong"]


def test_cmd_shim_without_entry_raises(tmp_path: Path, monkeypatch):
    shim = tmp_path / "weird.cmd"
    shim.write_text("@echo off\r\nREM 没有 npm 的标准收尾行\r\n", encoding="utf-8", newline="")
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    with pytest.raises(launcher.LauncherError, match=r"原生 \.exe"):
        launcher.resolve_command(["weird"], which=_fake_which({"weird": str(shim)}))


def test_cmd_shim_pointing_at_exe_runs_it_directly(tmp_path: Path, monkeypatch):
    """Claude Code 的 npm shim 直接指向 bin/claude.exe——不能交给 node。"""
    shim = tmp_path / "claude.cmd"
    shim.write_text(
        '@ECHO off\r\nSETLOCAL\r\nendLocal & "%_prog%"  '
        '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe" %*\r\n',
        encoding="utf-8",
        newline="",
    )
    entry = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    monkeypatch.setattr(launcher.sys, "platform", "win32")

    argv = launcher.resolve_command(
        ["claude", "-p", "x"],
        which=_fake_which({"claude": str(shim), "node": "C:/fake/node.exe"}),
    )
    assert argv == [str(entry), "-p", "x"]


def test_plain_executable_passthrough(tmp_path: Path, monkeypatch):
    exe = tmp_path / "tool.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    assert launcher.resolve_command(["tool", "a"], which=_fake_which({"tool": str(exe)})) == [
        str(exe),
        "a",
    ]


def test_ps1_shim_goes_through_pwsh(tmp_path: Path, monkeypatch):
    script = tmp_path / "thing.ps1"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    argv = launcher.resolve_command(
        ["thing", "-x"], which=_fake_which({"thing": str(script), "pwsh": "C:/pwsh.exe"})
    )
    assert argv == ["C:/pwsh.exe", "-NoProfile", "-File", str(script), "-x"]
