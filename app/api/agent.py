"""ZK-Agent API under ``/admin/agent``: session CRUD, approvals and the SSE stream.

All routes sit behind ``require_admin`` (same as the rest of ``/admin/*``) and
disappear entirely when ``ZKAI_AGENT_ENABLED=false``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, require_admin
from app.core.container import Container
from app.core.errors import ZKAIError
from app.services.agent.cli import load_cli_registry
from app.services.agent.cli.models import discover_models
from app.services.agent.service import AgentService

router = APIRouter(prefix="/zkadmin/agent", tags=["agent"],
                   dependencies=[Depends(require_admin)])

_SSE_HEARTBEAT = 15.0


def get_agent(request: Request) -> AgentService:
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "网关正在启动")
    if not container.settings.agent_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent 功能已停用")
    return container.agent_service


AgentDep = Annotated[AgentService, Depends(get_agent)]


class StartPayload(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, max_length=128)
    workspace: str | None = Field(default=None, max_length=500)


class FollowupPayload(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    #: 带执行体 = 这一条消息直接交给 CLI；不带 = 照旧走父模型
    executor_role: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    effort: str | None = Field(default=None, max_length=16)
    context_window: int | None = Field(default=None, ge=0, le=2_000_000)


class ApprovalPayload(BaseModel):
    approved: bool
    remember: bool = False


class ExecutorRunPayload(BaseModel):
    """控制台「执行体」面板提交的任务：直接跑 CLI，不经父模型。"""

    role: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, max_length=128)
    effort: str | None = Field(default=None, max_length=16)
    context_window: int | None = Field(default=None, ge=0, le=2_000_000)
    workspace: str | None = Field(default=None, max_length=500)


class DelegationItem(BaseModel):
    role: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, max_length=128)
    effort: str | None = Field(default=None, max_length=16)
    context_window: int | None = Field(default=None, ge=0, le=2_000_000)


class DelegationPayload(BaseModel):
    """一条消息里点名多个执行体：按顺序串行跑。"""

    items: list[DelegationItem] = Field(min_length=1, max_length=10)
    session_id: str | None = Field(default=None, max_length=64)
    workspace: str | None = Field(default=None, max_length=500)


@router.get("/executors")
async def list_executors(container: ContainerDep) -> dict[str, Any]:
    """可派的执行体花名册（控制台 @ / 面板的数据源）。"""
    registry = load_cli_registry(container.settings)
    items: list[dict[str, Any]] = []
    for role in registry.known_roles():
        rc = registry.roles[role]
        tool = registry.tools[rc.tool]
        models = tool.models or [tool.model]
        items.append({
            "role": role,
            "tool": tool.name,
            "model": tool.model,
            "cost_tier": tool.cost_tier,
            "effort": tool.effort,
            "context_window": tool.context_window,
            "hint": rc.hint,
            # 只有这两家 CLI 有思考/上下文旋钮；gemini 0.61 没有暴露
            "knobs": ["effort", "context_window"] if tool.name in {"codebuddy", "qoder"} else [],
            "models": [
                {
                    "id": model_id,
                    "free": model_id in tool.free_models
                    if tool.free_models
                    else tool.cost_tier == "free",
                }
                for model_id in models
            ],
        })
    return {"executors": items}


@router.get("/executors/{role}/models")
async def executor_models(role: str, container: ContainerDep) -> dict[str, Any]:
    """该执行体**真正能调**的模型清单（能用 CLI 自己的口径就用它）。

    qoder 走 ``--list-models``，codebuddy 解析 ``--help`` 的 --model 说明；
    两条都拿不到时回落到 cli_tools.yaml 里的 ``models``。
    """
    registry = load_cli_registry(container.settings)
    config = registry.tool_for_role(role)
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"执行体角色 {role!r} 不可用")
    live = await asyncio.to_thread(discover_models, config.name, config.command)
    ids = live or config.models or [config.model]
    free = set(config.free_models)
    return {
        "role": role,
        "source": "live" if live else "config",
        "models": [{"id": model_id, "free": model_id in free} for model_id in ids],
    }


@router.post("/delegations", status_code=201)
async def run_delegations(payload: DelegationPayload, agent: AgentDep) -> dict[str, Any]:
    """多段执行体任务：按顺序串行跑，结果逐段落进同一条 transcript。"""
    try:
        return await agent.start_delegations(
            items=[item.model_dump() for item in payload.items],
            session_id=payload.session_id,
            workspace=payload.workspace,
        )
    except ZKAIError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc


@router.post("/executors", status_code=201)
async def run_executor(payload: ExecutorRunPayload, agent: AgentDep) -> dict[str, Any]:
    """建一个「执行体直跑」会话；结果通过既有 SSE 流回来。"""
    try:
        return await agent.start_delegation(
            role=payload.role,
            task=payload.task,
            model=payload.model,
            effort=payload.effort,
            context_window=payload.context_window,
            workspace=payload.workspace,
        )
    except ZKAIError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc


@router.get("/models")
async def list_models(agent: AgentDep, container: ContainerDep) -> dict[str, Any]:
    """Aliases + models the session picker may use."""
    del agent
    return {
        "default": container.settings.agent_default_model,
        "aliases": sorted(container.config.aliases),
        "models": sorted(container.config.models),
    }


@router.get("/sessions")
async def list_sessions(agent: AgentDep) -> dict[str, Any]:
    return {"sessions": await agent.list_sessions()}


@router.post("/sessions", status_code=201)
async def create_session(payload: StartPayload, agent: AgentDep,
                         container: ContainerDep) -> dict[str, Any]:
    model = payload.model or container.settings.agent_default_model
    if not agent.validate_model(model):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"未知的模型/别名：{model!r}")
    return await agent.start(task=payload.task, model=payload.model,
                             workspace=payload.workspace)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, agent: AgentDep) -> dict[str, Any]:
    detail = await agent.session_detail(session_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return detail


@router.post("/sessions/{session_id}/messages")
async def followup(session_id: str, payload: FollowupPayload,
                   agent: AgentDep) -> dict[str, Any]:
    try:
        if payload.executor_role:
            # 同一个会话里换执行体再派一次：结果照样落进这条 transcript
            detail = await agent.followup_delegation(
                session_id,
                role=payload.executor_role,
                task=payload.content,
                model=payload.model,
                effort=payload.effort,
                context_window=payload.context_window,
            )
        else:
            detail = await agent.followup(session_id, payload.content)
    except ZKAIError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return detail


@router.post("/sessions/{session_id}/cancel")
async def cancel(session_id: str, agent: AgentDep) -> dict[str, Any]:
    if not await agent.cancel(session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return {"cancelled": True}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, agent: AgentDep) -> dict[str, Any]:
    try:
        deleted = await agent.delete(session_id)
    except ZKAIError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return {"deleted": True}


@router.post("/sessions/{session_id}/approvals/{approval_id}")
async def decide_approval(session_id: str, approval_id: str, payload: ApprovalPayload,
                          agent: AgentDep) -> dict[str, Any]:
    result = await agent.decide(session_id, approval_id,
                                approved=payload.approved, remember=payload.remember)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "没有这条待审批请求（可能已处理，或会话已结束）")
    return result


@router.get("/sessions/{session_id}/events")
async def events(session_id: str, agent: AgentDep) -> StreamingResponse:
    """Live session stream: ``snapshot`` then JSON events, ``: ping`` keepalives."""
    detail = await agent.session_detail(session_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")

    async def stream() -> Any:
        queue = await agent.subscribe(session_id)
        try:
            # Late joiners get the full transcript first, then live events.
            detail = await agent.session_detail(session_id)
            if detail is not None:
                yield _sse({"type": "snapshot", "session": detail["session"],
                            "messages": detail["messages"]})
                if detail["session"]["status"] not in {"running", "waiting_approval"}:
                    yield _sse({"type": "session_done",
                                "status": detail["session"]["status"]})
                    return
            else:
                yield _sse({"type": "session_done", "status": "deleted"})
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(event)
                if event.get("type") == "session_done":
                    return
        finally:
            agent.unsubscribe(session_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


__all__ = ["router"]
