"""通知可靠性层回归（2026-11-08 审查 P1 的根修）。

三件事，对应报告里的三个发现：

1. **回执进卡片**——Agent 按提示词把要拍板的问题写进 ``ask``，但 verdict
   落库前被剥掉回执块，卡片只渲染正文，人收得到「要拍板」收不到「拍什么」。
2. **静默只延迟，不吞**——静默窗口挡下的 need_decision / failed 卡片留在库里
   （``notified=False``），补推扫描在窗口过后送出；原来没有任何补发路径，
   23:30 的审批卡会永远沉底。
3. **open 口径**——``waiting_decision`` 也是待办，看板不许假装没有这件事。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.database.db import Database
from app.database.repositories.event import EventRepository
from app.services.events.notify import NotifyConfig, build_card, should_push
from app.services.events.rules import parse_windows
from app.services.events.service import EventService

RULES_YAML = """
notify:
  webhook: "https://feishu.example/hook"
  # 真实窗口（全覆盖）只为了让 should_push 的静默分支参与判断；
  # 测试里静默与否统一由 push_recorder 夹具 patch in_silent_window 控制，
  # 不依赖测试运行的真实时刻。
  silent: ["00:00-23:59"]
rules:
  - name: 网关健康异常
    when:
      source: zkai-gateway
      types: [health.quarantined]
    to: cli-skill
    ask: 只读诊断。
    group: gateway-health
"""

ASK_RECEIPT = (
    "诊断完成，需要你选方案。\n\n```zkai\n"
    '{"ask":[{"title":"怎么处理被隔离的部署","text":"两个可选动作",'
    '"options":["下线该部署","保留观察"],"recommend":0,"key":"dep-处置"}]}\n'
    "```"
)


@pytest.fixture
async def event_service(tmp_path: Path) -> AsyncIterator[EventService]:
    (tmp_path / "events.yaml").write_text(RULES_YAML, encoding="utf-8")
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield EventService(
        settings=Settings(environment="test"),
        repository=EventRepository(database),
        config_dir=tmp_path,
    )
    await database.dispose()


@pytest.fixture
def push_recorder(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """拦截真正的飞书推送，记录被推的事件 id。

    默认「不在静默时段」（推送畅通）；要模拟静默的测试自己把
    ``in_silent_window`` 再 patch 成 True。不拦的话 complete() 会真的
    往 example.invalid 发 HTTP，慢且脏。
    """
    import app.services.events.notify as notify_module
    import app.services.events.service as service_module

    sent: list[int] = []

    async def fake_push(event, config, *, now=None):
        sent.append(int(event["id"]))
        return True

    monkeypatch.setattr(service_module, "push_event", fake_push)
    monkeypatch.setattr(notify_module, "in_silent_window", lambda *a, **k: False)
    return sent


# --------------------------------------------------------------------------- #
# 回执进卡片
# --------------------------------------------------------------------------- #
async def test_completed_receipt_lands_in_payload_and_on_the_card(
    event_service: EventService,
    push_recorder: list[int],
) -> None:
    created = await event_service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await event_service.claim(worker="w", wait_seconds=0)
    await event_service.complete(event_id, ok=True, verdict=ASK_RECEIPT)

    event = await event_service.get(event_id)
    assert event is not None
    assert event["state"] == "waiting_decision", "写了 ask 就该等人拍板"
    receipt = (event.get("payload") or {}).get("receipt")
    assert receipt and receipt["ask"], "回执的 ask 必须落进 payload，卡片才有的画"

    card = build_card(event, console_base="https://zk.example")
    content = str(card["elements"])
    assert "要你拍板" in content
    assert "怎么处理被隔离的部署" in content
    assert "下线该部署" in content
    assert "（✅ 推荐）" in content, "recommend 标记要看得见，否则推荐了等于没推荐"


async def test_receipt_problems_are_visible_on_the_card(
    event_service: EventService,
    push_recorder: list[int],
) -> None:
    """回执写坏了不让它无声无息——问题要跟着卡片出来。"""
    created = await event_service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await event_service.claim(worker="w", wait_seconds=0)
    await event_service.complete(
        event_id,
        ok=True,
        verdict="结论。\n```zkai\n{\"oops\": 1}\n```",
    )
    event = await event_service.get(event_id)
    assert event is not None
    receipt = (event.get("payload") or {}).get("receipt")
    assert receipt and receipt["problems"], "不认识的字段要进 problems"
    card = build_card(event)
    assert "回执问题" in str(card["elements"])


async def test_card_deeplinks_the_session_when_present(
    event_service: EventService,
    push_recorder: list[int],
) -> None:
    """交活带了 session_id，卡片按钮就该直达那个会话（agent.html 早支持 ?session=）。"""
    created = await event_service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await event_service.claim(worker="w", wait_seconds=0)
    await event_service.complete(
        event_id, ok=True, verdict="完了", session_id="sess-abc"
    )
    event = await event_service.get(event_id)
    assert event is not None
    card = build_card(event, console_base="https://zk.example")
    urls = [
        action.get("url")
        for element in card["elements"]
        if element.get("tag") == "action"
        for action in element.get("actions", [])
    ]
    assert any(str(url).endswith("/ui/agent?session=sess-abc") for url in urls)


# --------------------------------------------------------------------------- #
# 静默只延迟，不吞
# --------------------------------------------------------------------------- #
async def test_silent_window_only_delays_the_decision_card(
    event_service: EventService,
    monkeypatch: pytest.MonkeyPatch,
    push_recorder: list[int],
) -> None:
    """静默期间推送被挡（notified=False），窗口过后补推扫描把它送出去。"""
    import app.services.events.notify as notify_module

    sent = push_recorder

    # 永远在静默时段（夹具默认 False，这里临时翻转）
    monkeypatch.setattr(notify_module, "in_silent_window", lambda *a, **k: True)
    created = await event_service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await event_service.claim(worker="w", wait_seconds=0)
    await event_service.complete(event_id, ok=True, verdict=ASK_RECEIPT)
    assert sent == [], "静默时段不该把卡片推出去"

    event = await event_service.get(event_id)
    assert event is not None and event["notified"] is False

    # 天亮了：补推扫描把欠账送出去
    monkeypatch.setattr(notify_module, "in_silent_window", lambda *a, **k: False)
    event_service._last_push_sweep = 0.0
    await event_service._maybe_push_pending()
    assert sent == [event_id], "静默结束后的第一次扫描就该补推"

    event = await event_service.get(event_id)
    assert event is not None and event["notified"] is True


async def test_sweep_picks_up_failed_events_never_delivered(
    event_service: EventService,
    monkeypatch: pytest.MonkeyPatch,
    push_recorder: list[int],
) -> None:
    """失败通知（含判死）同样进补推范围——原来只有一行日志。"""
    import app.services.events.notify as notify_module

    sent = push_recorder
    monkeypatch.setattr(notify_module, "in_silent_window", lambda *a, **k: True)

    created = await event_service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await event_service.claim(worker="w", wait_seconds=0)
    await event_service.complete(event_id, ok=False, error="上游连不上")
    assert sent == []

    monkeypatch.setattr(notify_module, "in_silent_window", lambda *a, **k: False)
    event_service._last_push_sweep = 0.0
    await event_service._maybe_push_pending()
    assert sent == [event_id], "失败卡片必须在静默结束后补出去"


async def test_sweep_never_repeats_after_delivery(
    event_service: EventService,
    push_recorder: list[int],
) -> None:
    """送达过一次就不再推：notified 标记 + 节流双保险。"""
    sent = push_recorder
    created = await event_service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await event_service.claim(worker="w", wait_seconds=0)
    await event_service.complete(event_id, ok=True, verdict=ASK_RECEIPT)

    event_service._last_push_sweep = 0.0
    await event_service._maybe_push_pending()
    event_service._last_push_sweep = 0.0
    await event_service._maybe_push_pending()
    assert sent.count(event_id) == 1, "补推是 at-least-once 的欠账回收，不是每分钟重发"


# --------------------------------------------------------------------------- #
# wake_on_decision：拍板卡片可豁免静默（运营者开关，默认关）
# --------------------------------------------------------------------------- #
def test_wake_on_decision_breaks_silence_only_for_decisions() -> None:
    config = NotifyConfig(
        webhooks=["https://feishu.example/hook"],
        silent=parse_windows(["23:00-08:00"]),
        wake_on_decision=True,
    )
    night = dt.datetime(2026, 11, 8, 23, 30)
    day = dt.datetime(2026, 11, 8, 12, 0)
    # 拍板卡片：半夜也响
    push, _ = should_push(
        tell="always", need_decision=True, config=config, rule=None, now=night
    )
    assert push is True
    # 普通通知：照样等早上
    push, reason = should_push(
        tell="always", need_decision=False, config=config, rule=None, now=night
    )
    assert push is False and "静默" in reason
    # 白天一切照旧
    push, _ = should_push(
        tell="needed", need_decision=True, config=config, rule=None, now=day
    )
    assert push is True


def test_silent_still_blocks_without_the_switch() -> None:
    """默认（不开豁免）保持原语义：半夜拍板卡片也等到早上。"""
    config = NotifyConfig(
        webhooks=["https://feishu.example/hook"],
        silent=parse_windows(["23:00-08:00"]),
    )
    push, reason = should_push(
        tell="always",
        need_decision=True,
        config=config,
        rule=None,
        now=dt.datetime(2026, 11, 8, 23, 30),
    )
    assert push is False and "静默" in reason


# --------------------------------------------------------------------------- #
# open 口径
# --------------------------------------------------------------------------- #
async def test_open_count_includes_waiting_decision(
    event_service: EventService,
) -> None:
    """看板的「待处理」必须包含欠你拍板的事件——否则 open=0 不代表没事。"""
    created = await event_service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await event_service.claim(worker="w", wait_seconds=0)
    await event_service.complete(event_id, ok=True, verdict=ASK_RECEIPT)

    stats = await event_service.stats()
    assert stats["open"] == 1, "waiting_decision 也是待办"
    assert stats["by_state"].get("waiting_decision") == 1
