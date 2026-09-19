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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import PROJECT_ROOT, AppConfig, Settings
from app.core.errors import ZKAIError
from app.core.logging import get_logger
from app.database.repository import AgentRepository
from app.models.request import ChatCompletionRequest, ChatMessage, ToolCall
from app.services.agent.confirm import SessionApprovals
from app.services.agent.prompts import build_system_prompt
from app.services.agent.tools import (
    DANGEROUS_TOOLS,
    SAFE_TOOLS,
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


@dataclass(slots=True)
class _Running:
    """Live state of one executing session."""

    session_id: str
    workspace: Path
    model: str
    approvals: SessionApprovals = field(default_factory=SessionApprovals)
    subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    cancelled: bool = False


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
        self, *, task: str, model: str | None = None, workspace: str | None = None
    ) -> dict[str, Any]:
        resolved_model = model or self.settings.agent_default_model
        ws = self._resolve_workspace(workspace)
        session_id = uuid.uuid4().hex
        title = task.strip().splitlines()[0][:80] if task.strip() else "(空任务)"
        await self.repo.create_session(
            session_id=session_id, title=title, model=resolved_model, workspace=str(ws)
        )
        await self.repo.add_message(session_id, role="user", kind="task", content=task)
        run = _Running(session_id=session_id, workspace=ws, model=resolved_model)
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
                       model=row["model"])
        self._running[session_id] = run
        await self.repo.add_message(session_id, role="user", kind="task", content=content)
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

    # ------------------------------------------------------------------ #
    # The loop
    # ------------------------------------------------------------------ #
    def _spawn(self, run: _Running, *, resume: bool) -> None:
        task = asyncio.create_task(self._guarded_loop(run, resume=resume))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    async def _guarded_loop(self, run: _Running, *, resume: bool) -> None:
        session_id = run.session_id
        try:
            async with self._sem:
                await self._loop(run, resume=resume)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            await self.repo.update_status(session_id, "cancelled", error="任务被取消")
            self._emit(run, {"type": "session_done", "status": "cancelled"})
        except Exception as exc:  # the loop must never die silently
            logger.exception("agent session %s crashed", session_id)
            await self.repo.update_status(session_id, "failed", error=repr(exc)[:500])
            self._emit(run, {"type": "session_done", "status": "failed",
                             "error": repr(exc)[:200]})
        finally:
            self._running.pop(session_id, None)

    async def _loop(self, run: _Running, *, resume: bool) -> None:
        session_id = run.session_id
        s = self.settings
        await self.repo.update_status(session_id, "running")
        self._emit(run, {"type": "session_started", "model": run.model,
                         "workspace": str(run.workspace), "resumed": resume})

        system = ChatMessage(role="system", content=build_system_prompt(str(run.workspace),
                                                                        max_steps=s.agent_max_steps))
        messages: list[ChatMessage] = [system, *self._replay(await self.repo.messages(session_id))]
        toolbox = ToolBox(run.workspace, command_timeout=s.agent_command_timeout)
        steps = 0
        in_tokens = 0
        out_tokens = 0

        while True:
            if run.cancelled:
                await self.repo.update_status(session_id, "cancelled", steps=steps,
                                              input_tokens=in_tokens, output_tokens=out_tokens)
                self._emit(run, {"type": "session_done", "status": "cancelled"})
                return
            if steps >= s.agent_max_steps:
                note = f"已达到单任务步数上限（{s.agent_max_steps} 步），任务停止。"
                await self.repo.add_message(session_id, role="assistant", kind="note",
                                            content=note)
                await self.repo.update_status(session_id, "done", steps=steps,
                                              input_tokens=in_tokens, output_tokens=out_tokens)
                self._emit(run, {"type": "note", "content": note})
                self._emit(run, {"type": "session_done", "status": "done",
                                 "reason": "max_steps"})
                return

            request = ChatCompletionRequest(
                model=run.model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                stream=False,
                session_id=f"agent-{session_id}",
            )
            if request.estimated_input_tokens() > s.agent_context_token_limit:
                messages = await self._compact(run, messages)
                request = ChatCompletionRequest(
                    model=run.model,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
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
                return
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
                for call in tool_calls:
                    tool_message = await self._execute_tool(run, toolbox, call)
                    messages.append(tool_message)
                continue

            await self.repo.update_status(session_id, "done", steps=steps,
                                          input_tokens=in_tokens, output_tokens=out_tokens)
            self._emit(run, {"type": "session_done", "status": "done"})
            return

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
