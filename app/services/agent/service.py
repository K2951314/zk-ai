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
except the loop of a session that was mid-flight (marked ``interrupted``)."""

from __future__ import annotations

import asyncio

from app.core.config import AppConfig, Settings
from app.database.repository import AgentRepository
from app.services.agent.service_child import ChildMixin
from app.services.agent.service_common import (  # noqa: F401
    _BACKGROUND_TASKS,
    PERMISSION_MODES,
    _Running,
    logger,
    usage_fields,
)
from app.services.agent.service_delegate import DelegateMixin
from app.services.agent.service_loop import LoopMixin
from app.services.agent.service_session import SessionMixin
from app.services.request_service import RequestService


class AgentService(
    SessionMixin,
    LoopMixin,
    DelegateMixin,
    ChildMixin,
):
    """Agent 工作台的门面：四块职责分在四个 mixin 里，这里只做状态装配。

    为什么这样拆（2026-11-14）：原来 1,818 行一个类、44 个方法，改任何一块都要
    在一个文件里上下翻 1,800 行——**定位成本远大于理解成本**。拆开后：

      service_session.py    会话生命周期与 SSE 管道
      service_loop.py       任务循环、工具调度、审批、上下文压缩
      service_delegate.py   外部执行体委派（直跑/批量/模型派单/产物/续接）
      service_child.py      子任务派生与结论回填
      service_common.py     共享常量、_Running 运行态、消息/附件纯函数

    方法体**逐字节未改**（纯物理搬移），所以调用方、测试、行为都是零影响。
    跨簇调用（36 处，多为 ``self._emit``）靠 MRO 正常解析。
    """

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

