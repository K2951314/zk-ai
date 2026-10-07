"""Repository package: all database access lives here.

Every repository takes the :class:`~app.database.db.Database` and opens a short
transactional session per operation. Nothing in this package knows about
credentials: only ``credential_id`` strings and non-reversible fingerprints are
ever persisted.

The sub-modules are split by domain (config / requests / usage / health / agent).
This ``__init__`` re-exports every repository class so callers can keep using
``from app.database.repository import ConfigRepository`` (the ``repository``
module is now a thin shim that forwards to this package).
"""

from __future__ import annotations

from app.database.repositories.agent import AgentRepository
from app.database.repositories.config import ConfigRepository
from app.database.repositories.health import HealthRepository
from app.database.repositories.request import RequestRepository
from app.database.repositories.usage import UsageRepository

__all__ = [
    "AgentRepository",
    "ConfigRepository",
    "HealthRepository",
    "RequestRepository",
    "UsageRepository",
]
