"""事件中台：规则匹配、语义去重、认领/完结、静默时段、飞书签名。

不碰网络：通知推送只在 ``should_push`` / ``sign_feishu`` / ``build_card``
这几个纯函数上验证，真正发 HTTP 的那层留给线上跑。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.database.db import Database
from app.database.repositories.event import EventRepository
from app.services.events.notify import NotifyConfig, build_card, should_push, sign_feishu
from app.services.events.rules import EventRule, in_silent_window, parse_ruleset, parse_windows
from app.services.events.service import (
    DECISION_MARKER,
    EventError,
    EventService,
    build_task,
    strip_decision_marker,
)

RULES_YAML = """
notify:
  webhook: ""
  format: feishu
  silent: ["23:00-08:00"]
rules:
  - name: 网关健康异常
    when:
      source: zkai-gateway
      types: [health.quarantined, "rate_limit.*"]
    to: cli-claude
    skill: zkai-ops
    ask: 只读诊断。
    group: gateway-health
    dedupe_window: 1800
    parallel: 1
    tell: needed

  - name: 兜底健康
    when:
      source: zkai-gateway
    to: cli-grinder
    tell: never
"""


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[EventService]:
    (tmp_path / "events.yaml").write_text(RULES_YAML, encoding="utf-8")
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield EventService(
        settings=Settings(environment="test"),
        repository=EventRepository(database),
        config_dir=tmp_path,
    )
    await database.dispose()


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #
def test_rules_parse_and_keep_file_order() -> None:
    ruleset = parse_ruleset(
        {
            "rules": [
                {"name": "第一条", "when": {"source": "a", "types": ["x"]}, "to": "r1"},
                {"name": "第二条", "when": {"source": "a"}, "to": "r2"},
            ]
        }
    )
    assert [rule.name for rule in ruleset.rules] == ["第一条", "第二条"]
    # 两条都能匹配 a/x，第一条先命中 —— 顺序即优先级，别改成「最具体优先」
    matched = ruleset.match("a", "x", {})
    assert matched is not None
    assert matched.name == "第一条"


def test_rule_types_support_glob_and_payload_match() -> None:
    ruleset = parse_ruleset(
        {
            "rules": [
                {
                    "name": "限定环境",
                    "when": {"source": "gw", "types": ["pr.*"], "match": {"env": "prod*"}},
                    "to": "cli-claude",
                }
            ]
        }
    )
    assert ruleset.match("gw", "pr.opened", {"env": "prod-cn"}) is not None
    assert ruleset.match("gw", "pr.opened", {"env": "dev"}) is None
    assert ruleset.match("gw", "issue.opened", {"env": "prod"}) is None


def test_rule_without_to_still_matches_but_warns() -> None:
    ruleset = parse_ruleset({"rules": [{"name": "只落库", "when": {"source": "x"}}]})
    assert ruleset.rules[0].to == ""
    assert any("没有 to" in warning for warning in ruleset.warnings)


# --------------------------------------------------------------------------- #
# 入库 / 去重
# --------------------------------------------------------------------------- #
async def test_unmatched_event_is_kept_as_dropped(service: EventService) -> None:
    result = await service.ingest({"source": "未知来源", "type": "whatever"})
    assert result["matched"] is False
    assert result["event"]["state"] == "dropped"
    # 落库了 —— 否则「为什么这条没触发」无从查起
    assert await service.get(int(result["event"]["id"])) is not None


async def test_same_semantic_event_is_deduped_within_window(service: EventService) -> None:
    first = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "sensenova-03"}
    )
    second = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "sensenova-03"}
    )
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["event"]["id"] == first["event"]["id"]
    # 重复只加计数，不新建行
    assert second["event"]["payload"].get("dup_count") == 2


async def test_different_object_is_not_deduped(service: EventService) -> None:
    a = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "sensenova-03"}
    )
    b = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "sensenova-04"}
    )
    assert b["duplicate"] is False
    assert b["event"]["id"] != a["event"]["id"]


async def test_missing_required_field_is_rejected(service: EventService) -> None:
    with pytest.raises(EventError):
        await service.ingest({"source": "x"})


# --------------------------------------------------------------------------- #
# 认领 / 完结
# --------------------------------------------------------------------------- #
async def test_claim_then_complete_with_decision(service: EventService) -> None:
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "sensenova-03"}
    )
    event_id = int(created["event"]["id"])

    claimed = await service.claim(worker="test-worker", wait_seconds=0)
    assert claimed is not None
    assert claimed["id"] == event_id
    assert claimed["state"] == "running"
    assert claimed["attempts"] == 1

    done = await service.complete(
        event_id,
        ok=True,
        verdict=f"隔离原因是 tpm 打满。\n{DECISION_MARKER} yes",
        cost_cny=0.42,
    )
    assert done is not None
    assert done["state"] == "waiting_decision"
    assert done["need_decision"] is True
    assert DECISION_MARKER not in (done["verdict"] or "")
    assert done["cost_cny"] == pytest.approx(0.42)


async def test_claim_returns_none_when_queue_empty(service: EventService) -> None:
    assert await service.claim(worker="w", wait_seconds=0) is None


async def test_parallel_cap_queues_extra_events_instead_of_dropping(service: EventService) -> None:
    for index in range(3):
        await service.ingest(
            {"source": "zkai-gateway", "type": "health.quarantined", "object": f"dep-{index}"}
        )
    first = await service.claim(worker="w1", wait_seconds=0)
    assert first is not None
    # parallel=1：组内已有一条在跑，后面的必须排队而不是被丢掉
    assert await service.claim(worker="w2", wait_seconds=0) is None
    await service.complete(int(first["id"]), ok=True, verdict="处理完了")
    second = await service.claim(worker="w2", wait_seconds=0)
    assert second is not None
    assert second["id"] != first["id"]


async def test_failed_task_is_recorded_as_failed_and_keeps_error(service: EventService) -> None:
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await service.claim(worker="w", wait_seconds=0)
    done = await service.complete(event_id, ok=False, verdict="", error="执行体超时")
    assert done is not None
    assert done["state"] == "failed"
    assert "执行体超时" in (done["verdict"] or "")


async def test_human_decision_closes_the_event(service: EventService) -> None:
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await service.claim(worker="w", wait_seconds=0)
    await service.complete(event_id, ok=True, verdict=f"建议关掉消耗器。\n{DECISION_MARKER} yes")
    decided = await service.decide(event_id, approved=True, note="同意")
    assert decided is not None
    assert decided["state"] == "done"
    assert decided["need_decision"] is False
    assert "已批准" in (decided["verdict"] or "")


async def test_approval_redispatches_so_the_loop_actually_closes(
    service: EventService,
) -> None:
    """拍板之后必须派生一条可执行事件，否则审批只是终点。

    2026-11-03：原来的 decide() 只把「已批准」写进 verdict 就结束，
    于是「批准关服务」之后没有人去关，而运营者以为它关了。
    """
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    parent_id = int(created["event"]["id"])
    await service.claim(worker="w", wait_seconds=0)
    await service.complete(parent_id, ok=True, verdict=f"建议关掉消耗器。\n{DECISION_MARKER} yes")

    await service.decide(parent_id, approved=True, note="同意")

    # 子事件必须存在、且带着决策内容
    children = [
        e for e in await service.list(limit=50) if int(e["id"]) != parent_id
    ]
    assert len(children) == 1, "批准之后没有派生任何事件——审批是终点不是闭环"
    child = children[0]
    assert child["type"] == "health.quarantined.decided"
    assert child["payload"]["parent_id"] == parent_id
    assert child["payload"]["approved"] is True
    assert child["payload"]["note"] == "同意"


async def test_the_same_decision_twice_does_not_open_two_tasks(
    service: EventService,
) -> None:
    """重复拍板不产生第二条子事件，也不产生第二份活。

    2026-11-04 起口径变严：`decide` 只对 `waiting_decision` 生效，
    已结束的再点一次直接忽略（原来是靠子事件去重兜住的）。
    这样「一个批准被执行两次」在源头就不可能。
    """
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    parent_id = int(created["event"]["id"])
    await service.claim(worker="w", wait_seconds=0)
    await service.complete(parent_id, ok=True, verdict=f"要人拍板。\n{DECISION_MARKER} yes")

    await service.decide(parent_id, approved=True, note="同意")
    await service.decide(parent_id, approved=True, note="同意")
    approved_children = [
        e
        for e in await service.list(limit=50)
        if e["type"] == "health.quarantined.decided" and e["payload"]["approved"] is True
    ]
    assert len(approved_children) == 1, "重复拍板不该开出第二条子事件"

    # 改了主意也无法生效：父事件已经不是 waiting_decision 了。
    # 这是有意的——决策一旦落账，改主意要走一条新事件，不能悄悄改历史。
    await service.decide(parent_id, approved=False, note="等等，先别关")
    rejected = [
        e
        for e in await service.list(limit=50)
        if e["type"] == "health.quarantined.decided" and e["payload"]["approved"] is False
    ]
    assert rejected == [], "已结束的事件不该再派新活"


async def test_followup_can_be_turned_off_for_do_it_myself(
    service: EventService,
) -> None:
    """``followup=False`` = 只记账，不派活（这条我自己去办）。"""
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    parent_id = int(created["event"]["id"])
    await service.claim(worker="w", wait_seconds=0)
    await service.complete(parent_id, ok=True, verdict=f"要人拍板。\n{DECISION_MARKER} yes")

    await service.decide(parent_id, approved=True, note="我自己办", followup=False)
    children = [e for e in await service.list(limit=50) if int(e["id"]) != parent_id]
    assert children == []
    parent = await service.get(parent_id)
    assert parent is not None and parent["state"] == "done"


# --------------------------------------------------------------------------- #
# 超时回收与重试上限
# --------------------------------------------------------------------------- #
async def test_a_permanently_failing_event_is_judged_failed_not_requeued_forever(
    service: EventService,
) -> None:
    """超过重试上限的判 failed。

    2026-11-03：``reclaim_stale`` 原来无条件退回，``attempts`` 只增不读，
    一条永远失败的事件会每 2400s 重投一次、每投一次推一张失败卡片。
    设计文档 §8 写了「attempts 上限 3 次」，但一直没落地。
    """
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])

    # 模拟「被认领了 3 次都没交活」：attempts 已经到上限，claimed_at 很旧
    await service.claim(worker="w", wait_seconds=0)
    await service.claim(worker="w", wait_seconds=0)  # attempts=2（第一次没退回，直接再取）
    repo = service.repository
    # 直接把 attempts 顶到上限并伪造一个过期的 claimed_at
    async with repo.db.session() as session:
        from app.database.models import EventRecord

        row = await session.get(EventRecord, event_id)
        assert row is not None
        row.attempts = 3
        row.state = "running"
        row.claimed_at = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=2)
        await session.commit()

    reclaimed, dead = await repo.reclaim_stale(older_than_seconds=2400, max_attempts=3)
    assert dead == 1
    assert reclaimed == 0
    event = await service.get(event_id)
    assert event is not None
    assert event["state"] == "failed", "超过重试上限的事件必须停止重投"

    # 判死的事件不该再被认领
    assert await service.claim(worker="w", wait_seconds=0) is None


async def test_an_event_under_the_retry_limit_still_gets_requeued(
    service: EventService,
) -> None:
    """没超上限的还要正常退回 —— 别把上限做成「一次超时就判死」。"""
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await service.claim(worker="w", wait_seconds=0)
    repo = service.repository
    async with repo.db.session() as session:
        from app.database.models import EventRecord

        row = await session.get(EventRecord, event_id)
        assert row is not None
        row.claimed_at = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=2)
        await session.commit()

    reclaimed, dead = await repo.reclaim_stale(older_than_seconds=2400, max_attempts=3)
    assert reclaimed == 1
    assert dead == 0
    event = await service.get(event_id)
    assert event is not None and event["state"] == "received"


# --------------------------------------------------------------------------- #
# 决策标记
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("verdict", "expected_need", "expected_tail"),
    [
        (f"结论。\n{DECISION_MARKER} yes", True, "结论。"),
        (f"结论。\n{DECISION_MARKER} no", False, "结论。"),
        ("结论。", False, "结论。"),
        # 正文里提到这个串但没单独成行 —— 不当真
        (f"注意 {DECISION_MARKER} 这个标记的用法", False, f"注意 {DECISION_MARKER} 这个标记的用法"),
    ],
)
def test_decision_marker_parsing(verdict: str, expected_need: bool, expected_tail: str) -> None:
    cleaned, need = strip_decision_marker(verdict)
    assert need is expected_need
    assert cleaned == expected_tail


def test_build_task_embeds_context_and_the_decision_contract() -> None:
    rule = EventRule(name="t", to="cli-claude", skill="zkai-ops", ask="只读诊断。")
    task = build_task(rule, "zkai-gateway", "health.quarantined", "dep-1", "摘要", None, {"a": 1})
    assert "zkai-ops" in task
    assert "只读诊断。" in task
    assert "dep-1" in task
    assert DECISION_MARKER in task


# --------------------------------------------------------------------------- #
# 静默时段 / 推送判断
# --------------------------------------------------------------------------- #
def test_silent_window_crossing_midnight() -> None:
    windows = parse_windows(["23:00-08:00"])
    assert in_silent_window(windows, dt.datetime(2026, 11, 2, 23, 30))
    assert in_silent_window(windows, dt.datetime(2026, 11, 2, 3, 0))
    assert not in_silent_window(windows, dt.datetime(2026, 11, 2, 12, 0))


def test_silent_window_same_day() -> None:
    windows = parse_windows(["09:00-12:00"])
    assert in_silent_window(windows, dt.datetime(2026, 11, 2, 10, 0))
    assert not in_silent_window(windows, dt.datetime(2026, 11, 2, 12, 0))


def test_bad_silent_spec_is_ignored_rather_than_crashing() -> None:
    assert parse_windows(["25:00-08:00", "乱写", "08:00"]) == []


def test_push_is_skipped_when_not_configured() -> None:
    push, reason = should_push(
        tell="always", need_decision=True, config=NotifyConfig(), rule=None
    )
    assert push is False
    assert "webhook" in reason


def test_push_needed_only_when_decision_required() -> None:
    config = NotifyConfig(webhooks=["https://example.invalid/hook"])
    assert should_push(tell="needed", need_decision=False, config=config, rule=None)[0] is False
    assert should_push(tell="needed", need_decision=True, config=config, rule=None)[0] is True
    assert should_push(tell="never", need_decision=True, config=config, rule=None)[0] is False


def test_push_respects_silent_window() -> None:
    config = NotifyConfig(webhooks=["https://example.invalid/hook"], silent=parse_windows(["23:00-08:00"]))
    night = dt.datetime(2026, 11, 2, 23, 30)
    push, reason = should_push(
        tell="needed", need_decision=True, config=config, rule=None, now=night
    )
    assert push is False
    assert "静默" in reason


# --------------------------------------------------------------------------- #
# 飞书
# --------------------------------------------------------------------------- #
def test_feishu_signature_follows_the_documented_algorithm() -> None:
    import base64
    import hashlib
    import hmac

    timestamp = "1700000000"
    secret = "test-secret"
    expected = base64.b64encode(
        hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    ).decode()
    assert sign_feishu(timestamp, secret) == expected
    # 没开签名校验时不该硬塞一个空 sign
    assert sign_feishu(timestamp, "") == ""


def test_card_marks_decisions_and_links_back() -> None:
    card = build_card(
        {
            "id": 7,
            "summary": "3 个部署被自动隔离",
            "source": "zkai-gateway",
            "type": "health.quarantined",
            "rule": "网关健康异常",
            "role": "cli-claude",
            "object": "sensenova-03",
            "verdict": "tpm 打满",
            "need_decision": True,
            "cost_cny": 0.42,
            "link": "https://example.invalid/health",
        },
        console_base="https://zkai.example.com",
    )
    header = card["header"]
    assert header["title"]["content"] == "需要你拍板"
    assert header["template"] == "orange"
    buttons = [el for el in card["elements"] if el.get("tag") == "action"]
    assert buttons and buttons[0]["actions"][0]["url"] == "https://example.invalid/health"
    assert any("¥0.4200" in str(el) for el in card["elements"])


# --------------------------------------------------------------------------- #
# 跳数熔断（TTL）
# --------------------------------------------------------------------------- #
async def test_a_derived_event_inherits_the_depth_of_its_parent(
    service: EventService,
) -> None:
    """派生事件的深度 = 父事件 + 1，且由中台算，不认事件体里写的 hops。"""
    first = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "a"}
    )
    first_id = int(first["event"]["id"])
    assert first["event"]["hops"] == 0

    second = await service.ingest(
        {
            "source": "zkai-gateway",
            "type": "health.quarantined",
            "object": "b",
            # 冒充深度 0 —— 必须被无视
            "hops": 0,
            "parent_id": first_id,
        }
    )
    assert second["event"]["hops"] == 1
    assert second["event"]["parent_id"] == first_id


async def test_a_forged_parent_id_cannot_reset_the_depth(
    service: EventService,
) -> None:
    """不存在的 parent_id 当作没给，深度从 0 起 —— 但不能因此被「赦免」。"""
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "x", "parent_id": 999_999}
    )
    assert created["event"]["hops"] == 0
    assert created["event"]["parent_id"] is None


async def test_a_chain_deeper_than_the_limit_is_recorded_but_not_dispatched(
    service: EventService,
) -> None:
    """超过 8 层：落库为 failed，**不进入待认领队列**，并如实写上原因。

    借自 agent-autoloop 的 TTL：A 叫醒 B、B 叫醒 A 会造成静默烧钱，
    而每一跳看起来都「正常成功」，日志里查不出异常。
    """
    parent_id: int | None = None
    last = None
    # 0..8 共 9 条正常，第 10 条 hops=9 该被拦
    for _ in range(10):
        body = {"source": "zkai-gateway", "type": "health.quarantined", "object": "chain"}
        if parent_id is not None:
            body["parent_id"] = parent_id
        last = await service.ingest(body)
        parent_id = int(last["event"]["id"])

    assert last is not None
    event = last["event"]
    assert event["hops"] == 9
    assert event["state"] == "failed", "超限事件不该进入待认领队列"
    assert "派生深度" in (event["verdict"] or "")
    assert "上限" in (event["verdict"] or "")

    # 队列里不该有它
    claimed = await service.claim(worker="w", wait_seconds=0)
    assert claimed is None or claimed["id"] != event["id"]


async def test_approval_also_counts_as_one_more_hop(
    service: EventService,
) -> None:
    """人工拍板不清零跳数 —— 否则「批准→执行体又发事件→再批准」能无限绕。"""
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    parent_id = int(created["event"]["id"])
    await service.claim(worker="w", wait_seconds=0)
    await service.complete(parent_id, ok=True, verdict=f"要拍板。\n{DECISION_MARKER} yes")
    await service.decide(parent_id, approved=True, note="同意")

    child = next(
        e for e in await service.list(limit=50) if e["type"] == "health.quarantined.decided"
    )
    assert child["hops"] == 1
    assert child["parent_id"] == parent_id


# --------------------------------------------------------------------------- #
# 交活幂等
# --------------------------------------------------------------------------- #
async def test_delivering_the_same_result_twice_does_not_wipe_the_cost(
    service: EventService,
) -> None:
    """worker 交活后网断了会重交同一份结果。重交不能把成本清零、不能重复推送。

    2026-11-04：原来的 finish 无条件覆盖 —— 重交会把结论盖掉、
    ``cost_tokens`` 清零、``ended_at`` 刷新，看起来像又跑了一遍。
    """
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await service.claim(worker="w", wait_seconds=0)
    await service.complete(event_id, ok=True, verdict="查完了，一切正常。", cost_tokens=1234, cost_cny=0.5)

    again = await service.complete(
        event_id, ok=True, verdict="查完了，一切正常。", cost_tokens=0, cost_cny=0.0
    )
    assert again is not None
    assert again["cost_tokens"] == 1234, "重交不能把成本清零"
    assert again["cost_cny"] == 0.5
    assert again["state"] == "done"


async def test_a_second_different_result_cannot_overwrite_the_first(
    service: EventService,
) -> None:
    """两次不同的执行抢同一件事：不覆盖，保留第一份，让运营者看得见。"""
    created = await service.ingest(
        {"source": "zkai-gateway", "type": "health.quarantined", "object": "dep"}
    )
    event_id = int(created["event"]["id"])
    await service.claim(worker="w", wait_seconds=0)
    await service.complete(event_id, ok=True, verdict="第一份结论。")

    again = await service.complete(event_id, ok=False, verdict="第二份不同的结论。", error="撞车了")
    assert again is not None
    assert "第一份结论" in (again["verdict"] or "")
    assert again["state"] == "done", "不能因为第二份结果把已完成的事件改成失败"
