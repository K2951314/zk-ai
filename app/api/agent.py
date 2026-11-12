"""ZK-Agent API under ``/admin/agent``: session CRUD, approvals and the SSE stream.

All routes sit behind ``require_admin`` (same as the rest of ``/admin/*``) and
disappear entirely when ``ZKAI_AGENT_ENABLED=false``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, require_admin
from app.core.config import PROJECT_ROOT
from app.core.container import Container
from app.core.errors import ZKAIError
from app.services.agent import model_inventory as model_inventory_service
from app.services.agent import taskspace, usage_ledger
from app.services.agent.cli import load_cli_registry
from app.services.agent.cli.models import (
    cached_model_details,
    cached_models,
    discover_model_details,
    discover_models,
    probe_models,
    read_desktop_catalog,
)
from app.services.agent.model_catalog import load_catalog as load_model_catalog
from app.services.agent.service import AgentService

router = APIRouter(prefix="/zkadmin/agent", tags=["agent"],
                   dependencies=[Depends(require_admin)])

_SSE_HEARTBEAT = 15.0
#: 工作区选择器一次最多返回的子目录数（超大目录只给前 N 个，避免前端卡住）。
_MAX_DIR_ENTRIES = 500


def get_agent(request: Request) -> AgentService:
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "网关正在启动")
    if not container.settings.agent_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent 功能已停用")
    return container.agent_service


AgentDep = Annotated[AgentService, Depends(get_agent)]


def _list_drives() -> list[str]:
    """Windows 盘符；非 Windows 返回空表（前端据此隐藏盘符行）。"""
    if os.name != "nt":
        return []
    return [
        f"{letter}:\\"
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if Path(f"{letter}:\\").exists()
    ]


def _quick_dirs() -> list[dict[str, str]]:
    """工作区选择器的常用位置：项目根、桌面、文档、下载（存在才给）。"""
    home = Path.home()
    items = (
        ("当前项目", PROJECT_ROOT),
        ("桌面", home / "Desktop"),
        ("文档", home / "Documents"),
        ("下载", home / "Downloads"),
    )
    return [{"name": name, "path": str(path)} for name, path in items if path.is_dir()]


def _resolve_dir(raw: str | None) -> Path:
    """把用户给的路径解析成真实目录（阻塞 IO，调用方放线程里跑）。"""
    candidate = Path(raw).expanduser() if raw else Path.home()
    return candidate.resolve(strict=True)


def _scan_subdirs(current: Path) -> list[dict[str, str]]:
    """只列子目录，不碰文件内容；权限不足的条目跳过而不是整页失败。"""
    out: list[dict[str, str]] = []
    with os.scandir(current) as entries:
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    out.append({"name": entry.name, "path": str(current / entry.name)})
            except OSError:
                continue
    out.sort(key=lambda item: item["name"].lower())
    return out[:_MAX_DIR_ENTRIES]


class StartPayload(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, max_length=128)
    workspace: str | None = Field(default=None, max_length=500)
    #: 权限档：default（界内写文件免批）/ ask（写与命令都批）/ full（都不批）
    permission: str | None = Field(default=None, max_length=16)
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


class PermissionPayload(BaseModel):
    permission: str = Field(min_length=1, max_length=16)


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


def _tool_knobs(tool_name: str) -> list[str]:
    """控制台「设置每一段」里该执行体能调的旋钮。

    codebuddy / qoder 暴露思考强度 + 上下文；codearts 26.11.1 只有
    ``--variant``（思考强度，见 run --help），没有上下文参数；
    其余（gemini 0.61 等）都没暴露。
    """
    if tool_name in {"codebuddy", "qoder"}:
        return ["effort", "context_window"]
    if tool_name == "codearts":
        return ["effort"]
    return []


@router.get("/executors")
async def list_executors(container: ContainerDep) -> dict[str, Any]:
    """可派的执行体花名册（控制台 @ / 面板的数据源）。"""
    registry = load_cli_registry(container.settings)
    items: list[dict[str, Any]] = []
    for role in registry.known_roles():
        rc = registry.roles[role]
        tool = registry.tools[rc.tool]
        # 清单来源分三级（见 docs/模型市场与预设.md）：
        #   probed = 真发请求验证过；live = CLI 自报；config = 配置快照。
        # 只有 probed 算数——另两级如实标注「未实测」，别让面板看起来像验证过。
        live = cached_models(tool.name, tool.command)
        models = live or tool.models or ([tool.model] if tool.model else [])
        # 兑换率只有桌面端缓存能给（CLI 的 --help / --list-models 都不带价格）。
        # 实测缓存热时这里是 probed 条目；冷时是桌面条目（probed=False，
        # rate 仍真实可用），前端据 probed 决定标「实测可用」还是「未实测」。
        details = {d["id"]: d for d in cached_model_details(tool.name, tool.command)}
        probed_ids = {mid for mid, d in details.items() if d.get("probed")}
        source = "probed" if (live and probed_ids & set(live)) else ("live" if live else "config")
        free = set(tool.free_models)
        items.append({
            "role": role,
            "tool": tool.name,
            "model": tool.model,
            "cost_tier": tool.cost_tier,
            "effort": tool.effort,
            "context_window": tool.context_window,
            "hint": rc.hint,
            # 各 CLI 暴露的旋钮不同，见 _tool_knobs；别让面板显示并不存在的参数
            "knobs": _tool_knobs(tool.name),
            "models_source": source,
            "models": [
                {
                    "id": model_id,
                    "free": model_id in free if free else tool.cost_tier == "free",
                    "probed": model_id in probed_ids,
                    **(
                        {
                            "rate": details[model_id]["rate"],
                            "tools": details[model_id]["tools"],
                            "images": details[model_id]["images"],
                            "max_input_tokens": details[model_id]["max_input_tokens"],
                        }
                        if model_id in details else {}
                    ),
                }
                for model_id in models
            ],
        })
    return {"executors": items}


@router.get("/executors/{role}/models")
async def executor_models(role: str, container: ContainerDep) -> dict[str, Any]:
    """该执行体**真正能调**的模型清单。

    对 WorkBuddy/CodeBuddy：会**真发一次探测请求**（每个候选 ``max_tokens=1``），
    只有 200 的才进结果。这是唯一可信来源——实测 2026-11-11：桌面缓存 65 个里
    39 个能调、26 个 400，``/v1/models`` 只列 14 个且有 8 个调不通、
    ``--help`` 的 26 个里 17 个不存在。三者都不准，只有实测算数。
    首次调用约 5 秒（并发 6），结果缓存 10 分钟。

    其它执行体走 CLI 自己的口径（``qoder --list-models`` / ``codebuddy --help``）。
    """
    registry = load_cli_registry(container.settings)
    config = registry.tool_for_role(role)
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"执行体角色 {role!r} 不可用")
    details = await discover_model_details(config.name, config.command)
    if details:
        return {
            "role": role,
            "source": "probed",
            "models": [d for d in details if d.get("probed")],
            # 调不通的单独列出来：运营者要能回答「hy4free 为什么不在列表里」
            "unavailable": [
                {"id": d["id"], "error": d.get("probe_error", "")}
                for d in details if not d.get("probed")
            ],
        }
    live = await asyncio.to_thread(discover_models, config.name, config.command)
    ids = live or config.models or [config.model]
    free = set(config.free_models)
    return {
        "role": role,
        "source": "live" if live else "config",
        "models": [{"id": model_id, "free": model_id in free, "rate": None} for model_id in ids],
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
    """建一个「执行体直跑」会话；结果通过既有 SSE 流回来。

    ⚠ 前端零调用（2026-11-10 审计）。控制台的「@ 执行体」走 ``/delegations``
    （可一次点多段、按顺序串行），这个单段入口是给**脚本 / 事件中的角色**
    用的写入面——``scripts/agent_worker.py`` 那条链路是它的消费者。
    保留而不是收进「高级」的理由：它不是给人点的按钮，是给 worker 调的接口。
    改它的 payload 形状前先 grep ``agent_worker.py``，别只看前端。
    """
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


#: 任务空间 id 的形状白名单。生成器产出的是 ``时间戳-短id-hex4``，全部落在
#: ``[A-Za-z0-9_.-]`` 内；这里收紧到同款字符集并显式拒绝 ``..``，因为
#: ``taskspace.load`` / ``force_release`` 内部是 ``root_for(workspace) / task_id``
#: 直拼路径——detail / release 两个端点（2026-11-08 审查 P1）曾把 URL 段
#: 原样递进去，``..%2F..%2F..`` 可以读任意 state.json、删任意 state.lock。
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$")


def _safe_task_id(task_id: str) -> str:
    if not _TASK_ID_RE.match(task_id) or ".." in task_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务空间不存在")
    return task_id


@router.get("/taskspaces/{task_id}")
async def taskspace_detail(task_id: str, container: ContainerDep) -> dict[str, Any]:
    """读一个任务空间的进度（步骤、产物、交接说明）。"""
    task_id = _safe_task_id(task_id)
    workspace = Path(container.settings.agent_workspace)
    data = taskspace.load(workspace, task_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务空间不存在或进度文件损坏")
    return {"taskspace": data}


@router.post("/taskspaces/{task_id}/release")
async def release_taskspace(task_id: str, container: ContainerDep) -> dict[str, Any]:
    """人工释放占用（执行体崩了但租约还没到期时用）。"""
    task_id = _safe_task_id(task_id)
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
    """重点模型画像：人工确认过的那几款 + 免费档，带证据等级。

    ⚠ 前端零调用（2026-11-10 审计）。它和 ``/model-inventory``（模型与价格页
    的数据源）在"模型画像"上有重叠，但**语义不同**：inventory 回答「现在能调
    到什么、什么价」（探测得到），catalog 回答「官方文档怎么说、实测证据等级
    是什么」（``config/model_catalog.yaml`` 里人工维护）。前者会随账号漂移，
    后者是稳定的知识底稿。
    保留：将来「动态选执行体」打分要用这份证据等级（见 docs/总控大脑…§5）。
    要合并的话先想清楚「探测值 vs 人工确认值谁覆盖谁」——它们冲突时后者才对。
    """
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
            # 并发跑，别一个个等；discover_* 自己会写 5 分钟缓存。
            await asyncio.gather(
                *(
                    asyncio.to_thread(discover_models, name, command)
                    for name, command in targets
                ),
                return_exceptions=True,
            )
            # 实测（兑换率 / 工具 / 上下文 / 到底能不能调）是另一份缓存。
            # 面板点「刷新实测」就是要重发探测请求，所以这里必须 force——
            # 否则 10 分钟内的旧快照会让运营者以为按钮没生效。
            wb_targets = [t for t in targets if t[0] in {"codebuddy", "workbuddy"}]
            for _name, _cmd in wb_targets:
                catalog, _src = await asyncio.to_thread(read_desktop_catalog)
                if catalog:
                    await probe_models([m["id"] for m in catalog], force=True)
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


@router.get("/fs/list")
async def list_dirs(path: str | None = None) -> dict[str, Any]:
    """工作区选择器用的目录浏览：只列子目录，不读文件内容。"""
    try:
        current = await asyncio.to_thread(_resolve_dir, path)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"目录不存在或无法访问：{path or '用户主目录'}",
        ) from exc
    if not current.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不是目录：{current}")
    try:
        dirs = await asyncio.to_thread(_scan_subdirs, current)
    except OSError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"目录读取失败：{exc}"
        ) from exc
    parent = current.parent if current.parent != current else None
    return {
        "current": str(current),
        "parent": str(parent) if parent is not None else None,
        "dirs": dirs,
        "drives": _list_drives(),
        "quick": _quick_dirs(),
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
                             permission=payload.permission,
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


@router.post("/sessions/{session_id}/permission")
async def set_permission(session_id: str, payload: PermissionPayload,
                         agent: AgentDep) -> dict[str, Any]:
    try:
        row = await agent.set_permission(session_id, payload.permission)
    except ZKAIError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return {"session": row}


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
