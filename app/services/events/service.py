"""EventService：事件中台的大脑。

一条事件的生命周期：

``ingest``  → 规范化 → 匹配规则 → 语义去重 → 落库(received/dropped)
``claim``   → worker 长轮询取走（running）
``complete``→ 写结论 → 按 tell/静默时段决定是否推送

三条不能省的设计：

1. **未命中规则的事件也落库（state=dropped）**。不落库的话，
   「为什么这条没触发」就变成一句无法回答的话——而这是运营者第一个会问的问题。
2. **去重按规则窗口**，不是全局窗口。网关健康异常 30 分钟内只该开一条任务，
   而 PR 事件可能要 1 小时。窗口是规则的属性。
3. **结论里带机器可读的决策标记**（``[[NEED_DECISION]] yes``）。
   靠自然语言猜「这句话算不算要人拍板」是不可靠的，
   而这个判断决定了要不要半夜弹你一条卡片。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config.settings import Settings
from app.core.logging import get_logger
from app.database.repositories._common import utcnow
from app.database.repositories.event import EventRepository
from app.services.events.notify import build_notify_config, push_event, should_push
from app.services.events.rules import UNMATCHED_DEDUPE_SECONDS, EventRule, RuleLoader, RuleSet

logger = get_logger("events.service")

#: 结论里的决策标记。worker 侧也认这个常量，别各写一份。
DECISION_MARKER = "[[NEED_DECISION]]"

#: 回执代码块的语言标记（Agent 在最后一条消息末尾附一个 ```zkai {...}``` 代码块）
RECEIPT_MARKER = "zkai"

#: payload 进 prompt 时的截断长度（防止上游把整个日志文件塞进来）
_PAYLOAD_PROMPT_LIMIT = 2000

#: 认领轮询间隔（秒）
_CLAIM_POLL = 0.5

#: 超时认领回收的检查间隔（秒）
_RECLAIM_EVERY = 30.0

#: 已经走到头的事件状态：再交活只做幂等比对，不再覆盖。
#: ``waiting_decision`` 也算终态 —— 它的结论就是「等你拍板」，不该被重交冲掉。
_TERMINAL_STATES = frozenset({"done", "failed", "waiting_decision"})


class EventError(Exception):
    """事件参数不合法（API 层翻成 400）。"""


class EventService:
    """事件入库、认领、完结与推送。"""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: EventRepository,
        config_dir: Path,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self._loader = RuleLoader(config_dir)
        self._last_reclaim = 0.0

    # ------------------------------------------------------------------ #
    # 规则
    # ------------------------------------------------------------------ #
    def rules(self) -> RuleSet:
        return self._loader.load()

    def max_hops(self) -> int:
        """派生链的最大深度（借自网络数据包 TTL）。

        为什么需要（2026-11-04）：审批闭环接上以后，规则可以把事件派回执行体，
        而执行体又能发新事件 —— A 叫醒 B、B 叫醒 A 会静默烧钱，而且
        每一跳看起来都「正常成功」，日志里查不出异常。
        """
        return int(getattr(self.settings, "events_max_hops", 8) or 8)

    def notify_config(self):
        base = ""
        try:
            base = self.settings.public_base_url_effective
        except Exception:  # pragma: no cover - 配置缺失时不影响推送本身
            base = ""
        return build_notify_config(self.rules(), console_base=base)

    # ------------------------------------------------------------------ #
    # 入库
    # ------------------------------------------------------------------ #
    async def ingest(self, raw: dict[str, Any]) -> dict[str, Any]:
        """收一条事件。返回 ``{event, duplicate, matched, rule}``。"""
        source = _require(raw, "source", 64)
        event_type = _require(raw, "type", 64)
        obj = str(raw.get("object") or "")[:200]
        summary = str(raw.get("summary") or obj or event_type)[:500]
        link = str(raw.get("link") or "")[:1000] or None
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        ruleset = self.rules()
        rule = ruleset.match(source, event_type, payload)
        window = rule.dedupe_window if rule is not None else UNMATCHED_DEDUPE_SECONDS
        dedupe_key = _dedupe_key(source, event_type, obj, payload)

        # 派生深度由**中台**算，事件体里写的 hops 一律不认（执行体不能自己赦免自己）。
        # 只有 ``parent_id`` 指向一条真实存在的父事件时才 +1；找不到父事件就从 0 起，
        # 否则一个乱填的 parent_id 就能把深度重置回 0。
        parent_id = _positive_int(raw.get("parent_id"))
        hops = 0
        if parent_id is not None:
            parent = await self.repository.get(parent_id)
            if parent is None:
                parent_id = None
            else:
                hops = int(parent.get("hops") or 0) + 1
                # 派生事件必须能穿透去重。
                # 否则「同一对象」的链路（A 诊断完派回同一条规则）会在 30 分钟内
                # 被上一条吃掉，hops 永远停在 0 —— 熔断形同虚设。
                # 链路上的重复由跳数上限兜底，不靠这个窗口。
                dedupe_key = hashlib.sha256(
                    f"{dedupe_key}|hop|{parent_id}|{hops}".encode()
                ).hexdigest()[:40]

        existing = await self.repository.find_recent(dedupe_key, within_seconds=window)
        if existing is not None:
            merged = await self.repository.bump_duplicate(int(existing["id"])) or existing
            logger.info(
                "事件重复（%ss 窗口内）：%s/%s %s -> #%s",
                int(window),
                source,
                event_type,
                obj,
                existing["id"],
            )
            return {
                "event": merged,
                "duplicate": True,
                "matched": rule is not None,
                "rule": rule.name if rule else None,
            }

        fields: dict[str, Any] = {
            "source": source,
            "type": event_type,
            "object": obj,
            "summary": summary,
            "link": link,
            "payload": payload,
            "dedupe_key": dedupe_key,
            "state": "dropped" if rule is None else "received",
            "rule": rule.name if rule else None,
            "group_name": rule.group_name if rule else None,
            "parallel": rule.parallel if rule else 1,
            "role": (rule.to or None) if rule else None,
            "cwd": (rule.cwd or None) if rule else None,
            "task": build_task(rule, source, event_type, obj, summary, link, payload) if rule else None,
            "hops": hops,
            "parent_id": parent_id,
        }
        # 超过跳数上限：**记下来但不派单**。静默丢弃比这更糟——
        # 「记了没派」至少事后能查清环是从哪断的。
        over_limit = hops > self.max_hops()
        if over_limit:
            fields["state"] = "failed"
            fields["verdict"] = (
                f"派生深度 {hops} 超过上限 {self.max_hops()}，已停止派发。"
                "这条链路可能形成了 Agent 互相叫醒的环，检查规则里的 emits/decided 事件。"
            )
            fields["ended_at"] = utcnow()
            logger.error("事件 %s/%s 派生深度 %s 超限，只记录不派发", source, event_type, hops)
        created = await self.repository.create(fields)
        if over_limit:
            if rule is not None:
                # 超限是异常，必须让人看见（tell 强制 always）
                await self._push(created, rule, tell="always")
            return {
                "event": created,
                "duplicate": False,
                "matched": rule is not None,
                "rule": rule.name if rule else None,
            }
        if rule is None:
            logger.info("事件未命中任何规则（已落库待查）：%s/%s", source, event_type)
        else:
            logger.info("事件 #%s 命中规则 %r，待 %s 认领", created["id"], rule.name, rule.to)
            if rule.tell == "always":
                await self._push(created, rule)
        return {
            "event": created,
            "duplicate": False,
            "matched": rule is not None,
            "rule": rule.name if rule else None,
        }

    # ------------------------------------------------------------------ #
    # 认领 / 完结
    # ------------------------------------------------------------------ #
    async def claim(self, *, worker: str, wait_seconds: float = 50.0) -> dict[str, Any] | None:
        """长轮询取一条待办；``wait_seconds`` 内没有就返回 None。

        注意这里**不持有数据库事务地等待**：``Database.session()`` 是进程级串行锁，
        拿着它 sleep 会把整个网关的写操作一起堵住。所以是「短事务取一次 → 放锁 → sleep」。
        """
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            await self._maybe_reclaim()
            event = await self.repository.claim(worker=worker)
            if event is not None:
                logger.info("事件 #%s 被 %s 认领（规则 %s）", event["id"], worker, event["rule"])
                return event
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_CLAIM_POLL)

    async def _maybe_reclaim(self) -> None:
        now = time.monotonic()
        if now - self._last_reclaim < _RECLAIM_EVERY:
            return
        self._last_reclaim = now
        # 任务超时上限取自执行体 timeout 的上界（1800s）+ 缓冲；
        # 短了会误伤正常长任务，长了会让卡死的事件占着坑。
        timeout = float(getattr(self.settings, "events_claim_timeout", 2400.0))
        max_attempts = int(getattr(self.settings, "events_max_attempts", 3) or 3)
        try:
            reclaimed, dead = await self.repository.reclaim_stale(
                older_than_seconds=timeout, max_attempts=max_attempts
            )
        except Exception:  # pragma: no cover - 回收失败不能挡住认领
            logger.exception("回收超时认领失败")
            return
        if reclaimed:
            logger.warning("%d 条事件认领超时，已退回待认领", reclaimed)
        if dead:
            # 判死要喊出来：这些是「试了 N 次都不行」的，运营者必须知道
            logger.error("%d 条事件超过重试上限 %d 次，已判失败", dead, max_attempts)

    async def complete(
        self,
        event_id: int,
        *,
        ok: bool = True,
        verdict: str = "",
        cost_tokens: int = 0,
        cost_cny: float = 0.0,
        session_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        """worker 交活。写结论、判决策、按需推送。

        **幂等**（2026-11-04，借自 agent-autoloop 的 finish）：
        worker 交活成功后网断了，它会重交同一份结果。原来的 ``finish`` 无条件覆盖 ——
        重交会把已有结论盖掉、把 ``cost_tokens`` 清零、刷新 ``ended_at``，
        看起来像又重新跑了一遍。这里先比对：已结束 + 结论相同 = 同一次交活，
        只回一声，不再重复写、不再重复推送。

        结论不同则**不覆盖**（那是两次不同的执行在抢同一件事，该拒绝），
        返回当前状态并记 WARNING，交给运营者判断。
        """
        current = await self.repository.get(event_id)
        if current is None:
            return None
        stripped, need_decision = strip_decision_marker(verdict)
        # 回执里的意图优先于那个布尔位：写了 ``ask`` 就是想让人拍板，
        # 不用再指望模型记得同时输出 ``[[NEED_DECISION]] yes``。
        cleaned, receipt = parse_receipt(stripped)
        if receipt.ask:
            need_decision = need_decision or ok
        if current["state"] in _TERMINAL_STATES:
            if (current.get("verdict") or "") == (cleaned or "") and (current.get("attempts") or 0) > 0:
                logger.info("事件 #%s 重复交活（结论相同），已忽略", event_id)
                return current
            logger.warning(
                "事件 #%s 已经结束（%s），又收到一份不同结论，已拒绝覆盖",
                event_id,
                current["state"],
            )
            return current
        state = "waiting_decision" if (ok and need_decision) else ("done" if ok else "failed")
        updated = await self.repository.finish(
            event_id,
            state=state,
            verdict=cleaned or None,
            need_decision=need_decision,
            cost_tokens=cost_tokens,
            cost_cny=cost_cny,
            session_id=session_id,
            error=error,
        )
        if updated is None:
            return None
        if ok:
            await self._apply_receipt(updated, receipt)
        rule = self._rule_by_name(updated.get("rule"))
        tell = rule.tell if rule is not None else "needed"
        if not ok:
            # 失败一定要让人看见，否则「自动化悄悄坏了」比没有自动化更糟。
            tell = "always" if tell == "needed" else tell
        await self._push(updated, rule, tell=tell, need_decision=need_decision or not ok)
        return await self.repository.get(event_id)

    async def _apply_receipt(self, event: dict[str, Any], receipt: Receipt) -> None:
        """把回执里的意图落成事实。

        只有 later 与 events 在这里执行（一条是排期、一条是派生，
        不排/不派就永远丢了）；ask / notice / resolve 是给人看的，
        随卡片展示，由 decide 那一侧处理。
        """
        for item in receipt.later:
            at = _parse_time(item.get("at"))
            key = str(item.get("key") or "").strip()
            if not key:
                receipt.problems.append("later 项缺少 key，已跳过")
                continue
            if item.get("cancel"):
                await self.repository.cancel_later(event["id"], key)
                continue
            if at is None:
                receipt.problems.append(f"later 项 {key!r} 的 at 不是合法时间，已跳过")
                continue
            await self.repository.schedule_later(event["id"], key, at, str(item.get("about") or ""))
        # 派生事件：Agent 干完活觉得该接着办下一件。**先限额再派**——
        # 这是 A 叫醒 B 的那条腿，跳数熔断就是在这里生效的。
        for item in receipt.events:
            event_type = str(item.get("type") or "").strip()
            if not event_type:
                receipt.problems.append("events 项缺少 type，已跳过")
                continue
            try:
                await self.ingest(
                    {
                        "source": str(item.get("source") or event.get("source") or "manual"),
                        "type": event_type,
                        "object": str(item.get("object") or ""),
                        "summary": str(item.get("summary") or ""),
                        "link": str(item.get("link") or ""),
                        "payload": item.get("payload") if isinstance(item.get("payload"), dict) else {},
                        "parent_id": event["id"],
                    }
                )
            except EventError as exc:
                receipt.problems.append(f"派生事件 {event_type!r} 失败：{exc}")
        if receipt.problems:
            logger.warning("事件 #%s 的回执有问题：%s", event["id"], "；".join(receipt.problems))

    async def decide(
        self,
        event_id: int,
        *,
        approved: bool,
        note: str = "",
        followup: bool = True,
    ) -> dict[str, Any] | None:
        """人在控制台/飞书拍板后的落账，并把决策变回一条**可执行**的事件。

        为什么要有后半段（2026-11-03）：原来的 ``decide`` 只把
        「【人工决策】已批准」追加到 verdict、把状态改成 done 就结束——
        审批是**终点**而不是闭环。后果是「你批准关服务，没有人去关」，
        而你以为它关了。文章里那句「点完以后，Agent 就收到我的决策了，
        继续自己干活了」，指的正是这里缺的这一环。

        实现刻意**不新建机制**：把决策当成一次普通 ``ingest``，
        类型加 ``.decided`` 后缀，沿用现有规则引擎重新匹配派单。
        规则作者只要给同一条流水线补一条
        ``types: [xxx.decided]`` 的规则，闭环就成立了；
        没补规则时子事件会以 ``dropped`` 落库，一眼看得出「还没配闭环」。
        """
        current = await self.repository.get(event_id)
        if current is None:
            return None
        if current["state"] != "waiting_decision":
            # 只有「等你拍板」的事件才拍得了。已结束的再点一次是重复操作，
            # 不能当新决定派一遍活 —— 那等于一个批准被执行两次。
            logger.info("事件 #%s 当前是 %s，不等待拍板，忽略这次决策", event_id, current["state"])
            return current
        verdict = str(current.get("verdict") or "")
        stamp = "已批准" if approved else "已否决"
        suffix = f"\n\n【人工决策】{stamp}" + (f"：{note}" if note else "")
        await self.repository.finish(
            event_id,
            state="done",
            verdict=(verdict + suffix).strip(),
            need_decision=False,
            cost_tokens=int(current.get("cost_tokens") or 0),
            cost_cny=float(current.get("cost_cny") or 0.0),
        )
        if followup:
            await self._dispatch_followup(current, approved=approved, note=note)
        return await self.repository.get(event_id)

    async def _dispatch_followup(
        self, parent: dict[str, Any], *, approved: bool, note: str
    ) -> None:
        """把拍板结果变成子事件，让它重新走一遍规则引擎。

        子事件的去重键里带**决策内容**（批准/否决 + note），
        所以「同一个决定点两次」不会开两条任务，但「先否决、后批准」
        会是两条——这正是想要的语义。
        """
        parent_type = str(parent.get("type") or "")
        verdict = str(parent.get("verdict") or "").strip()
        decision_word = "approved" if approved else "rejected"
        summary = f"（{'已批准' if approved else '已否决'}）{parent.get('summary') or parent_type}"
        payload: dict[str, Any] = {
            "parent_id": parent.get("id"),
            "approved": approved,
            "note": note,
            "verdict": verdict[:4000],
            # 指纹带决策内容：重复点同一个决定会被去重，改了主意则是新事件
            "fingerprint": f"decided|{parent.get('id')}|{decision_word}|{note}",
        }
        try:
            await self.ingest(
                {
                    "source": str(parent.get("source") or "manual"),
                    "type": f"{parent_type}.decided",
                    "object": str(parent.get("object") or ""),
                    "summary": summary,
                    "link": str(parent.get("link") or ""),
                    "payload": payload,
                    # 派生深度：人工拍板**也**会让链再长一跳。
                    # 不清零（否则「批准→执行体又发事件→再批准」能无限绕），
                    # 与 agent-autoloop 的 hops 语义一致。
                    "parent_id": parent.get("id"),
                }
            )
        except EventError as exc:
            # 子事件建不出来不能让决策本身丢失：父事件已经记了「已批准」
            logger.warning("决策已记录，但派生执行事件失败：%s", exc)

    async def _push(
        self,
        event: dict[str, Any],
        rule: EventRule | None,
        *,
        tell: str | None = None,
        need_decision: bool | None = None,
    ) -> bool:
        config = self.notify_config()
        effective_tell = tell if tell is not None else (rule.tell if rule else "needed")
        effective_need = (
            need_decision if need_decision is not None else bool(event.get("need_decision"))
        )
        push, reason = should_push(
            tell=effective_tell,
            need_decision=effective_need,
            config=config,
            rule=rule,
        )
        if not push:
            logger.debug("事件 #%s 不推送：%s", event.get("id"), reason)
            return False
        sent = await push_event(event, config)
        if sent:
            await self.repository.mark_notified(int(event["id"]))
        return sent

    def _rule_by_name(self, name: Any) -> EventRule | None:
        if not name:
            return None
        for rule in self.rules().rules:
            if rule.name == name:
                return rule
        return None

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    async def list(
        self,
        *,
        state: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return await self.repository.list(state=state, source=source, limit=limit, offset=offset)

    async def get(self, event_id: int) -> dict[str, Any] | None:
        return await self.repository.get(event_id)

    async def stats(self, *, days: int = 7) -> dict[str, Any]:
        data = await self.repository.stats(days=days)
        ruleset = self.rules()
        data["rules"] = ruleset.describe()
        data["warnings"] = ruleset.warnings
        return data


# --------------------------------------------------------------------------- #
# 纯函数（好测，且 worker 侧复用）
# --------------------------------------------------------------------------- #
def _require(raw: dict[str, Any], key: str, limit: int) -> str:
    value = str(raw.get(key) or "").strip()
    if not value:
        raise EventError(f"事件缺少必填字段 {key}")
    if len(value) > limit:
        raise EventError(f"字段 {key} 过长（上限 {limit} 字符）")
    return value


def _positive_int(value: Any) -> int | None:
    """``parent_id`` 这类可选的整数引用：不是正整数就当作没给。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse_time(value: Any) -> dt.datetime | None:
    """把回执里的 ``at`` 解析成 **UTC** 的 aware datetime。

    为什么必须归一化到 UTC（2026-11-04 实测）：SQLite 的 DateTime 列**丢时区**
    —— 写进去 ``2026-11-04 03:48+00:00``，读出来是 naive 的 ``03:48``。
    而仓库层所有比较用的都是 ``utcnow()``（UTC），所以库里存的必须是
    UTC 的实际时刻，绝不能是本地时间：存本地 ``11:48`` 读出来当 ``11:48``，
    跟 ``utcnow()=03:48`` 一比就凭空晚了 8 小时，复查永远不会到期。

    Agent 写不带时区的「明天9点」时，按**本机时区**理解（那才是人的直觉），
    再换算成 UTC 存。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 没写时区 = 本地时间
        parsed = parsed.astimezone()
    return parsed.astimezone(dt.UTC)


def _dedupe_key(source: str, event_type: str, obj: str, payload: dict[str, Any]) -> str:
    """语义去重键。

    优先用来源自己给的 ``fingerprint``（最准）；没给就用 object；
    再没有才退到 summary。**不用整个 payload** —— 时间戳/随机 id 会让
    同一条事件每次都算出不同的键，等于没去重。
    """
    fingerprint = payload.get("fingerprint") or obj or payload.get("summary") or ""
    raw = f"{source}|{event_type}|{obj}|{fingerprint}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def build_task(
    rule: EventRule,
    source: str,
    event_type: str,
    obj: str,
    summary: str,
    link: str | None,
    payload: dict[str, Any],
) -> str:
    """把规则里的 ``ask`` 展开成一条自带上下文的完整指令。"""
    parts: list[str] = []
    if rule.skill:
        parts.append(f"按 skill `{rule.skill}` 的口径处理。")
    if rule.ask:
        parts.append(rule.ask.strip())
    context = [
        "【事件上下文】",
        f"来源：{source}",
        f"类型：{event_type}",
    ]
    if obj:
        context.append(f"对象：{obj}")
    context.append(f"摘要：{summary}")
    if link:
        context.append(f"链接：{link}")
    if payload:
        dumped = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(dumped) > _PAYLOAD_PROMPT_LIMIT:
            dumped = dumped[:_PAYLOAD_PROMPT_LIMIT] + "\n…（已截断）"
        context.append("附加信息：\n```json\n" + dumped + "\n```")
    parts.append("\n".join(context))
    parts.append(
        "要求：\n"
        "1. 先说结论，再说依据；不要复述事件本身。\n"
        f"2. 最后单独一行输出 `{DECISION_MARKER} yes` 或 `{DECISION_MARKER} no`，"
        "表示这件事是否需要人来拍板。\n"
        f"3. 轮次上限 {rule.max_turns}；只读诊断类任务不要改任何配置或数据。"
    )
    parts.append(_RECEIPT_SPEC)
    return "\n\n".join(parts)


#: 回执格式说明。**每次派单都带上**——它是 Agent 和本系统之间唯一的结构化契约，
#: 不写在提示词里等于没有。刻意写成单行 JSON 示例，避免模型照抄出带缩进的坏格式。
_RECEIPT_SPEC = (
    "【回执】需要本系统中台替你做点事时，在最后一条消息末尾附一个标记为 "
    + RECEIPT_MARKER
    + " 的代码块，里面是一个 JSON 对象，用不到的字段不写；什么都不需要就不附。\n"
    "```"
    + RECEIPT_MARKER
    + "\n"
    '{"ask":[{"title":"一句话说要决定什么","text":"三行以内","options":["选项一","选项二"],'
    '"recommend":0,"about":"留给接着干的自己：背景、每个选项具体怎么做","key":"这件事的固定名字"}],'
    '"notice":[{"title":"一句话","text":"不是他亲自做不可的事：要他做什么"}],'
    '"resolve":[{"card":12,"reason":"在哪核实了已经解决"}],'
    '"later":[{"key":"这次复查的固定名字","at":"带时区的时间，比如 2026-11-05T09:00:00+08:00",'
    '"about":"到时候查什么、怎么算通过"}],'
    '"events":[{"source":"zkai-gateway","type":"下一件事的类型","object":"对象","summary":"一句话"}]}\n'
    "```\n"
    "- ask 和 notice 会打断人，能不发就不发。写给不看代码、也看不到这一轮对话的人。\n"
    "- later 是「到点再醒我」：约了时间，事件还在库里，但到点前不占进程、不花 Token。"
    " 同一个 key 再约一次等于改期（不是多排一条）。\n"
    "- 需要人拍板的事，光在正文里说是不够的——必须写进 ask，否则没人会看到通知。"
)


def strip_decision_marker(verdict: str) -> tuple[str, bool]:
    """摘掉 ``[[NEED_DECISION]]`` 标记并返回是否要人拍板。

    标记必须单独成行；正文里出现这个串（比如模型在解释这个机制）不当真。
    """
    need = False
    kept: list[str] = []
    for line in (verdict or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(DECISION_MARKER):
            tail = stripped[len(DECISION_MARKER) :].strip().lower()
            need = tail.startswith("y") or tail in {"1", "true", "是"}
            continue
        kept.append(line)
    return "\n".join(kept).strip(), need


# --------------------------------------------------------------------------- #
# 回执契约（借自 agent-autoloop：结构化意图比一句话可靠）
# --------------------------------------------------------------------------- #
#: 回执代码块的语言标记。Agent 在最后一条消息末尾附一个：
#:
#:     ```zkai
#:     {"ask": [...], "notice": [...], "resolve": [...], "later": [...], "events": [...]}
#:     ```
#:
#: 为什么不只靠 ``[[NEED_DECISION]]``：一个布尔位只能表达「要不要人」，
#: 表达不了「要人做什么选择」「这件事该多久后再核实」。靠自然语言再解析一遍
#: 是不可靠的——这几个字段全部是**会触发实际动作**的（开卡片、排明天、
#: 再派一单活），猜错就是乱叫醒人或者无限烧钱。
_RECEIPT_BLOCK = re.compile(r"```" + RECEIPT_MARKER + r"[ \t]*\r?\n(.*?)```", re.S | re.I)
#: 回执里认的键。多出来的直接忽略（而不是整份丢弃）——一个拼错的键
#: 不该带走整份结论，但要让它可见。
_RECEIPT_FIELDS = ("ask", "notice", "resolve", "later", "events")


@dataclass(slots=True)
class Receipt:
    """一份回执里中台会做的事。全部为空 = Agent 觉得不用麻烦人。"""

    #: 要人选的选项：[{"title","text","options","recommend","key"}]
    ask: list[dict[str, Any]] = field(default_factory=list)
    #: 只通知不选择：[{"title","text"}]
    notice: list[dict[str, Any]] = field(default_factory=list)
    #: 结掉已有的卡片：[{"card": 12, "reason": "在哪核实了"}]
    resolve: list[dict[str, Any]] = field(default_factory=list)
    #: 约个时间再醒：[{"key","at"(ISO 时间),"about","cancel"}]
    later: list[dict[str, Any]] = field(default_factory=list)
    #: 再派生事件：[{"source","type","object","summary"}]
    events: list[dict[str, Any]] = field(default_factory=list)
    #: 回执里不认识 / 写错的键。不丢弃整份回执，但要让人看见
    problems: list[str] = field(default_factory=list)

    @property
    def wanted(self) -> bool:
        return bool(self.ask or self.notice or self.resolve or self.later or self.events)


def parse_receipt(verdict: str) -> tuple[str, Receipt]:
    """从结论里取出回执代码块，返回 ``(去掉回执后的正文, 回执)``。

    **容错优先**：回执写坏了不让整份结论丢失——正文照用，问题写进
    ``receipt.problems`` 供卡片展示。Agent 输出不可控，解析器必须比它宽容：
    多一个键、少一个字段、JSON 里带注释，都只降级不抛异常。
    """
    text = verdict or ""
    receipt = Receipt()
    kept = text
    for match in _RECEIPT_BLOCK.finditer(text):
        kept = kept.replace(match.group(0), "")
        raw = (match.group(1) or "").strip()
        try:
            data = json.loads(raw)
        except ValueError as exc:
            receipt.problems.append(f"回执不是合法 JSON：{exc}")
            continue
        if not isinstance(data, dict):
            receipt.problems.append("回执要是一个 JSON 对象")
            continue
        for key, value in data.items():
            if key not in _RECEIPT_FIELDS:
                receipt.problems.append(f"不认识的回执字段 {key!r}")
                continue
            if value is None:
                continue
            items = value if isinstance(value, list) else [value]
            cleaned = [item for item in items if isinstance(item, dict)]
            if len(cleaned) != len(items):
                receipt.problems.append(f"回执字段 {key!r} 里有非对象项，已跳过")
            getattr(receipt, key).extend(cleaned)
    return kept.strip(), receipt


__all__ = [
    "DECISION_MARKER",
    "RECEIPT_MARKER",
    "EventError",
    "EventService",
    "Receipt",
    "build_task",
    "parse_receipt",
    "strip_decision_marker",
]
