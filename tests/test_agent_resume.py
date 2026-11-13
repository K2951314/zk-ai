"""委派续跑（B-4，2026-11-13）：跑到 80% 超时被杀，重派时接着上次跑。

``CliResult.session_ref`` 早就能从 claude 的 json 回执里拿到（2026-11-13 上半轮），
但一直没被用起来——超时只能整段重跑。这里钉三件事：

1. 只有 claude 会把 ref 变成 ``--resume``（其余适配器默认不支持）；
2. 位置在 ``-p`` 之前（claude 的 ``-p`` 是进入无头模式的开关）；
3. ref 只在**同一任务空间 + 同一执行体**里复用——换执行体/换空间都不接。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.services.agent import taskspace
from app.services.agent.cli.adapters import ClaudeAdapter, CodexAdapter
from app.services.agent.cli.base import build_adapter
from app.services.agent.cli.registry import CliToolConfig


def _claude(**over) -> CliToolConfig:
    base = CliToolConfig(
        name="claude", command="claude", backend="official", model="",
        cost_tier="paid", timeout=300, approval_mode="bypass",
    )
    return replace(base, **over) if over else base


def test_resume_args_is_empty_by_default() -> None:
    """没 ref = 全新会话，argv 与旧行为逐字一致。"""
    cfg = _claude()
    plain = ClaudeAdapter(cfg).build_command("干活", "/w")
    with_empty = ClaudeAdapter(cfg, resume_ref="").build_command("干活", "/w")
    assert plain == with_empty
    assert "--resume" not in plain


def test_claude_turns_a_ref_into_resume_before_the_print_flag() -> None:
    """``--resume <id>`` 必须在 ``-p`` 之前。"""
    cfg = _claude()
    ref = "46f23cb2-4f8b-408e-910c-0e3674542344"
    argv = ClaudeAdapter(cfg, resume_ref=ref).build_command("继续", "/w")
    assert argv.index("--resume") < argv.index("-p")
    assert argv[argv.index("--resume") + 1] == ref
    # 任务文本与输出格式不受影响
    assert argv[argv.index("-p") + 1] == "继续"
    assert argv[argv.index("--output-format") + 1] == "json"


def test_adapters_without_resume_support_ignore_the_ref() -> None:
    """codex 目前不解析 session id → 给 ref 也不能瞎拼旗标。

    ``exec resume`` 是子命令，位置和 ``--resume`` 不同，没实测过就不猜。
    """
    cfg = CliToolConfig(
        name="codex", command="codex", backend="official", model="zk-auto",
        cost_tier="paid", timeout=300, approval_mode="plan",
    )
    argv = CodexAdapter(cfg, resume_ref="some-id").build_command("x", "/w")
    assert "some-id" not in argv
    assert "resume" not in argv


def test_build_adapter_passes_the_ref_through() -> None:
    adapter = build_adapter(_claude(), resume_ref="abc-123")
    assert "--resume" in adapter.build_command("x", "/w")


# --------------------------------------------------------------------------- #
# 服务层：ref 只在「同一任务空间 + 同一执行体」里复用
# --------------------------------------------------------------------------- #
def test_resume_ref_requires_the_same_tool(tmp_path: Path) -> None:
    """claude 的 session id 给 codex 是无意义的。"""
    from app.services.agent.service import AgentService

    space = taskspace.create(tmp_path, session_id="s1", goal="g")
    taskspace.update(
        tmp_path, space.task_id, handoff_session="claude-session-uuid", handoff_tool="claude"
    )

    assert AgentService._resume_ref_for(tmp_path, space, "claude") == "claude-session-uuid"
    assert AgentService._resume_ref_for(tmp_path, space, "codex") == ""


def test_resume_ref_is_empty_without_a_space(tmp_path: Path) -> None:
    """没任务空间（显式 cwd 派单）就不续接——那是另一个任务。"""
    from app.services.agent.service import AgentService

    assert AgentService._resume_ref_for(tmp_path, None, "claude") == ""


def test_resume_ref_is_empty_when_nothing_was_recorded(tmp_path: Path) -> None:
    from app.services.agent.service import AgentService

    space = taskspace.create(tmp_path, session_id="s2", goal="g")
    assert AgentService._resume_ref_for(tmp_path, space, "claude") == ""


def test_resume_ref_is_scoped_per_task_space(tmp_path: Path) -> None:
    """A 空间的 ref 不能泄漏到 B 空间。"""
    from app.services.agent.service import AgentService

    space_a = taskspace.create(tmp_path, session_id="s3", goal="a")
    space_b = taskspace.create(tmp_path, session_id="s4", goal="b")
    taskspace.update(
        tmp_path, space_a.task_id, handoff_session="uuid-a", handoff_tool="claude"
    )

    assert AgentService._resume_ref_for(tmp_path, space_a, "claude") == "uuid-a"
    assert AgentService._resume_ref_for(tmp_path, space_b, "claude") == ""
