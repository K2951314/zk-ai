"""事件规则：``config/events.yaml`` 的解析、匹配与静默时段判断。

为什么规则用 YAML 而不是文章里的 ``rules.json``：
本项目的运维习惯是「改配置 → 自动生效」，``config/*.yaml`` 已经是这个形状。
这里没有接 ``ConfigWatcher``（它只盯 ``load_app_config`` 真正读过的那三个文件），
而是按 mtime 自己缓存 —— 规则文件很小，一次 stat + 偶尔一次 parse，
比把 watcher 的职责摊大到非 AppConfig 文件上更省事。

**匹配顺序就是文件里的顺序**，第一条命中即止。别改成「最具体的优先」：
运营者写规则时脑子里就是「从上往下」，隐式优先级只会让第 7 条为什么不生效
变成一道推理题。
"""

from __future__ import annotations

import datetime as dt
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.logging import get_logger

logger = get_logger("events.rules")

#: 没有规则接手的事件也留档（否则「为什么没触发」无从查起），
#: 但用更长的窗口去重，避免未匹配事件把表刷爆。
UNMATCHED_DEDUPE_SECONDS = 3600.0


@dataclass(slots=True)
class EventRule:
    """一条规则 = 「什么信号 -> 交给谁 -> 说什么」。"""

    name: str
    sources: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    #: payload 里的字段做 glob 匹配，全部满足才算命中
    match: dict[str, str] = field(default_factory=dict)
    #: 执行体角色（cli_tools.yaml 里的 role），不是模型名
    to: str = ""
    cwd: str = ""
    skill: str = ""
    ask: str = ""
    group: str = ""
    dedupe_window: float = 1800.0
    parallel: int = 1
    #: always = 每条都推；needed = 只有 need_decision 才推；never = 只落库
    tell: str = "needed"
    max_turns: int = 20
    budget_cny: float = 0.0
    #: 静默时段（"23:00-08:00"），静默期内只落库不推送
    silent: tuple[str, ...] = ()

    @property
    def group_name(self) -> str:
        return self.group or self.name

    def matches(self, source: str, event_type: str, payload: dict[str, Any]) -> bool:
        if self.sources and not any(fnmatch.fnmatchcase(source, p) for p in self.sources):
            return False
        if self.types and not any(fnmatch.fnmatchcase(event_type, p) for p in self.types):
            return False
        for key, pattern in self.match.items():
            actual = payload.get(key)
            if actual is None:
                return False
            if not fnmatch.fnmatchcase(_match_text(actual), pattern):
                return False
        return True


@dataclass(slots=True)
class RuleSet:
    """整个规则文件：规则列表 + 通知与全局默认。"""

    rules: list[EventRule] = field(default_factory=list)
    notify: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def match(self, source: str, event_type: str, payload: dict[str, Any]) -> EventRule | None:
        for rule in self.rules:
            if rule.matches(source, event_type, payload):
                return rule
        return None

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": r.name,
                "sources": list(r.sources),
                "types": list(r.types),
                "to": r.to,
                "group": r.group_name,
                "dedupe_window": r.dedupe_window,
                "parallel": r.parallel,
                "tell": r.tell,
            }
            for r in self.rules
        ]


class RuleLoader:
    """按 mtime 缓存地读 ``events.yaml``；文件坏了保留上一份好规则。"""

    def __init__(self, config_dir: Path) -> None:
        self._candidates = (
            config_dir / "events.yaml",
            config_dir / "events.yml",
        )
        self._cached: RuleSet | None = None
        self._signature: tuple[int, int] | None = None
        self._path: Path | None = None

    @property
    def path(self) -> Path | None:
        for candidate in self._candidates:
            if candidate.exists():
                return candidate
        return None

    def load(self) -> RuleSet:
        path = self.path
        if path is None:
            self._cached = self._cached or RuleSet(
                warnings=["未找到 config/events.yaml：所有事件都会以「无规则」落库"]
            )
            return self._cached
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return self._cached or RuleSet()
        if self._cached is not None and signature == self._signature:
            return self._cached
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            # 坏文件不能让中台停摆：沿用上一份能用的规则，并记录原因。
            logger.error("事件规则读取失败（沿用上一份）：%s", exc)
            if self._cached is not None:
                self._cached.warnings = [f"规则文件读取失败，正在用上一份：{exc}"]
                return self._cached
            return RuleSet(warnings=[f"规则文件读取失败：{exc}"])
        parsed = parse_ruleset(raw)
        self._cached = parsed
        self._signature = signature
        self._path = path
        return parsed


def parse_ruleset(raw: Any) -> RuleSet:
    """把 YAML 字典翻译成 :class:`RuleSet`。字段写错只警告，不抛。"""
    if not isinstance(raw, dict):
        return RuleSet(warnings=["events.yaml 顶层不是映射，已整体忽略"])
    warnings: list[str] = []
    rules: list[EventRule] = []
    raw_rules = raw.get("rules") or []
    if not isinstance(raw_rules, list):
        return RuleSet(warnings=["events.yaml 的 rules 不是列表，已整体忽略"])
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            warnings.append(f"第 {index + 1} 条规则不是映射，已跳过")
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            warnings.append(f"第 {index + 1} 条规则没有 name，已跳过")
            continue
        when = item.get("when") or {}
        if not isinstance(when, dict):
            when = {}
        match_map = when.get("match") or item.get("match") or {}
        if not isinstance(match_map, dict):
            match_map = {}
        to = str(item.get("to") or "").strip()
        if not to:
            warnings.append(f"规则 {name!r} 没有 to，永远不会被派单（只落库）")
        try:
            rule = EventRule(
                name=name,
                sources=_str_tuple(when.get("source") or when.get("sources")),
                types=_str_tuple(when.get("types") or when.get("type")),
                match={str(k): str(v) for k, v in match_map.items()},
                to=to,
                cwd=str(item.get("cwd") or ""),
                skill=str(item.get("skill") or ""),
                ask=str(item.get("ask") or ""),
                group=str(item.get("group") or ""),
                dedupe_window=float(item.get("dedupe_window", 1800) or 0),
                parallel=max(1, int(item.get("parallel", 1) or 1)),
                tell=str(item.get("tell") or "needed"),
                max_turns=max(1, int(item.get("max_turns", 20) or 20)),
                budget_cny=float(item.get("budget_cny", 0) or 0),
                silent=_str_tuple(item.get("silent")),
            )
        except (TypeError, ValueError) as exc:
            warnings.append(f"规则 {name!r} 字段类型不对，已跳过：{exc}")
            continue
        if rule.tell not in {"always", "needed", "never"}:
            warnings.append(f"规则 {name!r} 的 tell={rule.tell!r} 不认识，按 needed 处理")
            rule.tell = "needed"
        rules.append(rule)
    notify = raw.get("notify") if isinstance(raw.get("notify"), dict) else {}
    defaults = raw.get("defaults") if isinstance(raw.get("defaults"), dict) else {}
    return RuleSet(rules=rules, notify=dict(notify or {}), defaults=dict(defaults or {}), warnings=warnings)


def _match_text(value: Any) -> str:
    """把 payload 值转成用于 glob 比对的文本。

    布尔单独处理：``str(True)`` 是 ``"True"``，让运营者在 YAML 里写
    ``approved: "True"`` 才能命中——而所有人的直觉都是写 ``"true"``。
    这里把布尔统一成小写，两种写法都能命中。

    （2026-11-03：闭环规则的 ``match: {approved: ...}`` 依赖这个行为。）
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


# --------------------------------------------------------------------------- #
# 静默时段
# --------------------------------------------------------------------------- #
def parse_windows(specs: Any) -> list[tuple[dt.time, dt.time]]:
    """把 ``["23:00-08:00", ...]`` 解析成时间区间，坏格式静默丢弃。"""
    out: list[tuple[dt.time, dt.time]] = []
    for spec in _str_tuple(specs):
        head, _, tail = spec.partition("-")
        start = _parse_hhmm(head)
        end = _parse_hhmm(tail)
        if start is None or end is None:
            continue
        out.append((start, end))
    return out


def _parse_hhmm(text: str) -> dt.time | None:
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt.time(hour, minute)


def in_silent_window(windows: list[tuple[dt.time, dt.time]], now: dt.datetime) -> bool:
    """当前是否处于静默期。跨零点的区间（23:00-08:00）按「或」处理。"""
    current = now.time()
    for start, end in windows:
        if start <= end:
            if start <= current < end:
                return True
        elif current >= start or current < end:
            return True
    return False


__all__ = [
    "UNMATCHED_DEDUPE_SECONDS",
    "EventRule",
    "RuleLoader",
    "RuleSet",
    "in_silent_window",
    "parse_ruleset",
    "parse_windows",
]
