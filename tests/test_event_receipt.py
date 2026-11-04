"""回执契约 + 约时间复查（later）。

借自 agent-autoloop 的 worker/contract.ts：Agent 的结构化意图（要人拍板、
通知、结掉旧卡片、约时间再醒、派生下一件事）写在最后一条消息的代码块里，
中台照单执行。比「靠自然语言猜」可靠，也比一个布尔位的 [[NEED_DECISION]] 表达力强。

为什么 later 对本项目最要紧（2026-11-04）：刀具销售最常见的一条流水线是
「报价发出 → 3/7/15 天没回音 → 跟进」。没有「约个时间再醒」，Agent 只能一次跑完
然后等下次有人记得；有了它，事情留在库里、到点自己醒，等的时候不占进程不花 Token。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.database.db import Database
from app.database.repositories.event import EventRepository
from app.services.events.service import EventService, build_task, parse_receipt

RULES_YAML = """
rules:
  - name: 网关健康异常
    when:
      source: zkai-gateway
      types: [health.quarantined, "later.*"]
    to: cli-skill
    ask: 只读诊断。
    group: gateway-health
"""


@pytest.fixture
async def event_service(tmp_path: Path) -> AsyncIterator[EventService]:
    (tmp_path / "events.yaml").write_text(
        RULES_YAML, encoding="utf-8"
    )
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield EventService(
        settings=Settings(environment="test"),
        repository=EventRepository(database),
        config_dir=tmp_path,
    )
    await database.dispose()


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_a_well_formed_receipt_is_parsed_out_of_the_body() -> None:
    verdict = (
        "客户 24 小时没回，按跟进规则处理。\n\n"
        "```zkai\n"
        '{"ask":[{"title":"发不发跟进","text":"两条话术","options":["正式","简短"],'
        '"recommend":1,"key":"followup"}],'
        '"later":[{"key":"check-reply","at":"2026-11-07T09:00:00+08:00","about":"看他回没回"}],'
        '"notice":[{"title":"库存","text":"三号刀片只剩 2 盒"}]}\n'
        "```\n"
    )
    body, receipt = parse_receipt(verdict)
    assert "```zkai" not in body, "回执代码块不该留在结论正文里"
    assert body == "客户 24 小时没回，按跟进规则处理。"
    assert len(receipt.ask) == 1
    assert receipt.ask[0]["recommend"] == 1
    assert len(receipt.later) == 1
    assert receipt.later[0]["key"] == "check-reply"
    assert len(receipt.notice) == 1
    assert receipt.problems == []


def test_a_broken_receipt_never_loses_the_verdict() -> None:
    """回执写坏了：正文照用，问题写进 problems。不能让整份结论丢失。"""
    body, receipt = parse_receipt(
        "这是正经结论，得留下来。\n```zkai\n{不是合法 JSON}\n```"
    )
    assert "正经结论" in body, "回执坏了不能连正文一起丢"
    assert receipt.ask == []
    assert any("JSON" in p for p in receipt.problems)


def test_no_receipt_at_all_is_fine() -> None:
    body, receipt = parse_receipt("一切正常，没什么要做的。")
    assert body == "一切正常，没什么要做的。"
    assert receipt.wanted is False
    assert receipt.problems == []


def test_an_unknown_receipt_field_is_reported_not_fatal() -> None:
    """拼错一个键，不该带走同一份回执里写对的那些。"""
    _body, receipt = parse_receipt(
        "结论。\n```zkai\n{\"nate\":[1,2],\"ask\":[{\"title\":\"要不要\",\"options\":[\"是\",\"否\"]}]}\n```"
    )
    assert "nate" in "；".join(receipt.problems)
    assert len(receipt.ask) == 1


def test_the_last_receipt_block_wins() -> None:
    """模型有时会先示范一遍格式再给真的回执。取最后一个，跟 agent-autoloop 一致。"""
    verdict = (
        "结论。\n"
        '```zkai\n{"ask":[{"title":"示范","options":["A"]}]}\n```\n'
        '```zkai\n{"ask":[{"title":"真的","options":["B"]}]}\n```\n'
    )
    _body, receipt = parse_receipt(verdict)
    assert receipt.ask[-1]["title"] == "真的"


def test_parse_time_normalizes_to_utc() -> None:
    """回执里的 at 必须归一化到 UTC。

    2026-11-04 实测的坑：SQLite 的 DateTime 列**丢时区**，写进去
    ``03:48+00:00`` 读出来是 naive 的 ``03:48``。而仓库层比较都用
    ``utcnow()``（UTC）。如果按本地时间存（``11:48``），读出来当 ``11:48``
    去跟 ``utcnow()=03:48`` 比，就凭空晚了 8 小时，**复查永远不会到期**。
    """
    from app.services.events.service import _parse_time

    utc = _parse_time("2026-11-05T09:00:00+08:00")
    assert utc is not None
    assert utc.tzinfo is not None
    assert utc.utcoffset() == dt.timedelta(0)
    assert utc.hour == 1, "北京时间 9 点应当是 UTC 1 点"

    # Z 后缀同样要认
    z = _parse_time("2026-11-05T01:00:00Z")
    assert z == utc

    # 不带时区 = 本地时间，换算后仍是 UTC
    naive = _parse_time("2026-11-05T09:00:00")
    assert naive is not None
    assert naive.utcoffset() == dt.timedelta(0)
    assert naive == dt.datetime(2026, 11, 5, 9, 0).astimezone().astimezone(dt.UTC)

    assert _parse_time("不是时间") is None
    assert _parse_time("") is None


# --------------------------------------------------------------------------- #
# 指令里必须带上契约
# --------------------------------------------------------------------------- #
def test_the_dispatch_prompt_teaches_the_receipt_format() -> None:
    """不写进提示词等于没有 —— 采集器/执行体都不会自己知道有这个协议。"""
    from app.services.events.rules import EventRule

    rule = EventRule(name="t", to="cli-skill", ask="看看。", max_turns=5)
    task = build_task(rule, "zkai-gateway", "health.quarantined", "dep", "摘要", "", {})
    assert "```zkai" in task
    assert '"later"' in task
    assert "later 是「到点再醒我」" in task


# --------------------------------------------------------------------------- #
# 端到端：约了时间 -> 到点 -> 采集器 drain -> 变成一条待认领事件
# --------------------------------------------------------------------------- #
async def test_a_later_comes_back_as_an_event_when_due(event_service: EventService) -> None:
    created = await event_service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await event_service.claim(worker="w", wait_seconds=0)
    soon = (dt.datetime.now() - dt.timedelta(seconds=5)).isoformat(timespec="seconds")
    await event_service.complete(
        event_id,
        ok=True,
        verdict=(
            "已确认。\n```zkai\n"
            '{"later":[{"key":"check-reply","at":"' + soon + '","about":"看他回没回"}]}\n'
            "```"
        ),
    )

    due = await event_service.repository.take_due_laters()
    assert len(due) == 1, "到点的复查应该能取出来"
    assert due[0]["key"] == "check-reply"
    assert due[0]["event_id"] == event_id
    # 取走即消费：再取一次是空的
    assert await event_service.repository.take_due_laters() == []


async def test_a_later_in_the_future_is_not_due_yet(event_service: EventService) -> None:
    created = await event_service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await event_service.claim(worker="w", wait_seconds=0)
    await event_service.complete(
        event_id,
        ok=True,
        verdict=(
            "已确认。\n```zkai\n"
            '{"later":[{"key":"t-3d","at":"2099-01-01T09:00:00+08:00","about":"三天后问"}]}\n'
            "```"
        ),
    )
    assert await event_service.repository.take_due_laters() == []


async def test_a_receipt_event_derives_and_counts_as_one_more_hop(
    event_service: EventService,
) -> None:
    """回执里的 events = Agent 主动叫醒下一个 Agent，必须计入跳数。"""
    first = await event_service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "a"}
    )
    first_id = int(first["event"]["id"])
    await event_service.claim(worker="w", wait_seconds=0)
    await event_service.complete(
        first_id,
        ok=True,
        verdict=(
            "诊断完，转给下一环。\n```zkai\n"
            '{"events":[{"type":"health.quarantined","object":"b","summary":"接着查"}]}\n'
            "```"
        ),
    )
    events = await event_service.list(limit=20)
    derived = [e for e in events if e["object"] == "b"]
    assert len(derived) == 1
    assert derived[0]["parent_id"] == first_id
    assert derived[0]["hops"] == 1
