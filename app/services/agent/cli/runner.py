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
import time
from pathlib import Path

from app.services.agent.cli.base import (
    CliResult,
    FailureKind,
    build_adapter,
    classify_failure,
    failure_output,
)
from app.services.agent.cli.launcher import LauncherError, resolve_command
from app.services.agent.cli.registry import CliToolConfig

#: 单路输出最多读多少字节（超出即丢弃剩余，进程照常等死）
_MAX_STREAM_BYTES = 1_000_000


async def _read_limited(stream: asyncio.StreamReader | None) -> str:
    """限量读一路输出；进程写不停也只收前 _MAX_STREAM_BYTES。"""
    if stream is None:
        return ""
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = await stream.read(65536)
        except Exception:
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total >= _MAX_STREAM_BYTES:
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


async def run_delegation(
    config: CliToolConfig,
    *,
    task: str,
    cwd: Path,
) -> CliResult:
    """执行一次委派：拉起子进程、等它结束、分类结果。"""
    adapter = build_adapter(config)
    argv = adapter.build_command(task, str(cwd))
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
    try:
        # 必须把 proc.wait() 和两路读流一起 await：只读到 EOF 时 asyncio 的
        # 子进程回调可能还没跑，proc.returncode 仍是 None，原来那句
        # `if code == 0` 就会把**成功的**委派判成失败（2026-10-25 实测：
        # gemini 已经吐出完整 JSON，结果被标成【执行失败】，exit=None）。
        stdout, stderr, _ = await asyncio.wait_for(
            asyncio.gather(
                _read_limited(proc.stdout),
                _read_limited(proc.stderr),
                proc.wait(),
            ),
            timeout=config.timeout,
        )
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)
        elapsed = time.monotonic() - started
        return CliResult(
            ok=False,
            output=failure_output(
                FailureKind.FAILED,
                f"超过超时上限 {int(config.timeout)}s 未结束，已强制终止",
            ),
            failure=FailureKind.FAILED,
            elapsed=elapsed,
        )
    elapsed = time.monotonic() - started
    code = proc.returncode
    combined = f"{stdout}\n{stderr}"
    if code == 0:
        # 个别 CLI 用退出码 0 表达登录失败（CodeBuddy 2.176.0 实测），
        # 不拦就会被记成「成功」并把登录提示当成答案回给父模型。
        fatal = adapter.fatal_output(stdout, stderr)
        if fatal is not None:
            return CliResult(
                ok=False,
                output=failure_output(fatal, combined.strip()),
                failure=fatal,
                elapsed=elapsed,
                exit_code=code,
            )
        return CliResult(
            ok=True,
            output=adapter.parse_output(stdout, stderr) or "（执行成功，无文本输出）",
            elapsed=elapsed,
            exit_code=code,
        )
    kind = classify_failure(combined)
    return CliResult(
        ok=False,
        output=failure_output(kind, combined.strip() or f"退出码 {code}，无输出"),
        failure=kind,
        elapsed=elapsed,
        exit_code=code,
    )
