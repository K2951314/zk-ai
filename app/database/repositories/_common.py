"""Shared imports and logger for repository sub-modules.

Kept tiny on purpose: the heavy lifting (SQLAlchemy ORM, models, typing) is
re-exported here so each domain module only needs ``from ._common import *``
plus its own specialized symbols.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from sqlalchemy import CursorResult, case, delete, func, select, update

from app.core.logging import get_logger
from app.database.db import Database
from app.database.models import (
    AgentMessage,
    AgentSession,
    Credential,
    Deployment,
    EventRecord,
    HealthCheck,
    Model,
    ModelAlias,
    Provider,
    RequestAttempt,
    RequestRecord,
    UsageRecord,
    to_datetime,
    utcnow,
)
from app.models.credential import CredentialRuntime
from app.models.response import AttemptOutcome

logger = get_logger("database.repository")


def iso_utc(value: dt.datetime | None) -> str | None:
    """SQLite 读出 naive UTC；统一补 Z，避免前端按本地时区误显示。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "AgentMessage",
    "AgentSession",
    "Any",
    "AttemptOutcome",
    "Credential",
    "CredentialRuntime",
    "CursorResult",
    "Database",
    "Deployment",
    "EventRecord",
    "HealthCheck",
    "Model",
    "ModelAlias",
    "Provider",
    "RequestAttempt",
    "RequestRecord",
    "UsageRecord",
    "case",
    "cast",
    "delete",
    "func",
    "iso_utc",
    "logger",
    "select",
    "to_datetime",
    "update",
    "utcnow",
]
