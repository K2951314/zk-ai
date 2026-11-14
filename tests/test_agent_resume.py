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
    """没有续接能力的适配器（gemini）给 ref 也不能瞎拼旗标。

    2026-11-14 修正：原测试拿 codex 当反例，但当天实测 ``exec resume <id>``
    可用、``--json`` 也能拿到 thread_id，codex 已改成支持续接（见文件末尾）。
    现在用 gemini 当反例——它确实没有续接入口。
    """
    from app.services.agent.cli.adapters import GeminiAdapter

    cfg = CliToolConfig(
        name="gemini", command="gemini", backend="official", model="gemini-2.5-flash",
        cost_tier="free", timeout=300, approval_mode="default",
    )
    argv = GeminiAdapter(cfg, resume_ref="some-id").build_command("x", "/w")
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


# --------------------------------------------------------------------------- #
# codex：续接是**子命令**（exec resume <id>），不是 --resume 旗标（2026-11-14 实测）
# --------------------------------------------------------------------------- #
def _codex() -> CliToolConfig:
    return CliToolConfig(
        name="codex", command="codex", backend="official", model="zk-auto",
        cost_tier="paid", timeout=300, approval_mode="plan",
    )


def test_codex_resume_is_a_subcommand_not_a_flag() -> None:
    """``codex exec resume <id> <task>`` —— 位置错一个词它就当子命令解析失败。"""
    argv = CodexAdapter(_codex(), resume_ref="01a1d7bf-0b14").build_command("干活", "/w")
    assert argv[:2] == ["codex", "exec"]
    assert argv[2] == "resume"
    assert argv[3] == "01a1d7bf-0b14"
    # 任务必须留在最后（resume 也是位置参数形式）
    assert argv[-1] == "干活"


def test_codex_resume_never_passes_the_sandbox_flag() -> None:
    """真机实测（2026-11-14）：``resume`` 不接受 ``-s``，带了直接退出码 2。

    只断言 argv 形状的测试抓不到这个——必须真跑一次才知道。
    """
    argv = CodexAdapter(_codex(), resume_ref="01a1d7bf-0b14").build_command("干活", "/w")
    assert "-s" not in argv and "--sandbox" not in argv
    # 而 --json 是 resume 支持的（拿 thread_id 与用量都靠它）
    assert "--json" in argv


def test_codex_without_a_ref_is_unchanged_apart_from_json() -> None:
    argv = CodexAdapter(_codex()).build_command("干活", "/w")
    assert "resume" not in argv
    # --json 是为了拿 thread_id 与用量（2026-11-14 加）
    assert "--json" in argv


def test_codex_session_ref_comes_from_thread_started() -> None:
    """``--json`` 第一行就是 thread.started，续接靠它。"""
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"01a1d7bf-0b14-7ee1-9bc5-13e9f7f49ae3"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"OK"}}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":7}}',
        ]
    )
    adapter = CodexAdapter(_codex())
    assert adapter.session_ref(stdout) == "01a1d7bf-0b14-7ee1-9bc5-13e9f7f49ae3"
    assert adapter.parse_output(stdout, "") == "OK（上游自报 token：入 100 / 出 7）"
    assert adapter.parse_usage(stdout, "") == (100, 7, None)


def test_codex_falls_back_to_raw_text_when_json_is_missing() -> None:
    """格式变了不能把整次委派判死。"""
    adapter = CodexAdapter(_codex())
    assert adapter.parse_output("就是一段裸文本", "") == "就是一段裸文本"
    assert adapter.session_ref("就是一段裸文本") == ""


def test_qoder_has_no_reliable_session_ref_so_it_stays_unsupported() -> None:
    """qoder 的 json 输出里**没有** session id（只能 --list-sessions 事后查）。

    并发时无法确定哪条是刚才的，所以刻意不实现续接——宁可没有，也不要接错。
    """
    from app.services.agent.cli.adapters import QoderAdapter

    cfg = CliToolConfig(
        name="qoder", command="qodercli", backend="official", model="Sonus",
        cost_tier="free", timeout=300, approval_mode="plan",
    )
    argv = QoderAdapter(cfg, resume_ref="fe152bb9-8d44").build_command("x", "/w")
    assert "fe152bb9-8d44" not in argv
