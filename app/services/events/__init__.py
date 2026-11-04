"""事件中台：信号 -> 规则 -> 派单 -> 结论 -> 沉淀。"""

from __future__ import annotations

from app.services.events.notify import (
    NotifyConfig,
    build_card,
    build_notify_config,
    push_event,
    should_push,
    sign_feishu,
)
from app.services.events.rules import (
    EventRule,
    RuleLoader,
    RuleSet,
    in_silent_window,
    parse_ruleset,
    parse_windows,
)
from app.services.events.service import (
    DECISION_MARKER,
    RECEIPT_MARKER,
    EventError,
    EventService,
    Receipt,
    build_task,
    parse_receipt,
    strip_decision_marker,
)

__all__ = [
    "DECISION_MARKER",
    "RECEIPT_MARKER",
    "EventError",
    "EventRule",
    "EventService",
    "NotifyConfig",
    "Receipt",
    "RuleLoader",
    "RuleSet",
    "build_card",
    "build_notify_config",
    "build_task",
    "in_silent_window",
    "parse_receipt",
    "parse_ruleset",
    "parse_windows",
    "push_event",
    "should_push",
    "sign_feishu",
    "strip_decision_marker",
]
