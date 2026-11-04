"""Backward-compatibility shim.

All repository classes now live in :mod:`app.database.repositories` (split by
domain: config / request / usage / health / agent). This module re-exports them
so existing callers — ``from app.database.repository import ConfigRepository`` —
keep working unchanged. New code should import from
``app.database.repositories`` directly.
"""

from __future__ import annotations

from app.database.repositories import (
    AgentRepository,
    ConfigRepository,
    EventRepository,
    HealthRepository,
    RequestRepository,
    UsageRepository,
)

__all__ = [
    "AgentRepository",
    "ConfigRepository",
    "EventRepository",
    "HealthRepository",
    "RequestRepository",
    "UsageRepository",
]
