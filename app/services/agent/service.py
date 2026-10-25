"""ZK-Agent service: the task loop that turns a user prompt into tool work.

Per session the loop runs inside an :class:`asyncio.Task`:

1. rebuild the transcript from ``agent_messages`` (the rows *are* the LLM
   history), prepend the system prompt;
2. call the gateway's own ``RequestService.chat`` non-streaming with the tool
   schemas (routing / key pool / usage stats all reused, ``session_id`` pins
   the credential);
3. execute requested tools (dangerous ones only after user approval), append
   tool messages, repeat until the model answers without tool calls;
4. cap steps at ``ZKAI_AGENT_MAX_STEPS`` and compact the context (ephemeral
   summary) when the estimated prompt outgrows the token limit.

Events are broadcast to SSE subscribers; the transcript itself is persisted
row by row, so refreshing the page or restarting the gateway loses nothing
except the loop of a session that was mid-flight (marked ``interrupted``).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.core.config import PROJECT_ROOT, AppConfig, Settings
from app.core.errors import ZKAIError
from app.core.logging import get_logger
from app.database.repository import AgentRepository
from app.models.request import ChatCompletionRequest, ChatMessage, ToolCall
from app.services.agent.cli import CliRegistry, load_cli_registry
from app.services.agent.cli import delegate as cli_delegate
from app.services.agent.cli.registry import EFFORT_LEVELS
from app.services.agent.cli.runner import run_delegation
from app.services.agent.confirm import SessionApprovals
from app.services.agent.prompts import build_system_prompt
from app.services.agent.roles import describe_roles, resolve_alias
from app.services.agent.tools import (
    DANGEROUS_TOOLS,
    ROOT_TOOL_SCHEMAS,
    SAFE_TOOLS,
    SUPERVISOR_TOOLS,
    TOOL_SCHEMAS,
    ToolBox,
    ToolError,
    ToolOutcome,
)
from app.services.request_service import RequestService

logger = get_logger("services.agent")

_ACTIVE_STATUSES = frozenset({"running", "waiting_approval"})
#: followup 允许从哪些状态继续
_RESUMABLE_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})
_COMPACT_TAIL = 10  # 压缩时保留最近的消息条数
#: 回填给父模型的子任务结论上限（再长就是拿子会话的原文挤父的上下文）。
_MAX_CHILD_SUMMARY = 4_000
#: 等子任务时的轮询间隔：只用于「顺带看一眼是否卡在等审批」，完成信令本身
#: 是事件驱动（run.done），所以这个间隔不影响响应速度。
_CHILD_POLL = 0.25


@dataclass(slots=True)
class _Running:
    """Live state of one executing session."""

    session_id: str
    workspace: Path
    model: str
    approvals: SessionApprovals = field(default_factory=SessionApprovals)
    subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    cancelled: bool = False
    #: 本会话启动时的外部执行体花名册快照；空册 = delegate_cli 不出现
    cli_registry: CliRegistry = field(default_factory=CliRegistry)
    # ---- 子任务派发 ----------------------------------------------------- #
    #: 父服务的引用；非 None = 这是子会话，不允许再派发（工具箱里也没有那个 schema）。
    parent: AgentService | None = None
    #: 父会话 id，仅用于事件归属与展示，不作查询键。
    parent_session: str | None = None
    #: 根=1，子=2。
    depth: int = 1
    #: 本会话在跑的子任务数。
    live_kids: int = 0
    #: 本批 tool_calls 里还没起跑的 spawn 数（**不含正在执行的那个**）。
    reserved: int = 0
    #: 本批**已批准**的 spawn 数（同批全程累加，批末不清）。
    #: _execute_tool 是串行 await 的，同批 spawn 天然一个个跑完，第一个结束时
    #: live_kids 已归 0——光看实时并发拦不住同批第二个，用这个补上。
    spawned_batch: int = 0
    #: 父子信令：loop 结束时由 ``_guarded_loop`` 的 finally put 一次。
    #: 与 subscribers（SSE 订阅者列表）刻意分开——那个是列表可多个，这个是单值。
    done: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)


class AgentService:
    """Owns every running agent session; API layer calls into this."""

    def __init__(
        self,
        *,
        settings: Settings,
        config: AppConfig,
        request_service: RequestService,
        repository: AgentRepository,
    ) -> None:
        self.settings = settings
        self.config = config
        self.requests = request_service
        self.repo = repository
        self._running: dict[str, _Running] = {}
        self._sem = asyncio.Semaphore(max(1, settings.agent_max_concurrent))
        # 子任务走**独立**信号量：根循环持 ``_sem`` 等子，子若也来抢 ``_sem``
        # 就是「持锁等锁」——max_concurrent 被根会话占满时全局死锁。两个 sem 的
        # 获取方完全不相交（根不碰 _child_sem，子不碰 _sem），因此无环。
        self._child_sem = asyncio.Semaphore(max(1, settings.agent_max_children_total))

    # ------------------------------------------------------------------ #
    # Queries (with lazy zombie cleanup)
    # ------------------------------------------------------------------ #
    async def mark_stale_interrupted(self) -> int:
        """Flag sessions stuck in an active state without a live loop (startup)."""
        stale = [sid for sid in await self.repo.running_session_ids()
                 if sid not in self._running]
        for session_id in stale:
            await self.repo.update_status(session_id, "interrupted",
                                          error="网关重启或进程退出，任务中断")
        return len(stale)

    async def list_sessions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        sessions = await self.repo.list_sessions(limit=limit)
        for row in sessions:
            if row["status"] in _ACTIVE_STATUSES and row["id"] not in self._running:
                row["status"] = "interrupted"
                await self.repo.update_status(row["id"], "interrupted",
                                              error="网关重启或进程退出，任务中断")
        return sessions

    async def session_detail(self, session_id: str) -> dict[str, Any] | None:
        row = await self.repo.get_session(session_id)
        if row is None:
            return None
        if row["status"] in _ACTIVE_STATUSES and session_id not in self._running:
            row["status"] = "interrupted"
            await self.repo.update_status(session_id, "interrupted",
                                          error="网关重启或进程退出，任务中断")
        return {"session": row, "messages": await self.repo.messages(session_id)}

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #
    def validate_model(self, model: str) -> bool:
        return model in self.config.aliases or model in self.config.models

    async def start(
        self,
        *,
        task: str,
        model: str | None = None,
        workspace: str | None = None,
        parent: AgentService | None = None,
        parent_session: str | None = None,
        depth: int = 1,
    ) -> dict[str, Any]:
        resolved_model = model or self.settings.agent_default_model
        ws = self._resolve_workspace(workspace)
        session_id = uuid.uuid4().hex
        title = task.strip().splitlines()[0][:80] if task.strip() else "(空任务)"
        await self.repo.create_session(
            session_id=session_id, title=title, model=resolved_model, workspace=str(ws)
        )
        await self.repo.add_message(session_id, role="user", kind="task", content=task)
        run = _Running(
            session_id=session_id,
            workspace=ws,
            model=resolved_model,
            parent=parent,
            parent_session=parent_session,
            depth=depth,
            cli_registry=load_cli_registry(self.settings),
        )
        self._running[session_id] = run
        self._spawn(run, resume=False)
        return {"session": await self.repo.get_session(session_id), "messages": []}

    async def followup(self, session_id: str, content: str) -> dict[str, Any] | None:
        row = await self.repo.get_session(session_id)
        if row is None:
            return None
        if row["status"] in _ACTIVE_STATUSES:
            raise ZKAIError(
                "会话正在执行中，先等它结束或取消",
                http_status=409,
            )
        if row["status"] not in _RESUMABLE_STATUSES:
            raise ZKAIError(f"会话状态 {row['status']} 不能继续", http_status=409)
        run = _Running(session_id=session_id, workspace=Path(row["workspace"]),
                       model=row["model"], cli_registry=load_cli_registry(self.settings))
        self._running[session_id] = run
        await self.repo.add_message(session_id, role="user", kind="task", content=content)
        await self.repo.update_status(session_id, "running", error=None)
        self._spawn(run, resume=True)
        return await self.session_detail(session_id)

    def _resolve_delegation(
        self,
        role: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        context_window: int | None = None,
    ) -> tuple[CliRegistry, Any]:
        """角色 + 本次覆盖 -> (花名册, 处理过的执行体配置)。"""
        registry = load_cli_registry(self.settings)
        config = registry.tool_for_role(role)
        if config is None:
            raise ZKAIError(
                f"执行体角色 {role!r} 不可用（检查 config/cli_tools.yaml）",
                http_status=400,
            )
        overrides: dict[str, Any] = {}
        if model:
            overrides["model"] = model.strip()
        if effort:
            level = effort.strip().lower()
            if level not in EFFORT_LEVELS:
                raise ZKAIError(
                    f"思考档位 {effort!r} 不合法（可选 {sorted(EFFORT_LEVELS)}）",
                    http_status=400,
                )
            overrides["effort"] = level
        if context_window:
            overrides["context_window"] = max(0, int(context_window))
        if overrides:
            config = replace(config, **overrides)
        return registry, config

    async def start_delegation(
        self,
        *,
        role: str,
        task: str,
        model: str | None = None,
        effort: str | None = None,
        context_window: int | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """直接把任务交给外部执行体（不经父模型）。

        控制台里 ``@`` / ``/`` 选执行体走这条：不起任务循环、不花父模型 token，
        结果作为一条 assistant 消息落库，所以刷新页面还在、左侧列表里能回看。
        """
        registry, config = self._resolve_delegation(
            role, model=model, effort=effort, context_window=context_window
        )
        workspace_path = self._resolve_workspace(workspace)
        session_id = uuid.uuid4().hex
        first_line = task.strip().splitlines()[0][:60] if task.strip() else "(空任务)"
        await self.repo.create_session(
            session_id=session_id,
            title=f"[{role}] {first_line}",
            model=f"cli:{config.name}",
            workspace=str(workspace_path),
        )
        await self.repo.add_message(
            session_id,
            role="user",
            kind="task",
            content=task,
            data={"executor": role, "model": config.model},
        )
        run = _Running(
            session_id=session_id,
            workspace=workspace_path,
            model=f"cli:{config.name}",
            cli_registry=registry,
        )
        self._running[session_id] = run
        job = asyncio.create_task(self._delegation(run, config, task))
        _BACKGROUND_TASKS.add(job)
        job.add_done_callback(_BACKGROUND_TASKS.discard)
        return {"session": await self.repo.get_session(session_id), "messages": []}

    async def followup_delegation(
        self,
        session_id: str,
        *,
        role: str,
        task: str,
        model: str | None = None,
        effort: str | None = None,
        context_window: int | None = None,
    ) -> dict[str, Any] | None:
        """在**同一个会话**里再派一次执行体（可以换另一条 CLI）。

        同一轮对话里连续指派不同执行体是正常用法：每条任务各自带
        ``data.executor``，transcript 顺序保留。
        """
        row = await self.repo.get_session(session_id)
        if row is None:
            return None
        if row["status"] in _ACTIVE_STATUSES:
            raise ZKAIError("会话正在执行中，先等它结束或取消", http_status=409)
        if row["status"] not in _RESUMABLE_STATUSES:
            raise ZKAIError(f"会话状态 {row['status']} 不能继续", http_status=409)
        registry, config = self._resolve_delegation(
            role, model=model, effort=effort, context_window=context_window
        )
        run = _Running(
            session_id=session_id,
            workspace=Path(row["workspace"]),
            model=row["model"],
            cli_registry=registry,
        )
        self._running[session_id] = run
        await self.repo.add_message(
            session_id,
            role="user",
            kind="task",
            content=task,
            data={"executor": role, "model": config.model},
        )
        await self.repo.update_status(session_id, "running", error=None)
        job = asyncio.create_task(self._delegation(run, config, task))
        _BACKGROUND_TASKS.add(job)
        job.add_done_callback(_BACKGROUND_TASKS.discard)
        return await self.session_detail(session_id)

    async def _delegation(self, run: _Running, config: Any, task: str) -> None:
        """执行体直跑：一次 run_delegation + 两条消息 + SSE 事件。"""
        session_id = run.session_id
        outcome: dict[str, Any] = {"session_id": session_id, "status": "failed", "error": None}
        status = "failed"
        error: str | None = None
        info = {
            "role": config.name,
            "tool": config.name,
            "model": config.model,
            "cost_tier": config.cost_tier,
            "effort": config.effort,
            "context_window": config.context_window,
        }
        try:
            await self.repo.update_status(session_id, "running")
            self._emit(run, {
                "type": "session_started",
                "model": config.model,
                "workspace": str(run.workspace),
                "resumed": False,
            })
            self._emit(run, {"type": "step_start", "step": 1, "model": config.model})
            result = await run_delegation(config, task=task, cwd=run.workspace)
            status = "done" if result.ok else "failed"
            error = None if result.ok else result.output[:300]
            if run.cancelled:
                # 子进程已经跑完（run_delegation 不支持中途 kill），但状态要如实
                # 记成「已取消」，否则界面会覆盖掉用户刚点的停止。
                status, error = "cancelled", "任务被取消"
            payload = {**info, "elapsed": round(result.elapsed, 1), "ok": result.ok}
            seq = await self.repo.add_message(
                session_id,
                role="assistant",
                kind="message",
                content=result.output,
                data={"executor": payload},
            )
            self._emit(run, {
                "type": "assistant",
                "seq": seq,
                "content": result.output,
                "thinking": None,
                "tool_calls": [],
                "routing": None,
                "step": 1,
                "executor": payload,
            })
            await self.repo.update_status(session_id, status, steps=1, error=error)
            self._emit(run, {
                "type": "session_done",
                "status": status,
                **({"error": error} if error else {}),
            })
            outcome.update(status=status, error=error)
        except Exception as exc:  # 执行体崩溃不能把会话永远挂在 running
            logger.exception("delegation session %s crashed", session_id)
            message = repr(exc)[:300]
            try:
                await self.repo.update_status(session_id, "failed", error=message)
            except Exception:
                logger.exception("agent session %s 状态落库失败", session_id)
            self._emit(run, {"type": "session_done", "status": "failed", "error": message[:200]})
            outcome.update(status="failed", error=message)
        finally:
            run.done.put_nowait(outcome)
            self._running.pop(session_id, None)

    async def start_delegations(
        self,
        *,
        items: list[dict[str, Any]],
        session_id: str | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        """按顺序跑多段执行体任务（一条消息里点名多个 CLI）。

        每段各自带 ``data.executor``；一条消息里「先让 A 查、再让 B 审」是常态，
        所以这里一次落库全部任务，再串行跑——串行是有意的：免费档额度本来就紧，
        并发只会互相挤成 429。
        """
        if not items:
            raise ZKAIError("至少要有一段任务", http_status=400)
        if len(items) > 10:
            raise ZKAIError("一条消息最多指派 10 段任务", http_status=400)

        resolved: list[tuple[Any, str, str]] = []
        registry = CliRegistry()
        for item in items:
            role = str(item.get("role") or "").strip()
            task = str(item.get("task") or "").strip()
            if not task:
                raise ZKAIError(f"执行体 {role!r} 那段没有任务内容", http_status=400)
            registry, config = self._resolve_delegation(
                role,
                model=item.get("model"),
                effort=item.get("effort"),
                context_window=item.get("context_window"),
            )
            resolved.append((config, task, role))

        if session_id:
            row = await self.repo.get_session(session_id)
            if row is None:
                raise ZKAIError("会话不存在", http_status=404)
            if row["status"] in _ACTIVE_STATUSES:
                raise ZKAIError("会话正在执行中，先等它结束或取消", http_status=409)
            if row["status"] not in _RESUMABLE_STATUSES:
                raise ZKAIError(f"会话状态 {row['status']} 不能继续", http_status=409)
            ws = Path(row["workspace"])
        else:
            ws = self._resolve_workspace(workspace)
            session_id = uuid.uuid4().hex
            first = resolved[0][1].splitlines()[0][:60]
            await self.repo.create_session(
                session_id=session_id,
                title=f"[{len(resolved)}段] {first}",
                model=f"cli:{resolved[0][0].name}",
                workspace=str(ws),
            )

        for config, task, role in resolved:
            await self.repo.add_message(
                session_id,
                role="user",
                kind="task",
                content=task,
                data={"executor": role, "model": config.model},
            )
        run = _Running(
            session_id=session_id,
            workspace=ws,
            model=f"cli:{resolved[0][0].name}",
            cli_registry=registry,
        )
        self._running[session_id] = run
        await self.repo.update_status(session_id, "running", error=None)
        job = asyncio.create_task(self._delegation_batch(run, resolved))
        _BACKGROUND_TASKS.add(job)
        job.add_done_callback(_BACKGROUND_TASKS.discard)
        return await self.session_detail(session_id) or {"session": None, "messages": []}

    async def _delegation_batch(
        self, run: _Running, resolved: list[tuple[Any, str, str]]
    ) -> None:
        """串行跑多段：每段一条 assistant 消息，失败即停（后面的段不再烧额度）。"""
        session_id = run.session_id
        outcome: dict[str, Any] = {"session_id": session_id, "status": "failed", "error": None}
        status, error = "done", None
        try:
            await self.repo.update_status(session_id, "running")
            self._emit(run, {
                "type": "session_started",
                "model": run.model,
                "workspace": str(run.workspace),
                "resumed": False,
            })
            for index, (config, task, role) in enumerate(resolved, start=1):
                if run.cancelled:
                    status, error = "cancelled", "任务被取消"
                    break
                self._emit(run, {"type": "step_start", "step": index, "model": config.model})
                result = await run_delegation(config, task=task, cwd=run.workspace)
                payload = {
                    "role": role,
                    "tool": config.name,
                    "model": config.model,
                    "cost_tier": config.cost_tier,
                    "effort": config.effort,
                    "context_window": config.context_window,
                    "elapsed": round(result.elapsed, 1),
                    "ok": result.ok,
                    "index": index,
                    "total": len(resolved),
                }
                seq = await self.repo.add_message(
                    session_id,
                    role="assistant",
                    kind="message",
                    content=result.output,
                    data={"executor": payload},
                )
                self._emit(run, {
                    "type": "assistant",
                    "seq": seq,
                    "content": result.output,
                    "thinking": None,
                    "tool_calls": [],
                    "routing": None,
                    "step": index,
                    "executor": payload,
                })
                if not result.ok:
                    status, error = "failed", result.output[:300]
                    break
            await self.repo.update_status(session_id, status, steps=len(resolved), error=error)
            self._emit(run, {
                "type": "session_done",
                "status": status,
                **({"error": error} if error else {}),
            })
            outcome.update(status=status, error=error)
        except Exception as exc:
            logger.exception("delegation batch %s crashed", session_id)
            message = repr(exc)[:300]
            try:
                await self.repo.update_status(session_id, "failed", error=message)
            except Exception:
                logger.exception("agent session %s 状态落库失败", session_id)
            self._emit(run, {"type": "session_done", "status": "failed", "error": message[:200]})
            outcome.update(status="failed", error=message)
        finally:
            run.done.put_nowait(outcome)
            self._running.pop(session_id, None)

    async def cancel(self, session_id: str) -> bool:
        row = await self.repo.get_session(session_id)
        if row is None:
            return False
        run = self._running.get(session_id)
        if run is None:
            if row["status"] in _ACTIVE_STATUSES:
                await self.repo.update_status(session_id, "cancelled")
            return True
        run.cancelled = True
        run.approvals.deny_all()  # 唤醒等审批的循环，让它走取消分支
        return True

    async def delete(self, session_id: str) -> bool:
        if session_id in self._running:
            raise ZKAIError("会话正在执行，先取消再删除", http_status=409)
        return await self.repo.delete_session(session_id)

    async def decide(
        self, session_id: str, approval_id: str, *, approved: bool, remember: bool
    ) -> dict[str, Any] | None:
        run = self._running.get(session_id)
        if run is None:
            return None
        approval = run.approvals.resolve(approval_id, approved=approved, remember=remember)
        if approval is None:
            return None
        decision = "批准" if approved else "拒绝"
        await self.repo.add_message(
            session_id,
            role="assistant",
            kind="approval",
            content=f"{decision} {approval.tool}"
                    + ("（本会话内不再询问）" if approval.remember else ""),
            data={"approval_id": approval_id, "tool": approval.tool,
                  "decision": approved, "remember": approval.remember},
        )
        self._emit(run, {
            "type": "approval_resolved",
            "approval_id": approval_id,
            "tool": approval.tool,
            "approved": approved,
        })
        return {"approval_id": approval_id, "approved": approved}

    # ------------------------------------------------------------------ #
    # SSE plumbing
    # ------------------------------------------------------------------ #
    async def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        run = self._running.get(session_id)
        if run is not None:
            run.subscribers.append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        run = self._running.get(session_id)
        if run is not None and queue in run.subscribers:
            run.subscribers.remove(queue)

    def _emit(self, run: _Running, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        for queue in list(run.subscribers):
            queue.put_nowait(event)
        # 子会话的事件也往父会话的订阅者透传一份，父页面才有「子任务在干什么」的
        # 可见性。父已结束时它不在 _running 里，透传静默失效——这是可接受的空
        # 分支，不是 bug（那时候父的 SSE 流本来就关了）。
        #
        # 必须 copy：_emit 会原地 mutate 传进来的 dict（上面的 setdefault），
        # 父子共用同一个 dict 会让子的事件对象被父的字段污染。
        if run.parent_session is not None:
            parent = self._running.get(run.parent_session)
            if parent is None:
                return
            forwarded: dict[str, Any] = {
                **event,
                "type": "child_event",
                "child_session": run.session_id,
            }
            for queue in list(parent.subscribers):
                queue.put_nowait(forwarded)

    # ------------------------------------------------------------------ #
    # The loop
    # ------------------------------------------------------------------ #
    def _spawn(self, run: _Running, *, resume: bool) -> None:
        task = asyncio.create_task(self._guarded_loop(run, resume=resume))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    async def _guarded_loop(self, run: _Running, *, resume: bool) -> None:
        session_id = run.session_id
        # 完成信令先攒在局部变量里：下面 except 分支的 update_status 自己可能抛
        # （DB 故障），若把 put 写在它后面，父会话就永远等不到这一声。
        outcome: dict[str, Any] = {"session_id": session_id, "status": "failed",
                                   "error": None}
        try:
            gate = self._child_sem if run.parent is not None else self._sem
            async with gate:
                outcome.update(await self._loop(run, resume=resume))
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            await self.repo.update_status(session_id, "cancelled", error="任务被取消")
            self._emit(run, {"type": "session_done", "status": "cancelled"})
            outcome["status"] = "cancelled"
            outcome["error"] = "任务被取消"
        except Exception as exc:  # the loop must never die silently
            logger.exception("agent session %s crashed", session_id)
            message = repr(exc)[:500]
            try:
                await self.repo.update_status(session_id, "failed", error=message)
            except Exception:  # DB 自己坏了也不能吞掉信令
                logger.exception("agent session %s 状态落库失败", session_id)
            self._emit(run, {"type": "session_done", "status": "failed",
                             "error": message[:200]})
            outcome["status"] = "failed"
            outcome["error"] = message
        finally:
            # 必须在 self._running.pop 之前：父被唤醒后要能读到这个 run 的
            # approvals.pending 判断它是否卡在等审批。
            run.done.put_nowait(outcome)
            self._running.pop(session_id, None)

    async def _loop(self, run: _Running, *, resume: bool) -> dict[str, Any]:
        """跑完一个会话的任务循环，返回 ``{"status": ..., "error": ...}``。

        返回值是给父会话看的完成信令，不只是给日志的。所以这里**不能**把失败
        也 `return` 成空 dict——那会被 `_guarded_loop` 当成正常 done。
        """
        session_id = run.session_id
        s = self.settings
        await self.repo.update_status(session_id, "running")
        self._emit(run, {"type": "session_started", "model": run.model,
                         "workspace": str(run.workspace), "resumed": resume})

        system = ChatMessage(
            role="system",
            content=build_system_prompt(
                str(run.workspace),
                max_steps=s.agent_max_steps,
                settings=s,
                is_child=run.parent is not None,
            ),
        )
        messages: list[ChatMessage] = [system, *self._replay(await self.repo.messages(session_id))]
        toolbox = ToolBox(run.workspace, command_timeout=s.agent_command_timeout)
        # 子会话拿不到 spawn_subagent——递归派发由「根本不发这个 schema」兜底，
        # 而不是靠 prompt 里写「你不许用」。
        schemas = ROOT_TOOL_SCHEMAS if run.parent is None else TOOL_SCHEMAS
        # delegate_cli 只在根会话出现（子会话不许派外部执行体，防链式烧额度），
        # 且注册表为空时连 schema 都不发——模型根本不知道有这个东西。
        if run.parent is None and run.cli_registry.known_roles():
            schemas = [*schemas, cli_delegate.build_schema(run.cli_registry)]
        steps = 0
        in_tokens = 0
        out_tokens = 0

        while True:
            if run.cancelled:
                await self.repo.update_status(session_id, "cancelled", steps=steps,
                                              input_tokens=in_tokens, output_tokens=out_tokens)
                self._emit(run, {"type": "session_done", "status": "cancelled"})
                return {"status": "cancelled", "error": "任务被取消"}
            if steps >= s.agent_max_steps:
                note = f"已达到单任务步数上限（{s.agent_max_steps} 步），任务停止。"
                await self.repo.add_message(session_id, role="assistant", kind="note",
                                            content=note)
                await self.repo.update_status(session_id, "done", steps=steps,
                                              input_tokens=in_tokens, output_tokens=out_tokens)
                self._emit(run, {"type": "note", "content": note})
                self._emit(run, {"type": "session_done", "status": "done",
                                 "reason": "max_steps"})
                return {"status": "done", "error": None}

            request = ChatCompletionRequest(
                model=run.model,
                messages=messages,
                tools=schemas,
                tool_choice="auto",
                stream=False,
                session_id=f"agent-{session_id}",
            )
            if request.estimated_input_tokens() > s.agent_context_token_limit:
                messages = await self._compact(run, messages)
                request = ChatCompletionRequest(
                    model=run.model,
                    messages=messages,
                    tools=schemas,
                    tool_choice="auto",
                    stream=False,
                    session_id=f"agent-{session_id}",
                )

            step = steps + 1
            self._emit(run, {"type": "step_start", "step": step, "model": run.model})
            started = time.monotonic()
            try:
                response = await self.requests.chat(
                    request, request_id=f"agent-{session_id}-{step}"
                )
            except ZKAIError as exc:
                await self.repo.update_status(session_id, "failed", steps=steps,
                                              input_tokens=in_tokens,
                                              output_tokens=out_tokens,
                                              error=exc.message[:500])
                self._emit(run, {"type": "session_done", "status": "failed",
                                 "error": exc.message[:200]})
                # 这里是父会话最需要看到的失败：上游全挂、路由不到模型等。
                # 必须如实回传，不能 return 空 dict 让父以为成功了。
                return {"status": "failed", "error": exc.message[:500]}
            latency = time.monotonic() - started
            steps += 1
            in_tokens += response.usage.prompt_tokens
            out_tokens += response.usage.completion_tokens
            message = response.choices[0].message if response.choices else ChatMessage(role="assistant")

            extra = message.model_extra or {}
            thinking = str(extra.get("reasoning_content") or extra.get("reasoning") or "") or None
            tool_calls = list(message.tool_calls or [])
            routing = None
            if response.zk_ai is not None:
                routing = {
                    "provider": response.zk_ai.provider,
                    "model": response.zk_ai.resolved_model,
                    "alias": response.zk_ai.alias,
                    "latency_ms": round(latency * 1000),
                }
            seq = await self.repo.add_message(
                session_id,
                role="assistant",
                kind="message",
                content=message.text() or None,
                data={
                    "tool_calls": [tc.model_dump(exclude_none=True) for tc in tool_calls],
                    **({"thinking": thinking} if thinking else {}),
                    **({"routing": routing} if routing else {}),
                },
            )
            await self.repo.update_status(session_id, "running", steps=steps,
                                          input_tokens=in_tokens, output_tokens=out_tokens)
            self._emit(run, {
                "type": "assistant",
                "seq": seq,
                "content": message.text() or "",
                "thinking": thinking,
                "tool_calls": [tc.model_dump(exclude_none=True) for tc in tool_calls],
                "routing": routing,
                "step": step,
            })

            if tool_calls:
                messages.append(message)
                # _execute_tool 是一个个 await 的，同批 spawn 天然**串行**：第一个
                # 跑完 live_kids 就归 0，第二个看到的实时并发永远是 1，拦不住。
                # 所以同批闸用「本批已批准的 spawn 数」+ live_kids：前者记住这一
                # 轮一口气派了几个，后者挡住跨轮次的连发。
                remaining = sum(
                    1 for c in tool_calls if c.function.name in SUPERVISOR_TOOLS
                )
                run.reserved += remaining
                try:
                    for call in tool_calls:
                        if call.function.name in SUPERVISOR_TOOLS:
                            run.reserved -= 1
                        tool_message = await self._execute_tool(run, toolbox, call)
                        messages.append(tool_message)
                finally:
                    # 中途异常时把剩下的待跑名额一次还干净，并清掉本批计数。
                    run.reserved = 0
                    run.spawned_batch = 0
                continue

            await self.repo.update_status(session_id, "done", steps=steps,
                                          input_tokens=in_tokens, output_tokens=out_tokens)
            self._emit(run, {"type": "session_done", "status": "done"})
            return {"status": "done", "error": None}

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #
    async def _execute_tool(
        self, run: _Running, toolbox: ToolBox, call: ToolCall
    ) -> ChatMessage:
        session_id = run.session_id
        name = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = None
        if not isinstance(args, dict):
            outcome = ToolOutcome(ok=False, output="工具参数不是合法 JSON 对象")
        else:
            outcome = await self._dispatch(run, toolbox, name, args)

        seq = await self.repo.add_message(
            session_id,
            role="tool",
            kind="tool_result",
            content=outcome.output,
            data={
                "tool_call_id": call.id,
                "tool": name,
                "ok": outcome.ok,
                **({"display": outcome.display} if outcome.display else {}),
            },
        )
        self._emit(run, {
            "type": "tool_result",
            "seq": seq,
            "tool_call_id": call.id,
            "tool": name,
            "ok": outcome.ok,
            "output": outcome.output[:2_000],
            "display": outcome.display,
        })
        return ChatMessage(role="tool", content=outcome.output, tool_call_id=call.id)

    async def _dispatch(
        self, run: _Running, toolbox: ToolBox, name: str, args: dict[str, Any]
    ) -> ToolOutcome:
        session_id = run.session_id
        try:
            if name in SUPERVISOR_TOOLS:
                return await self._spawn_child(run, args)
            if name == cli_delegate.TOOL_NAME:
                return await self._delegate_cli(run, toolbox, args)
            if name in SAFE_TOOLS:
                return await toolbox.perform(name, args)
            if name not in DANGEROUS_TOOLS:
                raise ToolError(f"未知工具 {name!r}，可用：{sorted(SAFE_TOOLS | DANGEROUS_TOOLS)}")
            if not run.approvals.skipped(name):
                prepared = toolbox.prepare(name, args)  # 黑名单在这里直接 ToolError
                preview = str((prepared.display or {}).get("diff")
                              or (prepared.display or {}).get("command") or "")
                approval = run.approvals.register(name, args, preview, prepared.display)
                await self.repo.update_status(session_id, "waiting_approval")
                seq = await self.repo.add_message(
                    session_id,
                    role="assistant",
                    kind="approval",
                    content=preview or f"请求执行 {name}",
                    data={"approval_id": approval.id, "tool": name, "args": args},
                )
                self._emit(run, {
                    "type": "approval_request",
                    "seq": seq,
                    "approval_id": approval.id,
                    "tool": name,
                    "preview": preview,
                    "display": prepared.display,
                    "args": args,
                })
                await approval.event.wait()
                await self.repo.update_status(session_id, "running")
                if not approval.approved:
                    return ToolOutcome(
                        ok=False,
                        output="用户拒绝了此操作。请调整方案后重试，或直接给出结论。",
                    )
            return await toolbox.perform(name, args)
        except ToolError as exc:
            return ToolOutcome(ok=False, output=f"工具执行失败：{exc}")
    # ------------------------------------------------------------------ #
    # 外部执行体委派（delegate_cli）
    # ------------------------------------------------------------------ #
    async def _delegate_cli(
        self, run: _Running, toolbox: ToolBox, args: dict[str, Any]
    ) -> ToolOutcome:
        """delegate_cli 的实现：走与危险工具相同的两阶段审批，批准后拉起子进程。"""
        self._require_root(run)
        if not run.cli_registry.known_roles():
            raise ToolError(
                "外部执行体功能未启用（config/cli_tools.yaml 未配置或无可用执行体）。"
            )
        session_id = run.session_id
        if not run.approvals.skipped(cli_delegate.TOOL_NAME):
            prepared = cli_delegate.prepare(run.cli_registry, toolbox, args)
            preview = str((prepared.display or {}).get("command") or "")
            approval = run.approvals.register(
                cli_delegate.TOOL_NAME, args, preview, prepared.display
            )
            await self.repo.update_status(session_id, "waiting_approval")
            seq = await self.repo.add_message(
                session_id,
                role="assistant",
                kind="approval",
                content=preview or "请求执行 delegate_cli",
                data={"approval_id": approval.id, "tool": cli_delegate.TOOL_NAME, "args": args},
            )
            self._emit(run, {
                "type": "approval_request",
                "seq": seq,
                "approval_id": approval.id,
                "tool": cli_delegate.TOOL_NAME,
                "preview": preview,
                "display": prepared.display,
                "args": args,
            })
            await approval.event.wait()
            await self.repo.update_status(session_id, "running")
            if not approval.approved:
                return ToolOutcome(
                    ok=False,
                    output="用户拒绝了此操作。请调整方案后重试，或直接给出结论。",
                )
        return await cli_delegate.perform(run.cli_registry, toolbox, args)

    # ------------------------------------------------------------------ #
    # 子任务派发
    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_root(run: _Running) -> None:
        """子会话不许再派发。工具箱里没这个 schema，这里是第二道闸。"""
        if run.parent is not None:
            raise ToolError(
                "你是子任务，不能派发下一级；请自己完成任务并把结论写清楚。"
            )

    def _child_gate(self, run: _Running) -> ToolOutcome | None:
        """深度 / 每父并发闸；返回非 None 表示拒绝（不用抛异常）。"""
        s = self.settings
        if run.depth >= s.agent_max_depth:
            return ToolOutcome(
                ok=False,
                output=f"派发深度已达上限（depth={run.depth}，上限 {s.agent_max_depth}），"
                       "请自己完成或直接给出结论。",
            )
        # 三个口径取最大：实时在跑、本批已批准、本批还没起跑。同批并派时
        # 实时在跑会被串行执行清零，必须靠「本批已批准」补上。
        in_use = max(run.live_kids, run.spawned_batch, run.reserved)
        if in_use >= s.agent_max_live_children:
            return ToolOutcome(
                ok=False,
                output=f"你已经有 {in_use} 个子任务在跑（上限 "
                       f"{s.agent_max_live_children}）。等它们回来再派，或自己做完。",
            )
        return None

    async def _spawn_child(self, run: _Running, args: dict[str, Any]) -> ToolOutcome:
        """spawn_subagent 的实现：起一个独立子会话，等它跑完，把结果回填。"""
        self._require_root(run)
        role = str(args.get("role") or "").strip()
        task = str(args.get("task") or "").strip()
        if not task:
            raise ToolError("task 不能为空：子会话看不到你的对话历史，任务必须自带上下文。")

        alias = resolve_alias(role, self.settings)
        if alias is None:
            raise ToolError(
                f"未知角色 {role!r}。可用角色：{describe_roles(self.settings)}。"
                "请从清单里挑一个，不要自己编。"
            )
        if not self.validate_model(alias):
            raise ToolError(
                f"角色 {role!r} 指向 {alias!r}，但网关里没有这个模型/别名。"
                "换个角色，或让管理员在控制台补上它。"
            )

        blocked = self._child_gate(run)
        if blocked is not None:
            return blocked
        # gate 通过才算「本批已批准一个」。放在这里而不是 _loop 的循环里：否则
        # 第一个 spawn 判定时 batch 已经是 1，limit=1 下连它自己都被拒。
        run.spawned_batch += 1

        # 关键：全程**不 acquire _sem**。_loop 正持着根会话的槽，再 acquire 同一把
        # 锁等于自锁。子会话走独立信号量（见 __init__ 与 _guarded_loop）。
        created = await self.start(
            task=task,
            model=alias,
            workspace=str(run.workspace),
            parent=self,
            parent_session=run.session_id,
            depth=run.depth + 1,
        )
        child_id = str(created["session"]["id"])
        run.live_kids += 1
        # reserved 的交割在 _loop 的批循环里做（轮到哪个 spawn 就 -1），这里只
        # 管 live_kids，避免两个计数器互相踩。
        try:
            result = await self._wait_child(run, child_id, role=role)
        finally:
            # 取消 / 异常 / 正常结束三条路径都要把在跑数减回去，否则父会话会被
            # 「并发已满」永久卡死。
            run.live_kids -= 1
            child_run = self._running.get(child_id)
            if child_run is not None:
                child_run.cancelled = True
        return result

    async def _wait_child(
        self, run: _Running, child_id: str, *, role: str
    ) -> ToolOutcome:
        """等一个子会话结束，把它最后的总结转成给父模型的 tool_result。

        两个信号都从内存拿，**不查 DB、不轮询**：完成走 ``run.done``（子的
        ``_guarded_loop`` 在 finally 里 put），等审批读 ``approvals.pending``。
        轮询会每 0.1s 抢一次数据库串行锁，把其它会话的 add_message 挤到后面去。
        """
        child = self._running.get(child_id)
        while child is not None:
            # 子会话挂起在等人批准时，父不该陪着干等——那可能几小时不动。
            waiting = next(iter(child.approvals.pending.values()), None)
            if waiting is not None:
                return ToolOutcome(
                    ok=False,
                    output=(
                        f"子任务（角色 {role}）正在等你批准「{waiting.tool}」。"
                        "请在左侧会话列表里打开它、处理那条审批，然后再让我重试同一个任务。"
                    ),
                )
            try:
                outcome = await asyncio.wait_for(child.done.get(), timeout=_CHILD_POLL)
            except TimeoutError:
                continue
            # 子会话已经结束时 _running 里刚 pop 掉，summary 从 transcript 读。
            return await self._render_child(run, child_id, role=role, outcome=outcome)

        # child 不在 _running：要么从没起来（不可能走到这），要么已经结束了。
        return await self._render_child(run, child_id, role=role, outcome=None)

    async def _render_child(
        self, run: _Running, child_id: str, *, role: str, outcome: dict[str, Any] | None
    ) -> ToolOutcome:
        """把子会话的终态 + 最后一轮助手结论组装成父模型的 tool_result。

        这里**只查一次 DB**：子任务已经结束、不在竞争写路径上。transcript 行
        本身就是 LLM 重放历史，最后一条 assistant 消息就是子任务的结论。
        """
        del run  # 目前不需要；保留签名以便以后加父侧上下文
        if outcome is None:
            return ToolOutcome(ok=False, output=f"子任务（角色 {role}）没有返回结果。")

        status = str(outcome.get("status") or "failed")
        if status != "done":
            error = outcome.get("error") or "未知原因"
            return ToolOutcome(
                ok=False,
                output=f"子任务（角色 {role}）没有成功完成（{status}）：{error}\n"
                       "你可以换个方案重试，或自己完成剩余部分。",
            )
        try:
            rows = await self.repo.messages(child_id)
        except Exception as exc:  # 读不到不能让父会话失败，子任务本身已经 done
            summary = f"（读取子任务结论失败：{exc!r}）"
        else:
            summary = self._child_summary(rows)
        return ToolOutcome(
            ok=True,
            output=(f"子任务（角色 {role}）已完成，下面是它的结论：\n"
                    "---- 子任务结论 ----\n"
                    f"{summary}\n"
                    "-------------------\n"
                    f"（子会话 id：{child_id}，用户可在会话列表里看它的完整过程）"),
        )

    def _child_summary(self, rows: list[dict[str, Any]]) -> str:
        """从子会话 transcript 里挑最后一条助手的正文（截断）。"""
        for row in reversed(rows):
            if row.get("role") != "assistant" or row.get("kind") != "message":
                continue
            text = (row.get("content") or "").strip()
            if text:
                return text[:_MAX_CHILD_SUMMARY]
        return "（子任务没有留下文字结论）"

    # ------------------------------------------------------------------ #
    # Context compaction（临时压缩，不落库：重放时按需重算）
    # ------------------------------------------------------------------ #
    async def _compact(self, run: _Running, messages: list[ChatMessage]) -> list[ChatMessage]:
        if len(messages) <= _COMPACT_TAIL + 2:
            return messages
        head, middle, tail = messages[1], messages[2:-_COMPACT_TAIL], messages[-_COMPACT_TAIL:]
        rendered: list[str] = []
        for m in middle:
            text = m.text()[:400] if m.text() else ""
            rendered.append(f"[{m.role}] {text}" if text else f"[{m.role}] (工具调用)")
        prompt = (
            "请把以下编码任务对话历史压缩成一份要点摘要（800 字以内），"
            "保留：任务目标、已完成的关键操作与结果、重要文件路径、遗留问题。"
            "直接输出摘要正文：\n\n" + "\n".join(rendered)
        )
        try:
            response = await self.requests.chat(
                ChatCompletionRequest(model=run.model,
                                      messages=[ChatMessage(role="user", content=prompt)],
                                      stream=False),
                request_id=f"agent-{run.session_id}-compact",
            )
            summary = response.text()
        except ZKAIError:
            logger.warning("agent session %s compaction failed; continuing uncompressed",
                           run.session_id)
            return messages
        summary_message = ChatMessage(role="user",
                                      content=f"[此前对话的要点摘要]\n{summary}")
        self._emit(run, {"type": "note", "content": "上下文较长，已把中间历史压缩为摘要。"})
        return [messages[0], head, summary_message, *tail]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _replay(rows: list[dict[str, Any]]) -> list[ChatMessage]:
        """agent_messages rows -> OpenAI chat history (display rows skipped)."""
        out: list[ChatMessage] = []
        for row in rows:
            kind = row.get("kind")
            role = row.get("role")
            if kind in {"approval", "note"}:
                continue
            data = row.get("data") or {}
            content = row.get("content")
            if role == "assistant":
                calls = [ToolCall(**tc) for tc in (data.get("tool_calls") or [])
                         if isinstance(tc, dict)]
                out.append(ChatMessage(role="assistant", content=content,
                                       tool_calls=calls or None))
            elif role == "tool":
                out.append(ChatMessage(role="tool", content=content or "",
                                       tool_call_id=str(data.get("tool_call_id") or "")))
            else:
                out.append(ChatMessage(role="user", content=content or ""))
        return out

    def _resolve_workspace(self, workspace: str | None) -> Path:
        raw = Path(workspace) if workspace else self.settings.resolved_workspace
        if not raw.is_absolute():
            raw = PROJECT_ROOT / raw
        raw.mkdir(parents=True, exist_ok=True)
        return raw.resolve()


#: 保活对 loop 任务的强引用（done callback 里自动清理）
_BACKGROUND_TASKS: set[asyncio.Task] = set()

__all__ = ["AgentService"]
