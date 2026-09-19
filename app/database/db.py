"""Async SQLAlchemy engine / session management.

SQLite is the default (``sqlite+aiosqlite:///./data/zkai.db``). The URL is
overridable through ``ZKAI_DATABASE_URL`` so the same code runs on PostgreSQL by
swapping the driver - no repository changes required.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.logging import get_logger
from app.database.models import Base

logger = get_logger("database")


class Database:
    """Owns the engine and session factory."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self._ensure_sqlite_dir()
        connect_args: dict = {}
        if url.startswith("sqlite"):
            # aiosqlite + a single writer; allow cross-thread usage.
            connect_args = {"check_same_thread": False, "timeout": 30}
        self.engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            future=True,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        if url.startswith("sqlite"):
            self._install_sqlite_pragmas()
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False
        )
        # Serializes whole transactions (see :meth:`session`).
        self._tx_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    def _ensure_sqlite_dir(self) -> None:
        prefix = "sqlite+aiosqlite:///"
        if not self.url.startswith(prefix):
            return
        raw = self.url[len(prefix) :]
        if raw in {":memory:", ""}:
            return
        path = Path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)

    def _install_sqlite_pragmas(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    # ------------------------------------------------------------------ #
    async def init(self) -> None:
        """Create the schema if it does not exist yet."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info("database ready (%s)", self._safe_url())

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional session scope.

        Sessions are serialized with a process-wide lock. The async engine
        funnels every session through a single pooled aiosqlite connection
        (SQLite is single-writer anyway), and two sessions overlapping on that
        connection corrupt each other's implicit transaction — observed as
        silently lost UPDATEs and spurious "no transaction is active" commits
        when the ZK-Agent loop wrote status rows while a console poller read
        them. Contention is a non-issue at personal-gateway scale.
        """
        async with self._tx_lock:
            session = self.session_factory()
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    def _safe_url(self) -> str:
        """URL without credentials embedded (PostgreSQL URLs may contain them)."""
        if "@" in self.url:
            scheme, _, rest = self.url.partition("://")
            _, _, host = rest.rpartition("@")
            return f"{scheme}://***@{host}"
        return self.url

    async def health(self) -> bool:
        """Cheap connectivity probe used by ``/health``."""
        from sqlalchemy import text

        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.exception("database health check failed")
            return False
