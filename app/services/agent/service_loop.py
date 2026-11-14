from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from app.core.errors import ZKAIError
from app.models.request import ChatCompletionRequest, ChatMessage, ToolCall
from app.services.agent import mcp_call, usage_ledger
from app.services.agent.cli import delegate as cli_delegate
from app.services.agent.model_catalog import describe_channels, load_catalog
from app.services.agent.prompts import build_system_prompt
from app.services.agent.service_common import (
    _COMPACT_TAIL,
    _Running,
    _user_content,
    logger,
)
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

if TYPE_CHECKING:  # 只在类型检查时存在，运行期不 import（避免循环）
    from app.core.config import AppConfig, Settings
    from app.database.repository import AgentRepository
    from app.services.request_service import RequestService



class LoopMixin:
    """任务循环、工具调度、审批与上下文压缩（从 service.py 拆出，2026-11-14）。

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
        _emit: Any
        _spawn_child: Any
        _delegate_cli: Any


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

        # 模型画像里有「只能在桌面端用」的条目（CatPawAI / WorkBuddy），
        # 它们不在执行体花名册里。不把这份清单注入提示词，模型就会回
        # 「我这边没有这个模型」——2026-11-08 运营者踩过。
        catalog, _catalog_warnings = load_catalog(s.resolved_config_dir)
        channels = describe_channels(catalog, set(run.cli_registry.tools))
        system = ChatMessage(
            role="system",
            content=build_system_prompt(
                str(run.workspace),
                max_steps=s.agent_max_steps,
                permission=run.permission,
                model_channels=channels,
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
        # mcp_call 只在有 MCP 白名单时出现；子会话同样不给（与 delegate_cli 同理）。
        if run.parent is None:
            mcp_schema = mcp_call.build_schema(run.cli_registry)
            if mcp_schema is not None:
                schemas = [*schemas, mcp_schema]
        steps = 0
        in_tokens = 0
        out_tokens = 0

        while True:
            if run.cancelled:
                await self.repo.update_status(session_id, "cancelled", steps=steps,
                                              input_tokens=in_tokens, output_tokens=out_tokens)
                self._emit(run, {"type": "session_done", "status": "cancelled"})
                return {"status": "cancelled", "error": "任务被取消"}
            if s.agent_max_steps > 0 and steps >= s.agent_max_steps:
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
    @staticmethod
    def _needs_approval(run: _Running, tool: str) -> bool:
        """按会话权限档判断这个危险工具要不要停下来问用户。

        只决定「问不问」，不决定「能不能」：``ToolBox`` 的路径越界检查与
        命令黑名单在 ``perform`` 里照跑，任何档位都绕不过去。
        """
        if run.approvals.skipped(tool) or run.permission == "full":
            return False
        return not (run.permission == "default" and tool == "write_file")

    async def _dispatch(
        self, run: _Running, toolbox: ToolBox, name: str, args: dict[str, Any]
    ) -> ToolOutcome:
        session_id = run.session_id
        try:
            if name in SUPERVISOR_TOOLS:
                return await self._spawn_child(run, args)
            if name == cli_delegate.TOOL_NAME:
                return await self._delegate_cli(run, toolbox, args)
            if name == mcp_call.TOOL_NAME:
                return await self._mcp_call(run, toolbox, args)
            if name in SAFE_TOOLS:
                return await toolbox.perform(name, args)
            if name not in DANGEROUS_TOOLS:
                raise ToolError(f"未知工具 {name!r}，可用：{sorted(SAFE_TOOLS | DANGEROUS_TOOLS)}")
            if self._needs_approval(run, name):
                prepared = toolbox.prepare(name, args)  # 黑名单在这里直接 ToolError
                preview = str((prepared.display or {}).get("diff")
                              or (prepared.display or {}).get("command") or "")
                approval = run.approvals.register(
                    name, args, preview, prepared.display, role=""
                )
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
    async def _approve(
        self,
        run: _Running,
        name: str,
        args: dict[str, Any],
        prepared: ToolOutcome,
    ) -> ToolOutcome | None:
        """两阶段审批；返回 None 表示已批准（或本会话已免打扰），可以继续执行。

        ``delegate_cli`` 的免打扰按**角色**记（见 ``SessionApprovals._key``）：
        放行一次免费档，不该等于放行所有角色（含收费 + bypass 档）。
        """
        role = str(args.get("role") or "").strip()
        if run.approvals.skipped(name, role=role):
            return None
        preview = str(
            (prepared.display or {}).get("diff")
            or (prepared.display or {}).get("command")
            or ""
        )
        approval = run.approvals.register(
            name, args, preview, prepared.display, role=role
        )
        await self.repo.update_status(run.session_id, "waiting_approval")
        display = prepared.display or {}
        seq = await self.repo.add_message(
            run.session_id,
            role="assistant",
            kind="approval",
            content=preview or f"请求执行 {name}",
            data={
                "approval_id": approval.id,
                "tool": name,
                "args": args,
                # 实际生效的权限档（提权之后的值）：各 CLI 的档位词表不齐，
                # 「plan 就是只读」不成立，界面必须显示真档位而不是配置里的名义值。
                **({"approval_mode": display["approval_mode"]} if "approval_mode" in display else {}),
            },
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
        await self.repo.update_status(run.session_id, "running")
        if not approval.approved:
            return ToolOutcome(
                ok=False,
                output="用户拒绝了此操作。请调整方案后重试，或直接给出结论。",
            )
        return None

    async def _mcp_call(
        self, run: _Running, toolbox: ToolBox, args: dict[str, Any]
    ) -> ToolOutcome:
        """mcp_call：直接调一个白名单里的 MCP 工具，走与委派相同的两阶段审批。"""
        server = str(args.get("server") or "")
        tool = str(args.get("tool") or "")
        started = time.monotonic()
        try:
            prepared = mcp_call.prepare(run.cli_registry, toolbox, args)
            rejected = await self._approve(run, mcp_call.TOOL_NAME, args, prepared)
            if rejected is not None:
                return rejected
            outcome = await mcp_call.perform(run.cli_registry, toolbox, args)
        except ToolError as exc:
            outcome = ToolOutcome(ok=False, output=f"工具执行失败：{exc}")
        # 记账失败不影响主流程（usage_ledger 自己吞异常）。
        usage_ledger.record(
            tool=tool or "未知",
            server=server,
            session_id=run.session_id,
            task_space=str(run.workspace),
            ok=outcome.ok,
            elapsed=time.monotonic() - started,
            error="" if outcome.ok else outcome.output[:200],
        )
        return outcome
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
                out.append(ChatMessage(role="user", content=_user_content(content, data)))
        return out
