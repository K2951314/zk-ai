from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.config import PROJECT_ROOT
from app.core.errors import ZKAIError
from app.services.agent.cli import load_cli_registry
from app.services.agent.service_common import (
    _ACTIVE_STATUSES,
    _BACKGROUND_TASKS,
    _RESUMABLE_STATUSES,
    PERMISSION_MODES,
    _attachment_data,
    _describe_files,
    _Running,
    _store_files,
    logger,
)

if TYPE_CHECKING:  # 只在类型检查时存在，运行期不 import（避免循环）
    from app.core.config import AppConfig, Settings
    from app.database.repository import AgentRepository
    from app.services.agent.service import AgentService
    from app.services.request_service import RequestService



class SessionMixin:
    """会话生命周期与 SSE 管道（从 service.py 拆出，2026-11-14）。

    方法体逐字节从 service.py 搬来（2026-11-14 拆分），逻辑未改。
    跨簇依赖的属性在下面的 ``TYPE_CHECKING`` 块里声明——运行期零开销，
    只是让静态检查器知道 ``self`` 上有哪些成员。
    """

    if TYPE_CHECKING:
        repo: AgentRepository
        requests: RequestService
        settings: Settings
        config: AppConfig
        _running: dict[str, _Running]
        _sem: asyncio.Semaphore
        _child_sem: asyncio.Semaphore
        _loop: Any
        _spawn_child: Any
        _delegate_cli: Any


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
        permission: str | None = None,
        parent: AgentService | None = None,
        parent_session: str | None = None,
        depth: int = 1,
        images: list[str] | None = None,
        files: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        resolved_model = model or self.settings.agent_default_model
        perm = self._resolve_permission(permission)
        ws = self._resolve_workspace(workspace)
        session_id = uuid.uuid4().hex
        title = task.strip().splitlines()[0][:80] if task.strip() else "(空任务)"
        await self.repo.create_session(
            session_id=session_id, title=title, model=resolved_model,
            workspace=str(ws), permission=perm,
        )
        # 文件先落盘，再把路径写进任务正文 —— Agent 拿路径就能自己读
        stored = _store_files(files, data_dir=self.settings.resolved_data_dir,
                              session_id=session_id)
        if stored:
            task = f"{task}{_describe_files(stored)}"
        await self.repo.add_message(
            session_id, role="user", kind="task", content=task,
            data=_attachment_data(images, stored),
        )
        run = _Running(
            session_id=session_id,
            workspace=ws,
            model=resolved_model,
            permission=perm,
            parent=parent,
            parent_session=parent_session,
            depth=depth,
            cli_registry=load_cli_registry(self.settings),
        )
        self._running[session_id] = run
        self._spawn(run, resume=False)
        return {"session": await self.repo.get_session(session_id), "messages": []}

    async def followup(
        self,
        session_id: str,
        content: str,
        images: list[str] | None = None,
        files: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
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
                       model=row["model"], permission=row.get("permission") or "default",
                       cli_registry=load_cli_registry(self.settings))
        self._running[session_id] = run
        stored = _store_files(files, data_dir=self.settings.resolved_data_dir,
                              session_id=session_id)
        if stored:
            content = f"{content}{_describe_files(stored)}"
        await self.repo.add_message(
            session_id, role="user", kind="task", content=content,
            data=_attachment_data(images, stored),
        )
        await self.repo.update_status(session_id, "running", error=None)
        self._spawn(run, resume=True)
        return await self.session_detail(session_id)

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
        run.cancel_event.set()  # runner 的 watchdog 会 kill 子进程
        run.approvals.deny_all()  # 唤醒等审批的循环，让它走取消分支
        return True

    async def delete(self, session_id: str) -> bool:
        if session_id in self._running:
            raise ZKAIError("会话正在执行，先取消再删除", http_status=409)
        return await self.repo.delete_session(session_id)

    async def set_permission(
        self, session_id: str, permission: str
    ) -> dict[str, Any] | None:
        """改一个会话的权限档；正在跑的会话立刻生效（下一次工具调用起）。"""
        perm = self._resolve_permission(permission)
        if not await self.repo.update_permission(session_id, perm):
            return None
        run = self._running.get(session_id)
        if run is not None:
            run.permission = perm
        return await self.repo.get_session(session_id)

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
    @staticmethod
    def _resolve_permission(permission: str | None) -> str:
        """校验权限档；空值走 default（界内写文件免批）。"""
        perm = (permission or "default").strip().lower()
        if perm not in PERMISSION_MODES:
            raise ZKAIError(
                f"未知的权限档 {permission!r}，可选："
                "default（默认）/ ask（每次都问）/ full（完全访问）",
                http_status=400,
            )
        return perm

    def _resolve_workspace(self, workspace: str | None) -> Path:
        raw = Path(workspace) if workspace else self.settings.resolved_workspace
        if not raw.is_absolute():
            raw = PROJECT_ROOT / raw
        raw.mkdir(parents=True, exist_ok=True)
        return raw.resolve()
