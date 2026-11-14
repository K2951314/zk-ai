from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.services.agent.roles import describe_roles, resolve_alias
from app.services.agent.service_common import (
    _CHILD_POLL,
    _MAX_CHILD_SUMMARY,
    _Running,
)
from app.services.agent.tools import (
    ToolError,
    ToolOutcome,
)

if TYPE_CHECKING:  # 只在类型检查时存在，运行期不 import（避免循环）
    from app.core.config import Settings
    from app.database.repository import AgentRepository



class ChildMixin:
    """子任务派生与结论回填（从 service.py 拆出，2026-11-14）。

    方法体逐字节从 service.py 搬来（2026-11-14 拆分），逻辑未改。
    跨簇依赖的属性在下面的 ``TYPE_CHECKING`` 块里声明——运行期零开销，
    只是让静态检查器知道 ``self`` 上有哪些成员。
    """

    if TYPE_CHECKING:
        repo: AgentRepository
        settings: Settings
        _running: dict[str, _Running]
        _emit: Any
        start: Any
        validate_model: Any

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
