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
from app.services.agent.service import AgentService

router = APIRouter(prefix="/admin/agent", tags=["agent"],
                   dependencies=[Depends(require_admin)])

_SSE_HEARTBEAT = 15.0


def get_agent(request: Request) -> AgentService:
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "gateway is starting up")
    if not container.settings.agent_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent disabled")
    return container.agent_service


AgentDep = Annotated[AgentService, Depends(get_agent)]


class StartPayload(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, max_length=128)
    workspace: str | None = Field(default=None, max_length=500)


class FollowupPayload(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class ApprovalPayload(BaseModel):
    approved: bool
    remember: bool = False


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
                            f"unknown model/alias: {model!r}")
    return await agent.start(task=payload.task, model=payload.model,
                             workspace=payload.workspace)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, agent: AgentDep) -> dict[str, Any]:
    detail = await agent.session_detail(session_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return detail


@router.post("/sessions/{session_id}/messages")
async def followup(session_id: str, payload: FollowupPayload,
                   agent: AgentDep) -> dict[str, Any]:
    try:
        detail = await agent.followup(session_id, payload.content)
    except ZKAIError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return detail


@router.post("/sessions/{session_id}/cancel")
async def cancel(session_id: str, agent: AgentDep) -> dict[str, Any]:
    if not await agent.cancel(session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return {"cancelled": True}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, agent: AgentDep) -> dict[str, Any]:
    try:
        deleted = await agent.delete(session_id)
    except ZKAIError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return {"deleted": True}


@router.post("/sessions/{session_id}/approvals/{approval_id}")
async def decide_approval(session_id: str, approval_id: str, payload: ApprovalPayload,
                          agent: AgentDep) -> dict[str, Any]:
    result = await agent.decide(session_id, approval_id,
                                approved=payload.approved, remember=payload.remember)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "no such pending approval (already decided or session ended)")
    return result


@router.get("/sessions/{session_id}/events")
async def events(session_id: str, agent: AgentDep) -> StreamingResponse:
    """Live session stream: ``snapshot`` then JSON events, ``: ping`` keepalives."""
    detail = await agent.session_detail(session_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")

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
