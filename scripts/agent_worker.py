"""事件中台的「接单程序」：在本机长轮询取活、调执行体、交结论。

为什么是长轮询而不是反向推送：
本机在 NAT 后面（锁屏不休眠也不等于有公网入口），中台在公网服务器上。
中台推不到本机，所以反过来 —— 本机挂在中台上等活，一次最长 50 秒，
没活就再挂上去。这跟「每台 Mac 跑一个接单程序」是同一套思路。

用法::

    uv run python scripts/agent_worker.py                        # 常驻
    uv run python scripts/agent_worker.py --once                 # 只接一单，调试用
    uv run python scripts/agent_worker.py --gateway http://127.0.0.1:8317 \\
        --token <ZKAI_ADMIN_TOKEN> --worker m5

安全边界：
* worker 只执行 ``cli_tools.yaml`` 里已注册的角色，事件里写什么角色都不会绕过注册表
* 工作目录解析后必须在 ``--workspace`` 之内，越界直接判失败，不执行
* 失败也照样交活（``ok=false``）—— 卡在 running 里比失败更糟，没人看得见
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings, load_app_config
from app.services.agent import taskspace, usage_ledger
from app.services.agent.cli.registry import load_cli_registry
from app.services.agent.cli.runner import run_delegation

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="事件中台接单程序：拉任务、跑执行体、交结论")
    parser.add_argument("--gateway", default="http://127.0.0.1:8317", help="中台地址")
    parser.add_argument(
        "--token",
        default=os.environ.get("ZKAI_ADMIN_TOKEN", ""),
        help="管理令牌（默认读环境变量 ZKAI_ADMIN_TOKEN）",
    )
    parser.add_argument(
        "--worker",
        default=socket.gethostname(),
        help="worker 实例名，出现在事件的 claimed_by 上",
    )
    parser.add_argument("--workspace", default=str(PROJECT_ROOT), help="允许执行体工作的根目录")
    parser.add_argument("--wait", type=float, default=50.0, help="单次长轮询等待秒数（上限 120）")
    parser.add_argument("--once", action="store_true", help="只接一单就退出（调试用）")
    return parser


async def run_once(
    client: httpx.AsyncClient,
    *,
    worker: str,
    wait: float,
    workspace: Path,
    registry,
    settings: Settings,
) -> bool:
    """取一单并跑完。返回 False 表示这次没接到活。"""
    response = await client.post(
        "/zkadmin/events/claim", json={"worker": worker, "wait": min(wait, 120.0)}
    )
    if response.status_code == 404:
        raise SystemExit("中台未启用：/zkadmin/events 返回 404（检查 ZKAI_EVENTS_ENABLED）")
    response.raise_for_status()
    event = response.json().get("event")
    if event is None:
        return False

    event_id = int(event["id"])
    role = str(event.get("role") or "")
    print(f"[{worker}] 接到事件 #{event_id}：{event.get('summary') or event.get('type')}", flush=True)

    tool = registry.tool_for_role(role) if role else None
    if tool is None:
        await _complete(
            client,
            event_id,
            ok=False,
            verdict="",
            error=f"执行体角色 {role!r} 不可用（检查 config/cli_tools.yaml 的 enabled 与角色定义）",
        )
        return True

    cwd = _resolve_cwd(event.get("cwd"), workspace)
    if cwd is None:
        await _complete(
            client,
            event_id,
            ok=False,
            verdict="",
            error=f"工作目录越界或不存在：{event.get('cwd')!r}",
        )
        return True

    task = str(event.get("task") or "").strip()
    if not task:
        await _complete(client, event_id, ok=False, verdict="", error="事件没有可执行的指令（task 为空）")
        return True

    # 事件派单也进任务空间：与直跑、模型派单共用同一套隔离与租约。
    space: taskspace.TaskSpace | None = None
    allow_bypass = False
    owner = f"event-{event_id}"
    if settings.agent_task_space and not str(event.get("cwd") or "").strip():
        space = taskspace.create(workspace, session_id=owner, goal=task)
        got, holder = taskspace.acquire(workspace, space.task_id, owner=owner)
        if not got:
            await _complete(
                client,
                event_id,
                ok=False,
                verdict="",
                error=f"任务空间正被 {holder} 使用，本次没有执行",
            )
            return True
        cwd = space.path
        allow_bypass = settings.agent_permission_mode == "bypass"
    try:
        result = await run_delegation(
            tool, task=task, cwd=cwd, allow_bypass=allow_bypass
        )
        # 事件派单也记账：否则「这个月烧了多少」会漏掉自动跑的那部分。
        usage_ledger.record_delegation(
            tool=tool.name,
            model=tool.model,
            role=str(event.get("role") or ""),
            session_id=owner,
            task_space=str(workspace),
            ok=result.ok,
            elapsed=result.elapsed,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            error="" if result.ok else result.output[:200],
        )
    finally:
        if space is not None:
            taskspace.release(workspace, space.task_id, owner=owner)
    print(
        f"[{worker}] 事件 #{event_id} 完成：{'成功' if result.ok else '失败'} "
        f"（{result.elapsed:.1f}s）",
        flush=True,
    )
    await _complete(
        client,
        event_id,
        ok=result.ok,
        verdict=result.output,
        error=None if result.ok else f"执行体 {tool.name} 返回失败（{result.elapsed:.1f}s）",
    )
    return True


async def _complete(
    client: httpx.AsyncClient,
    event_id: int,
    *,
    ok: bool,
    verdict: str,
    error: str | None,
) -> None:
    payload = {"ok": ok, "verdict": verdict[:100_000], "error": error}
    try:
        response = await client.post(f"/zkadmin/events/{event_id}/complete", json=payload)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # 交活失败意味着这条事件会超时退回重跑 —— 必须喊出来，否则会静默重复执行。
        print(f"交活失败（事件 #{event_id} 会超时重跑）：{exc}", flush=True)


def _resolve_cwd(raw: object, workspace: Path) -> Path | None:
    text = str(raw or ".").strip() or "."
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_dir():
        return None
    if resolved != workspace and workspace not in resolved.parents:
        return None
    return resolved


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    config = load_app_config(settings)
    registry = load_cli_registry(config.settings)
    if not registry.known_roles():
        print("没有可用的执行体：先按 config/cli_tools.example.yaml 配好 config/cli_tools.yaml", flush=True)
        return 2
    workspace = Path(args.workspace)
    # argparse 的 default 在 parse_args 时就求值了，那时 .env 还没被读进进程环境
    # （load_app_config 在下面才调用）——所以令牌必须**在这之后**再取一次。
    # 2026-11-03：event_watch.py 栽过同一个坑，这里一并修。
    token = args.token or os.environ.get("ZKAI_ADMIN_TOKEN", "")
    headers = {"X-Admin-Token": token} if token else {}
    if not token:
        print(
            "⚠ 没有管理令牌：中台会以 401 拒绝认领。"
            "在 .env 里配 ZKAI_ADMIN_TOKEN，或用 --token 传。",
            flush=True,
        )
    print(
        f"[{args.worker}] 开始接单：中台 {args.gateway}，工作区 {workspace}，"
        f"可用角色 {', '.join(registry.known_roles())}",
        flush=True,
    )
    async with httpx.AsyncClient(
        base_url=args.gateway.rstrip("/"), headers=headers, timeout=args.wait + 30
    ) as client:
        while True:
            try:
                got = await run_once(
                    client,
                    worker=args.worker,
                    wait=args.wait,
                    workspace=workspace,
                    registry=registry,
                    settings=config.settings,
                )
            except KeyboardInterrupt:
                raise
            except httpx.HTTPError as exc:
                print(f"连不上中台（{exc}），5 秒后重试", flush=True)
                await asyncio.sleep(5)
                continue
            if args.once:
                # 调试语义：只尝试一次就退出，不管有没有接到活 ——
                # 否则队列空的时候 --once 会一直挂着，看起来像卡死。
                return 0
            if not got:
                await asyncio.sleep(1)


def main() -> int:
    args = build_parser().parse_args()
    # 在这里（同步上下文）解析路径：async 函数里做 resolve 会被 ASYNC240 判成阻塞调用，
    # 而且这一步本来就该在进事件循环之前完成。
    args.workspace = str(Path(args.workspace).resolve())
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("已停止接单", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
