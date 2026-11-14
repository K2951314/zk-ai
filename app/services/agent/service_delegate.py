from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.errors import ZKAIError
from app.services.agent import taskspace, usage_ledger
from app.services.agent.cli import CliRegistry, load_cli_registry
from app.services.agent.cli import delegate as cli_delegate
from app.services.agent.cli import prompting as cli_prompting
from app.services.agent.cli.registry import EFFORT_LEVELS
from app.services.agent.cli.runner import run_delegation, summarize_cli_line
from app.services.agent.service_common import (
    _ACTIVE_STATUSES,
    _BACKGROUND_TASKS,
    _CLI_LOG_THROTTLE,
    _RESUMABLE_STATUSES,
    _Running,
    logger,
    usage_fields,
)
from app.services.agent.tools import (
    ToolBox,
    ToolError,
    ToolOutcome,
)

if TYPE_CHECKING:  # 只在类型检查时存在，运行期不 import（避免循环）
    from app.core.config import AppConfig, Settings
    from app.database.repository import AgentRepository



class DelegateMixin:
    """外部执行体委派：直跑/批量/模型派单/产物清单/续接（拆出，2026-11-14）。

    方法体逐字节从 service.py 搬来（2026-11-14 拆分），逻辑未改。
    跨簇依赖的属性在下面的 ``TYPE_CHECKING`` 块里声明——运行期零开销，
    只是让静态检查器知道 ``self`` 上有哪些成员。
    """

    if TYPE_CHECKING:
        repo: AgentRepository
        settings: Settings
        config: AppConfig
        _running: dict[str, _Running]
        _emit: Any
        _resolve_workspace: Any
        _approve: Any
        _require_root: Any
        session_detail: Any


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
            permission="ask",
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
            permission="ask",
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

    def _cli_line_handler(self, run: _Running) -> Any:
        """执行体实时日志 → 摘要 → 节流 → SSE ``cli_log`` 事件。

        纯本地管道 I/O + 本地 SSE 推送，**不发任何额外上游请求、不额外消耗
        积分**。日志只在前端显示，不落 transcript，避免把上千行 INFO 日志
        写进会话历史。
        """
        last = [0.0]

        def handle(stream: str, line: str) -> None:
            summary = summarize_cli_line(line)
            if summary is None:
                return
            now = time.monotonic()
            if now - last[0] < _CLI_LOG_THROTTLE:
                return
            last[0] = now
            self._emit(run, {"type": "cli_log", "content": summary, "stream": stream})

        return handle
    @staticmethod
    def _resume_ref_for(
        workspace: Path,
        space: taskspace.TaskSpace | None,
        tool: str,
    ) -> str:
        """从任务空间读上一次的会话标识，用于续接（B-4）。

        只在**同一任务空间 + 同一执行体**里复用 ref：
        - 换执行体（claude 的 session_id 给 codex 是没意义的）；
        - 换任务空间（那是另一个任务，接着跑会串味）。

        ref 来自 ``_with_artifacts`` 落的 ``handoff_session``；它同时记了
        ``handoff_tool``，两个都对上才用。读不到就返回空 = 全新会话。
        """
        if space is None:
            return ""
        try:
            data = taskspace.load(workspace, space.task_id) or {}
        except (OSError, ValueError):
            return ""
        if str(data.get("handoff_tool") or "") != tool:
            return ""
        ref = str(data.get("handoff_session") or "").strip()
        return ref

    def _with_artifacts(
        self,
        output: str,
        *,
        cwd: Any,
        workspace: Path,
        space: taskspace.TaskSpace | None,
        step: int | None = None,
        session_ref: str = "",
        tool: str = "",
    ) -> str:
        """把 ``<cwd>/outputs`` 里的产物列出来，追加到回喂正文，并落 state.json。

        任务空间承诺「产物落 outputs/」，但此前全仓没有一处读过它：控制台的
        任务空间页永远显示「（暂无）」，调用方也只能靠执行体自己汇报。
        **调用方要的是「你产出了什么」，不是「你说你产出了什么」。**

        只追加文件清单（路径 + 大小），不读内容——内容由执行体自己在结论里说，
        这里只提供一个不依赖它诚实的旁证。写盘失败一律吞掉，不影响主流程。

        ``session_ref`` 是上游自报的会话标识（claude 的 session_id）：落盘是为了
        「同一个会话里再派一次」时能续接，而不是从零重跑。没有就留空。
        """
        items = cli_prompting.collect_artifacts(cwd)
        note = cli_prompting.format_artifacts(items)
        if space is not None:
            try:
                current = taskspace.load(workspace, space.task_id) or {}
                steps = list(current.get("steps") or [])
                if step is not None:
                    steps.append({"step": step, "artifacts": len(items)})
                patch: dict[str, Any] = {
                    "steps": steps,
                    "artifacts": items,
                    "handoff_brief": (output or "")[:500],
                }
                if session_ref:
                    patch["handoff_session"] = session_ref
                    # 同时记是**哪个执行体**的会话：ref 只在同一执行体内有意义
                    # （claude 的 uuid 给 codex 没用）。续接时两个都要对上。
                    if tool:
                        patch["handoff_tool"] = tool
                taskspace.update(workspace, space.task_id, **patch)
            except (OSError, ValueError):
                pass
        if not note:
            return output
        return f"{output}\n\n{note}"
    @staticmethod
    def _idle_timeout_for(config: Any) -> float | None:
        """该执行体的静默判死阈值；0/缺省 = 不判空闲，只看总时长。

        只有会持续打进度日志的执行体（码道 ``--print-logs``）才值得设这个值。
        对「结束才一次性吐输出」的无头 CLI，静默是常态，设了就是误杀。
        """
        value = float(getattr(config, "idle_timeout", 0) or 0)
        if value <= 0:
            return None
        return min(value, 1900.0)  # 比租约 TTL 略短：空闲判死不该晚于租约过期

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
        # 直跑与模型派单共用同一套任务空间与租约：先占空间，再执行。
        space, delegation_cwd, allow_bypass, space_error = self._enter_task_space(
            run, goal=task
        )
        if space_error:
            error = f"{space_error}，本次没有执行"
            await self.repo.update_status(session_id, "failed", error=error)
            self._emit(run, {
                "type": "session_done",
                "status": "failed",
                "error": error,
            })
            outcome.update(status="failed", error=error)
            run.done.put_nowait(outcome)
            self._running.pop(session_id, None)
            return
        try:
            await self.repo.update_status(session_id, "running")
            self._emit(run, {
                "type": "session_started",
                "model": config.model,
                "workspace": str(run.workspace),
                "resumed": False,
            })
            self._emit(run, {"type": "step_start", "step": 1, "model": config.model})
            result = await run_delegation(
                config, task=task, cwd=delegation_cwd, allow_bypass=allow_bypass,
                on_line=self._cli_line_handler(run),
                cancel=run.cancel_event,
                idle_timeout=self._idle_timeout_for(config),
                # 同一任务空间里重派同一个执行体时续接上次会话（B-4）。
                resume_ref=self._resume_ref_for(run.workspace, space, config.name),
            )
            # 记账：CLI 没有价目表，这条记录是「实测价格」的唯一来源。
            usage_ledger.record_delegation(
                tool=config.name,
                model=config.model,
                role=config.name,
                session_id=session_id,
                task_space=str(run.workspace),
                ok=result.ok,
                elapsed=result.elapsed,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=result.cost_usd,
                session_ref=result.session_ref,
                error="" if result.ok else result.output[:200],
            )
            status = "done" if result.ok else "failed"
            error = None if result.ok else result.output[:300]
            if run.cancelled:
                # 子进程已被 watchdog kill（2026-11-12 起真 kill），状态如实记成
                # 「已取消」，否则界面会覆盖掉用户刚点的停止。
                status, error = "cancelled", "任务被取消"
            body = self._with_artifacts(
                result.output, cwd=delegation_cwd, workspace=run.workspace,
                space=space, session_ref=result.session_ref, tool=config.name,
            )
            payload = {
                **info,
                **usage_fields(result),
                "elapsed": round(result.elapsed, 1),
                "ok": result.ok,
            }
            seq = await self.repo.add_message(
                session_id,
                role="assistant",
                kind="message",
                content=body,
                data={"executor": payload},
            )
            self._emit(run, {
                "type": "assistant",
                "seq": seq,
                "content": body,
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
            if space is not None:
                taskspace.release(run.workspace, space.task_id, owner=session_id)
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
                permission="ask",
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
            permission="ask",
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
        #: 上一段 (角色, 结论)；第一段为 None。跨段交接的唯一来源。
        prev: tuple[str, str] | None = None
        # 一批多段共享同一个任务空间：先让 A 查、再让 B 审，产物落在同一处。
        goals = "；".join(task for _, task, _ in resolved)[:200]
        space, batch_cwd, allow_bypass, space_error = self._enter_task_space(
            run, goal=goals
        )
        if space_error:
            error = f"{space_error}，本次没有执行"
            await self.repo.update_status(session_id, "failed", error=error)
            self._emit(run, {
                "type": "session_done",
                "status": "failed",
                "error": error,
            })
            outcome.update(status="failed", error=error)
            run.done.put_nowait(outcome)
            self._running.pop(session_id, None)
            return
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
                # 段间交接：上一段的结论注入下一段（2026-11-13）。没有这一步，
                # 「先让 A 查、再让 B 审」里的 B 根本看不到 A 查到了什么，
                # 两段唯一的联系只是共享一个目录。
                handed = task
                if prev is not None:
                    handed = cli_prompting.with_context(prev[0], prev[1], task)
                result = await run_delegation(
                    config, task=handed, cwd=batch_cwd, allow_bypass=allow_bypass,
                    on_line=self._cli_line_handler(run),
                    cancel=run.cancel_event,
                    idle_timeout=self._idle_timeout_for(config),
                    resume_ref=self._resume_ref_for(run.workspace, space, config.name),
                )
                prev = (role, result.output)
                # 记账：批量里每一段单独记，才能看出「哪一段最贵」。
                usage_ledger.record_delegation(
                    tool=config.name,
                    model=config.model,
                    role=role,
                    session_id=session_id,
                    task_space=str(run.workspace),
                    ok=result.ok,
                    elapsed=result.elapsed,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    cost_usd=result.cost_usd,
                    session_ref=result.session_ref,
                    error="" if result.ok else result.output[:200],
                )
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
                    **usage_fields(result),
                }
                body = self._with_artifacts(
                    result.output, cwd=batch_cwd, workspace=run.workspace,
                    space=space, step=index, session_ref=result.session_ref,
                    tool=config.name,
                )
                seq = await self.repo.add_message(
                    session_id,
                    role="assistant",
                    kind="message",
                    content=body,
                    data={"executor": payload},
                )
                self._emit(run, {
                    "type": "assistant",
                    "seq": seq,
                    "content": body,
                    "thinking": None,
                    "tool_calls": [],
                    "routing": None,
                    "step": index,
                    "executor": payload,
                })
                if run.cancelled:
                    status, error = "cancelled", "任务被取消"
                    break
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
            if space is not None:
                taskspace.release(run.workspace, space.task_id, owner=session_id)
            run.done.put_nowait(outcome)
            self._running.pop(session_id, None)

    def _enter_task_space(
        self, run: _Running, *, goal: str
    ) -> tuple[taskspace.TaskSpace | None, Path, bool, str]:
        """建任务空间并抢租约，三条派单路径共用。

        返回 ``(空间, 工作目录, 是否允许 bypass, 错误说明)``。错误非空时由调用方
        把失败写回自己的界面（工具结果 / 会话状态 / 事件回执），因为三条路径
        的失败呈现方式不同，不适合在这里统一。
        """
        if not self.settings.agent_task_space:
            return None, run.workspace, False, ""
        space = taskspace.create(run.workspace, session_id=run.session_id, goal=goal)
        got, holder = taskspace.acquire(
            run.workspace, space.task_id, owner=run.session_id
        )
        if not got:
            return None, run.workspace, False, f"任务空间正被 {holder} 使用"
        allow = self.settings.agent_permission_mode == "bypass"
        return space, space.path, allow, ""

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
        args = dict(args)
        # 任务空间：没指定 cwd 时自动开一个独立目录，执行体在里面工作，
        # 主工作区不被直接改写（产物落 outputs/，进度落 state.json）。
        space: taskspace.TaskSpace | None = None
        # 模型发起的委派**一律不提权**（2026-11-13 收紧）：
        # ``agent_permission_mode == "bypass"`` 的语义是「任务空间内不弹审批卡」，
        # 不是「把执行体的沙箱也关掉」。模型派单本来就有审批卡兜底，若再借这个
        # 全局开关给外部 CLI 加 --dangerously-skip-permissions，等于模型只要把
        # 工作目录说成任务空间内，外部执行体就能无沙箱跑——边界从一个开关滑成两个。
        # 真正需要提权的只有用户亲自 @ 的直跑路径（start_delegation）。
        allow_bypass = False
        if not str(args.get("cwd") or "").strip():
            space, space_cwd, _ignored, space_error = self._enter_task_space(
                run, goal=str(args.get("task") or "")
            )
            if space_error:
                return ToolOutcome(
                    ok=False,
                    output=(
                        f"{space_error}，为避免两边互相覆盖，本次没有派单。"
                        "等它完成后再试，或者换一个任务重开一个空间。"
                    ),
                )
            if space is not None:
                args["cwd"] = space_cwd.relative_to(run.workspace).as_posix()
        try:
            # 预览与执行必须用同一个 allow_bypass：审批卡上写的命令就是真要跑的命令。
            prepared = cli_delegate.prepare(
                run.cli_registry, toolbox, args, allow_bypass=allow_bypass
            )
            rejected = await self._approve(run, cli_delegate.TOOL_NAME, args, prepared)
            if rejected is not None:
                return rejected
            config = run.cli_registry.tool_for_role(str(args.get("role") or "").strip())
            outcome = await cli_delegate.perform(
                run.cli_registry,
                toolbox,
                args,
                allow_bypass=allow_bypass,
                session_id=session_id,
                task_space=str(run.workspace),
                # 执行句柄归最上层：停止按钮要能真 kill，实时日志要能看见。
                cancel=run.cancel_event,
                on_line=self._cli_line_handler(run),
                idle_timeout=self._idle_timeout_for(config),
                resume_ref=self._resume_ref_for(run.workspace, space, config.name)
                if config is not None
                else "",
            )
            # 产物清单与批次/直跑同一套口径（模型派单也是「执行体在任务空间里干活」）。
            try:
                cwd = toolbox.resolve(str(args.get("cwd") or "."))
            except ToolError:
                return outcome
            outcome.output = self._with_artifacts(
                outcome.output, cwd=cwd, workspace=run.workspace, space=space,
                session_ref=outcome.session_ref,
                tool=config.name if config is not None else "",
            )
            return outcome
        finally:
            if space is not None:
                taskspace.release(run.workspace, space.task_id, owner=session_id)
