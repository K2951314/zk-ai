"""Shared imports and logger for repository sub-modules.

Kept tiny on purpose: the heavy lifting (SQLAlchemy ORM, models, typing) is
re-exported here so each domain module only needs ``from ._common import *``
plus its own specialized symbols.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, case, delete, func, select, update

from app.core.logging import get_logger
from app.database.db import Database
from app.database.models import (
    AgentMessage,
    AgentSession,
    Credential,
    Deployment,
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
    "logger",
    "select",
    "to_datetime",
    "update",
    "utcnow",
]
