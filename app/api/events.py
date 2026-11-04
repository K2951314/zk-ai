"""事件中台 API：``/zkadmin/events``。

两条链路共用这套接口：

* **信号侧**：任何东西（脚本、webhook 接收器、其他系统）往 ``POST /zkadmin/events``
  丢一条事件，中台负责匹配规则、去重、落库。
* **执行侧**：本机 worker 用 ``POST /zkadmin/events/claim`` 长轮询取活，
  干完用 ``POST /zkadmin/events/{id}/complete`` 交结论。

长轮询放在这里而不是让中台反向推：worker 跑在 NAT 后面的本机上，
中台在公网服务器上 —— 只能由内网侧主动拉。这也是文章里「接单程序」同款思路。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, require_admin
from app.core.logging import get_logger
from app.services.events.service import EventError, EventService

logger = get_logger("events.api")

router = APIRouter(
    prefix="/zkadmin/events",
    tags=["events"],
    dependencies=[Depends(require_admin)],
)


def get_events(container: ContainerDep) -> EventService:
    if not container.settings.events_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "事件中台已停用")
    return container.event_service


EventsDep = Annotated[EventService, Depends(get_events)]


class EventIn(BaseModel):
    """一条事件的统一格式。字段刻意少：来源侧不该为了发事件去读文档。"""

    source: str = Field(min_length=1, max_length=64, description="来源标识，如 zkai-gateway")
    type: str = Field(min_length=1, max_length=64, description="事件类型，如 health.quarantined")
    object: str = Field(default="", max_length=200, description="受影响对象")
    summary: str = Field(default="", max_length=500, description="一句话摘要")
    link: str = Field(default="", max_length=1000, description="跳回原处的链接")
    payload: dict[str, Any] = Field(default_factory=dict, description="原始附加信息")
    #: 派生它的那条事件。**跳数由中台计算**，不接受请求里的 hops —— 否则
    #: 执行体可以自称深度 0，TTL 就形同虚设。
    parent_id: int | None = Field(default=None, ge=1, description="派生它的父事件编号")


class ClaimPayload(BaseModel):
    worker: str = Field(min_length=1, max_length=64, description="worker 实例名")
    wait: float = Field(default=50.0, ge=0, le=120, description="长轮询等待秒数")


class CompletePayload(BaseModel):
    ok: bool = True
    verdict: str = Field(default="", max_length=100_000)
    cost_tokens: int = Field(default=0, ge=0)
    cost_cny: float = Field(default=0.0, ge=0)
    session_id: str | None = Field(default=None, max_length=32)
    error: str | None = Field(default=None, max_length=4000)


class DecisionPayload(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=2000)
    #: 是否把决策变成一条派回执行体的子事件（默认开）。
    #: 关掉 = 只记账不派活，用于「这条我自己去办」的场景。
    followup: bool = True


@router.post("", status_code=201)
async def ingest(payload: EventIn, events: EventsDep) -> dict[str, Any]:
    """收一条事件。重复事件不会新建行，返回 ``duplicate=true``。"""
    try:
        return await events.ingest(payload.model_dump())
    except EventError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("")
async def list_events(
    events: EventsDep,
    state: str | None = Query(default=None, max_length=24),
    source: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    items = await events.list(state=state, source=source, limit=limit, offset=offset)
    return {"events": items}


@router.get("/stats")
async def stats(events: EventsDep, days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
    """看板：按状态/来源/规则聚合，附带当前规则清单与告警。"""
    return await events.stats(days=days)


@router.get("/rules")
async def list_rules(events: EventsDep) -> dict[str, Any]:
    ruleset = events.rules()
    return {"rules": ruleset.describe(), "warnings": ruleset.warnings}


@router.post("/claim")
async def claim(payload: ClaimPayload, events: EventsDep) -> dict[str, Any]:
    """worker 长轮询取活。没有活时等到 ``wait`` 秒后返回 ``event: null``。"""
    event = await events.claim(worker=payload.worker, wait_seconds=payload.wait)
    return {"event": event}


@router.post("/{event_id}/complete")
async def complete(event_id: int, payload: CompletePayload, events: EventsDep) -> dict[str, Any]:
    updated = await events.complete(
        event_id,
        ok=payload.ok,
        verdict=payload.verdict,
        cost_tokens=payload.cost_tokens,
        cost_cny=payload.cost_cny,
        session_id=payload.session_id,
        error=payload.error,
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "事件不存在")
    return {"event": updated}


@router.post("/later/drain")
async def drain_laters(events: EventsDep, source: str = Query(default="zkai-gateway")) -> dict[str, Any]:
    """取走到点的复查（回执 later），转成标准事件并派单。

    由采集器 ``event_watch.py`` clone polling 调用。**取走即消费**：
    这一步在中台里做，是因为要在一个事务里「查到期的 → 标 fired → 建事件」，
    采集器那边分两步做会丢（查到之后进程崩，那几条就永远 pending 着没人发）。
    """
    due = await events.repository.take_due_laters()
    created: list[int] = []
    for item in due:
        body = {
            "source": source,
            "type": "later.due",
            "object": item["key"],
            "summary": f"复查时间到了：{item['key']}",
            "link": "",
            "payload": {"later_id": item["id"], "about": item.get("about") or ""},
            "parent_id": item["event_id"],
        }
        try:
            result = await events.ingest(body)
        except EventError as exc:
            logger.warning("复查 #%s 转事件失败：%s", item["id"], exc)
            continue
        created.append(result["event"]["id"])
    return {"due": len(due), "created": created}


@router.post("/{event_id}/decision")
async def decide(event_id: int, payload: DecisionPayload, events: EventsDep) -> dict[str, Any]:
    """拍板。默认会把决策派成一条子事件，让执行体接着干（闭环）。"""
    updated = await events.decide(
        event_id, approved=payload.approved, note=payload.note, followup=payload.followup
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "事件不存在")
    return {"event": updated}


@router.get("/{event_id}")
async def get_event(event_id: int, events: EventsDep) -> dict[str, Any]:
    event = await events.get(event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "事件不存在")
    return {"event": event}


__all__ = ["router"]
