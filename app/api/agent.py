"""ZK-Agent API under ``/admin/agent``: session CRUD, approvals and the SSE stream.

All routes sit behind ``require_admin`` (same as the rest of ``/admin/*``) and
disappear entirely when ``ZKAI_AGENT_ENABLED=false``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, require_admin
from app.core.container import Container
from app.core.errors import ZKAIError
from app.services.agent import model_inventory as model_inventory_service
from app.services.agent import taskspace, usage_ledger
from app.services.agent.cli import load_cli_registry
from app.services.agent.cli.models import cached_models, discover_models
from app.services.agent.model_catalog import load_catalog as load_model_catalog
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
    #: 图片（data URL 或 http 直链）。前端把截图/图纸转成 data URL 塞这里，
    #: 后端还原成 OpenAI 的 image_url content part —— 模型看到的是图不是文件名。
    images: list[str] = Field(default_factory=list)
    #: 文件（[{"name": ..., "data": "data:...;base64,..."}]）。
    #: 后端落到 data/agent_uploads/<session>/，再把**真实路径**写进任务正文，
    #: 让 Agent 用自己的读写工具处理 —— 比把内容塞进 prompt 有用得多。
    files: list[dict[str, Any]] = Field(default_factory=list)


class FollowupPayload(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    #: 带执行体 = 这一条消息直接交给 CLI；不带 = 照旧走父模型
    executor_role: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    effort: str | None = Field(default=None, max_length=16)
    context_window: int | None = Field(default=None, ge=0, le=2_000_000)
    #: 跟首轮一样的图片字段；只有走父模型（不带 executor_role）时才用得上
    images: list[str] = Field(default_factory=list)
    files: list[dict[str, Any]] = Field(default_factory=list)


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
        # CLI 自己的清单最可信；但只读缓存，绝不在这里同步启动 node 进程。
        # 缓存冷时先用配置快照，前端点开某个执行体再走 /executors/{role}/models 拉 live。
        live = cached_models(tool.name, tool.command)
        models = live or tool.models or ([tool.model] if tool.model else [])
        free = set(tool.free_models)
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
            "models_source": "live" if live else "config",
            "models": [
                {
                    "id": model_id,
                    "free": model_id in free if free else tool.cost_tier == "free",
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


@router.get("/taskspaces")
async def list_taskspaces(container: ContainerDep) -> dict[str, Any]:
    """列出任务空间摘要（只读，不读产物内容）。"""
    return {
        "taskspaces": taskspace.list_all(container.settings.agent_workspace)
    }


def _task_space_path(container: Container, task_id: str) -> Path:
    """解析任务空间路径并做越界校验。"""
    workspace = Path(container.settings.agent_workspace)
    path = workspace / taskspace.DIRNAME / task_id
    if not taskspace.is_inside(workspace, path) or not path.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务空间不存在")
    return path


@router.get("/taskspaces/{task_id}")
async def taskspace_detail(task_id: str, container: ContainerDep) -> dict[str, Any]:
    """读一个任务空间的进度（步骤、产物、交接说明）。"""
    workspace = Path(container.settings.agent_workspace)
    data = taskspace.load(workspace, task_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务空间不存在或进度文件损坏")
    return {"taskspace": data}


@router.post("/taskspaces/{task_id}/release")
async def release_taskspace(task_id: str, container: ContainerDep) -> dict[str, Any]:
    """人工释放占用（执行体崩了但租约还没到期时用）。"""
    workspace = Path(container.settings.agent_workspace)
    if taskspace.load(workspace, task_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务空间不存在")
    holder = taskspace.force_release(workspace, task_id)
    return {"released": bool(holder), "owner": holder}


def _reveal_folder(path: Path) -> None:
    """在系统文件管理器里打开目录（同步实现，调用方放线程里跑）。"""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - 打开目录，不是执行外部程序
        return
    opener = shutil.which("open") or shutil.which("xdg-open")
    if opener is None:
        raise OSError("找不到可用的文件管理器命令")
    subprocess.Popen([opener, str(path)])  # noqa: S603 - 绝对路径 + 已校验目录


@router.post("/taskspaces/{task_id}/open")
async def open_taskspace(task_id: str, container: ContainerDep) -> dict[str, Any]:
    """在系统文件管理器里打开任务空间（仅限本机）。"""
    path = _task_space_path(container, task_id)
    try:
        await asyncio.to_thread(_reveal_folder, path)
    except OSError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"打开文件夹失败：{exc}"
        ) from exc
    return {"ok": True, "path": str(path)}


@router.get("/mcp-usage")
async def mcp_usage(limit: int = 50) -> dict[str, Any]:
    """工具调用账本：最近记录 + 按工具聚合（只读文件，不碰数据库）。"""
    return {
        "recent": usage_ledger.recent(limit),
        "summary": usage_ledger.summary(),
    }


@router.get("/model-catalog")
async def model_catalog(container: ContainerDep) -> dict[str, Any]:
    """重点模型画像：人工确认过的那几款 + 免费档，带证据等级。"""
    entries, warnings = load_model_catalog(container.settings.resolved_config_dir)
    return {
        "models": [entry.as_dict() for entry in entries],
        "warnings": warnings,
    }


@router.get("/model-inventory")
async def model_inventory(
    container: ContainerDep, refresh: bool = False
) -> dict[str, Any]:
    """**每个工具的全部模型 + 价格**（价格标明来源，不编造）。

    三份数据：网关模型的牌价（配置里真实单价）、各执行体的模型清单
    （live 发现优先，冷缓存回落配置快照）、账本里累计的实测成本。

    ``refresh=true`` 时先让各 CLI 重新报一遍清单（约 20 秒，起 node 进程），
    页面可以先用缓存渲染、再后台核实一次，避免打开就卡住。
    """
    if refresh:
        registry = load_cli_registry(container.settings)
        targets = {
            (tool.name, tool.command)
            for tool in registry.tools.values()
            if tool.command and tool.backend != "mcp"
        }
        if targets:
            # 并发跑，别一个个等；discover_models 自己会写 5 分钟缓存。
            await asyncio.gather(
                *(
                    asyncio.to_thread(discover_models, name, command)
                    for name, command in targets
                ),
                return_exceptions=True,
            )
    gateway_models = [
        model
        for model_id in container.config.public_model_ids()
        if (model := container.model_service.describe(model_id))
    ]
    executors = (await list_executors(container))["executors"]
    return model_inventory_service.build(
        gateway_models=gateway_models,
        executors=executors,
        observed=usage_ledger.cost_by_model(),
    )


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
                             workspace=payload.workspace,
                             images=payload.images or None,
                             files=payload.files or None)


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
            detail = await agent.followup(
                session_id, payload.content, payload.images, payload.files
            )
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
