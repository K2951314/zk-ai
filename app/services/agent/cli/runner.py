"""委派执行器：把一次外部 CLI 调用跑起来并收好结果。

安全与资源约束：
- argv 不经 shell（列表直传 exec），任务文本不会被当成命令拼接解析
- 超时硬上限来自执行体配置（注册表已压在 10~1800s 区间）
- stdout/stderr 分别限量读取，防止异常进程撑爆内存
- 工作目录由调用方（工具层）完成工作区内解析，这里不信任外来路径
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.services.agent.cli.base import (
    CliResult,
    FailureKind,
    build_adapter,
    classify_failure,
    clip_output,
    failure_output,
)
from app.services.agent.cli.launcher import LauncherError, resolve_command
from app.services.agent.cli.mcp_client import McpError, call_tool
from app.services.agent.cli.prompting import build_task_prompt
from app.services.agent.cli.registry import CliToolConfig

#: 单路输出最多读多少字节（超出即丢弃剩余，进程照常等死）
_MAX_STREAM_BYTES = 1_000_000
#: 超时/取消时回显的「已产生输出」长度上限（failure_output 会截到 1000 字，
#: 留 800 给正文，别让日志把失败原因挤掉）
_PARTIAL_OUTPUT_CHARS = 800
#: watchdog 轮询间隔
_WATCHDOG_INTERVAL = 1.0
#: ``--print-logs`` 等 INFO/DEBUG 日志行：只用于进度展示，不参与失败分类
_LOG_LEVEL_RE = re.compile(r"^\s*(INFO|DEBUG|TRACE)\s+\d{4}-", re.IGNORECASE)


def summarize_cli_line(line: str) -> str | None:
    """把 CLI 实时日志压成一句人话；非进度行返回 None。

    码道 26.11.1 的 ``--print-logs`` stderr 实测 **1786 行 / 42s**，全推会
    刷屏，日志里还带 URL 与代理地址。只挑「模型请求 / 工具调用 / 轮次 /
    检索」四类。纯函数，便于单测，也便于将来换 CLI 时只改这里。
    """
    text = line.strip()
    if not text:
        return None
    low = text.lower()
    if "chat/completions" in low or ("inferhub-provider" in low and "model-id" in low):
        return "正在请求模型…"
    if (
        "tool_request" in low
        or "tool execute start" in low
        or "toolexecutebefore" in low
    ):
        matched = re.search(r"tool[=:]([A-Za-z0-9_-]+)", text)
        return f"调用工具 {matched.group(1)}…" if matched else "调用工具…"
    if "step_finish" in low:
        return "模型完成一轮推理"
    if "step_start" in low:
        return "模型开始一轮推理"
    if "ripgrep start" in low:
        return "检索代码…"
    if '"type":"text"' in text or '"type": "text"' in text:
        return "模型正在输出…"
    return None


def _diagnostic_stderr(stderr: str) -> str:
    """剥掉 INFO/DEBUG 日志行，只留真正用于诊断的 stderr。

    ``--print-logs`` 会把「使用环境变量CODEARTS_CLI_AK/SK授权CLI」这类
    **正常**日志写进 stderr；不剥掉的话 ``fatal_output`` 会把成功的委派
    误判成「未登录」（AUTH），``classify_failure`` 也可能被日志里的数字带偏。
    """
    return "\n".join(ln for ln in stderr.splitlines() if not _LOG_LEVEL_RE.match(ln))


class _StreamSink:
    """一路输出：累计字节（超时后仍可回显）+ 逐行回调（推 SSE）+ 心跳。"""

    def __init__(
        self,
        *,
        on_line: Callable[[str, str], None] | None,
        name: str,
        touch: Callable[[], None],
    ) -> None:
        self.chunks: list[bytes] = []
        self._pending = b""
        self._on_line = on_line
        self._name = name
        self._touch = touch

    def feed(self, chunk: bytes) -> None:
        self.chunks.append(chunk)
        self._touch()
        if self._on_line is None:
            return
        self._pending += chunk
        while b"\n" in self._pending:
            raw, self._pending = self._pending.split(b"\n", 1)
            self._on_line(self._name, raw.decode("utf-8", errors="replace").rstrip("\r"))

    def flush(self) -> None:
        if self._on_line is not None and self._pending:
            self._on_line(self._name, self._pending.decode("utf-8", errors="replace").rstrip("\r"))
            self._pending = b""

    @property
    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", errors="replace")


async def _read_limited(stream: asyncio.StreamReader | None, sink: _StreamSink) -> str:
    """限量读一路输出；边读边逐行回调，字节留着供超时/取消时回显。"""
    if stream is None:
        return ""
    total = 0
    while True:
        try:
            chunk = await stream.read(65536)
        except Exception:
            break
        if not chunk:
            break
        sink.feed(chunk)
        total += len(chunk)
        if total >= _MAX_STREAM_BYTES:
            break
    sink.flush()
    return sink.text


def _substitute(value: Any, task: str) -> Any:
    """把参数模板里的 ``{task}`` 换成任务文本（递归，不做字符串拼接）。"""
    if isinstance(value, str):
        return value.replace("{task}", task)
    if isinstance(value, dict):
        return {key: _substitute(item, task) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, task) for item in value]
    return value


def _task_arguments(config: CliToolConfig, task: str) -> dict[str, Any]:
    """MCP 工具参数：空模板 = 无参数；要传任务就在模板里显式写 ``{task}``。

    不默认塞 ``{"text": task}``——不同工具的参数名不一样（有的叫 ``idx``、
    有的叫 ``range``），猜错会被工具当成非法参数拒绝。
    """
    if not config.mcp_args:
        return {}
    return _substitute(dict(config.mcp_args), task)


async def _run_mcp(config: CliToolConfig, *, task: str, cwd: Path) -> CliResult:
    """走 MCP：发 HTTP JSON-RPC，不拉子进程。"""
    del cwd  # 参数签名与子进程路径保持一致，MCP 不消费工作目录
    started = time.monotonic()
    arguments = _task_arguments(config, task)
    try:
        output = await asyncio.wait_for(
            call_tool(
                config.mcp_url,
                config.mcp_tool,
                arguments,
                timeout=config.timeout,
            ),
            timeout=config.timeout,
        )
    except TimeoutError:
        return CliResult(
            ok=False,
            output=failure_output(
                FailureKind.TIMEOUT,
                f"超过超时上限 {int(config.timeout)}s 未结束",
            ),
            failure=FailureKind.TIMEOUT,
            elapsed=time.monotonic() - started,
        )
    except McpError as exc:
        kind = classify_failure(str(exc))
        return CliResult(
            ok=False,
            output=failure_output(kind, str(exc)),
            failure=kind,
            elapsed=time.monotonic() - started,
        )
    elapsed = time.monotonic() - started
    return CliResult(ok=True, output=clip_output(output), elapsed=elapsed)


async def run_delegation(
    config: CliToolConfig,
    *,
    task: str,
    cwd: Path,
    allow_bypass: bool = False,
    on_line: Callable[[str, str], None] | None = None,
    cancel: asyncio.Event | None = None,
    idle_timeout: float | None = None,
    envelope: bool = True,
    resume_ref: str = "",
) -> CliResult:
    """执行一次委派：MCP 走 HTTP，其余拉起子进程、等它结束、分类结果。

    ``allow_bypass`` 由服务层按任务空间边界判定后传入；这里只把执行体的
    ``approval_mode`` 提到 ``bypass``，具体映射由各适配器负责。

    三个可选参数都是 2026-11-12 加的，默认全关，旧调用点/测试零改动：
    - ``on_line(stream, line)``：每读到一行就回调，服务层挑摘要推 SSE；
      纯本地管道 I/O，**不发任何额外上游请求、不额外消耗积分**。
    - ``idle_timeout``：有输出心跳就不砍，静默超过这个秒数才判挂死；
      ``None`` 表示只看总时长。总时长仍由 ``config.timeout`` 兜底。
    - ``cancel``：被 set 时真正 ``kill`` 子进程（不再只是改会话状态）。

    ``envelope``（2026-11-13）：给子进程类执行体套上「工作目录 / 无头不可追问 /
    交付契约」三行信封，默认开启；MCP 后端不受影响（见下）。测试里想断言
    原始 argv 时传 ``envelope=False``。
    """
    if allow_bypass and config.approval_mode != "bypass":
        config = replace(config, approval_mode="bypass")
    if config.backend == "mcp":
        return await _run_mcp(config, task=task, cwd=cwd)
    adapter = build_adapter(config, resume_ref=resume_ref)
    # 任务信封只给子进程类执行体：它们靠位置参数/--print 收一整段人话，
    # 需要知道自己在哪、怎么交活、能不能追问。MCP 的 task 是**结构化入参**
    # （模板里的 {task} 会被直接替换成某段待处理内容），塞信封等于污染入参。
    prompt = build_task_prompt(task, cwd=str(cwd)) if envelope else task
    argv = adapter.build_command(prompt, str(cwd))
    # 续接旗标的位置由**适配器**决定（claude 的最后一项是 --output-format 而不是
    # prompt，runner 在这一层插不安全）——所以 ref 经 build_adapter 注入，
    # 由 ``build_command`` 调 ``resume_args`` 自己拼。
    env = None
    extra = adapter.build_env()
    if extra is not None:
        env = {**os.environ, **extra}
    try:
        # npm 装的 CLI 在 Windows 上只有 .cmd/.ps1 shim，CreateProcess 不能
        # 直接执行；这里解析成 node + 入口脚本，argv 仍然不经 shell。
        argv = resolve_command(argv)
    except LauncherError as exc:
        return CliResult(
            ok=False,
            output=failure_output(FailureKind.FAILED, str(exc)),
            failure=FailureKind.FAILED,
        )
    started = time.monotonic()
    last_output = started
    timed_out: str | None = None
    cancelled = False

    def _touch() -> None:
        nonlocal last_output
        last_output = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return CliResult(
            ok=False,
            output=failure_output(
                FailureKind.FAILED,
                f"命令 {argv[0]!r} 不存在（可能刚被卸载），请检查 cli_tools.yaml",
            ),
            failure=FailureKind.FAILED,
        )
    except OSError as exc:
        return CliResult(
            ok=False,
            output=failure_output(FailureKind.FAILED, f"进程拉起失败：{exc}"),
            failure=FailureKind.FAILED,
        )

    out_sink = _StreamSink(on_line=on_line, name="stdout", touch=_touch)
    err_sink = _StreamSink(on_line=on_line, name="stderr", touch=_touch)

    async def _watchdog() -> None:
        nonlocal timed_out, cancelled
        idle = idle_timeout if idle_timeout and idle_timeout > 0 else None
        while proc.returncode is None:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            if cancel is not None and cancel.is_set():
                cancelled = True
                proc.kill()
                return
            if idle is not None and (time.monotonic() - last_output) >= idle:
                timed_out = f"空闲 {int(idle)}s 无任何输出，已强制终止"
                proc.kill()
                return

    watchdog = asyncio.create_task(_watchdog())
    stdout = stderr = ""
    try:
        try:
            # 必须把 proc.wait() 和两路读流一起 await：只读到 EOF 时 asyncio 的
            # 子进程回调可能还没跑，proc.returncode 仍是 None，原来那句
            # `if code == 0` 就会把**成功的**委派判成失败（2026-10-25 实测）。
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(
                    _read_limited(proc.stdout, out_sink),
                    _read_limited(proc.stderr, err_sink),
                    proc.wait(),
                ),
                timeout=config.timeout,
            )
        except TimeoutError:
            # watchdog 可能已经因取消/静默先 kill 了进程，它的原因更准确，别覆盖。
            if timed_out is None:
                timed_out = f"超过超时上限 {int(config.timeout)}s 未结束，已强制终止"
            proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5)
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog

    # wait_for 超时会 cancel gather，但 sink 里已积累的字节还在——旧实现把
    # 它们全丢了，运营者只看到一句「超时」，白烧的额度连线索都没有。
    stdout = stdout or out_sink.text
    stderr = stderr or err_sink.text
    elapsed = time.monotonic() - started
    code = proc.returncode
    diagnostic_stderr = _diagnostic_stderr(stderr)
    # stderr 放前面：真正的原因（Traceback、限流正文）几乎都在 stderr，
    # 而 stdout 常常是一堆跑到一半的进度行。2026-11-13 前是 stdout 在前，
    # 再把整体头部截断——等于优先把诊断丢掉、把噪音留下。
    combined = f"{diagnostic_stderr}\n{stdout}".strip()

    # 失败也可能已经烧了 token（超时/被拦之前已经在推理），所以用量在两条路径都抽。
    # 必须放在超时分支之前——那个分支也要如实记账。
    tokens_in, tokens_out, cost_usd = adapter.parse_usage(stdout, stderr)
    session_ref = adapter.session_ref(stdout)

    if timed_out or cancelled:
        partial = (out_sink.text + "\n" + err_sink.text).strip()
        tail = partial[-_PARTIAL_OUTPUT_CHARS:] if partial else ""
        detail = timed_out or "任务被取消，已终止执行体进程"
        if tail:
            detail += f"\n\n--- 已产生的输出（末尾 {len(tail)} 字符）---\n{tail}"
        # 超时与用户主动取消是两回事：前者要劝模型缩小范围，后者绝不能自动重试。
        kind = FailureKind.CANCELLED if cancelled and not timed_out else FailureKind.TIMEOUT
        return CliResult(
            ok=False,
            output=failure_output(kind, detail),
            failure=kind,
            elapsed=elapsed,
            exit_code=code,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            session_ref=session_ref,
        )

    # 失败也可能已经烧了 token（超时/被拦之前已经在推理），所以用量在两条路径都抽。
    if code == 0:
        # 个别 CLI 用退出码 0 表达登录失败（CodeBuddy 2.176.0 实测），
        # 不拦就会被记成「成功」并把登录提示当成答案回给父模型。
        # stderr 要剥掉 INFO 日志：码道 --print-logs 会写「使用环境变量
        # CODEARTS_CLI_AK/SK授权CLI」，只按变量名匹配会把成功误判成未登录。
        fatal = adapter.fatal_output(stdout, diagnostic_stderr)
        if fatal is not None:
            return CliResult(
                ok=False,
                output=failure_output(fatal, f"退出码 {code}\n{combined}".strip()),
                failure=fatal,
                elapsed=elapsed,
                exit_code=code,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                session_ref=session_ref,
            )
        return CliResult(
            ok=True,
            output=adapter.parse_output(stdout, stderr) or "（执行成功，无文本输出）",
            elapsed=elapsed,
            exit_code=code,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            session_ref=session_ref,
        )
    kind = classify_failure(combined)
    detail = f"退出码 {code}\n{combined}" if combined else f"退出码 {code}，无输出"
    return CliResult(
        ok=False,
        output=failure_output(kind, detail),
        failure=kind,
        elapsed=elapsed,
        exit_code=code,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        session_ref=session_ref,
    )
