"""AgentRepository: ZK-Agent sessions and their transcript rows (LLM replay history).

Concurrency note: the agent loop, console pollers and SSE snapshots hit these
rows simultaneously. Transactions are serialized process-wide by
``Database.session()`` (one pooled aiosqlite connection per engine, where
overlapping sessions corrupt each other's implicit transaction), so no
per-repository locking is needed here.
"""

from __future__ import annotations

from app.database.repositories._common import (
    AgentMessage,
    AgentSession,
    Any,
    Database,
    func,
    select,
)


class AgentRepository:
    """ZK-Agent sessions and transcript rows; see module docstring for concurrency."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_session(
        self,
        *,
        session_id: str,
        title: str,
        model: str,
        workspace: str,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                AgentSession(id=session_id, title=title, model=model, workspace=workspace)
            )
            await session.commit()

    async def list_sessions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(AgentSession).order_by(AgentSession.updated_at.desc()).limit(limit)
            )
            return [self._session_dict(row) for row in result.scalars()]

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.get(AgentSession, session_id)
            return self._session_dict(row) if row else None

    async def update_status(
        self,
        session_id: str,
        status: str,
        *,
        error: str | None = None,
        steps: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        title: str | None = None,
    ) -> None:
        async with self.db.session() as session:
            row = await session.get(AgentSession, session_id)
            if row is None:
                return
            row.status = status
            if error is not None:
                row.error = error
            if steps is not None:
                row.steps = steps
            if input_tokens is not None:
                row.input_tokens = input_tokens
            if output_tokens is not None:
                row.output_tokens = output_tokens
            if title is not None:
                row.title = title[:200]
            await session.commit()

    async def delete_session(self, session_id: str) -> bool:
        async with self.db.session() as session:
            row = await session.get(AgentSession, session_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def running_session_ids(self) -> list[str]:
        async with self.db.session() as session:
            result = await session.execute(
                select(AgentSession.id).where(
                    AgentSession.status.in_(("running", "waiting_approval"))
                )
            )
            return list(result.scalars())

    # ---- messages --------------------------------------------------------- #

    async def add_message(
        self,
        session_id: str,
        *,
        role: str,
        kind: str,
        content: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        """Append one transcript row; returns its seq number."""
        async with self.db.session() as session:
            seq_row = await session.execute(
                select(func.max(AgentMessage.seq)).where(
                    AgentMessage.session_id == session_id
                )
            )
            seq = int(seq_row.scalar() or 0) + 1
            session.add(
                AgentMessage(
                    session_id=session_id,
                    seq=seq,
                    role=role,
                    kind=kind,
                    content=content,
                    data=data,
                )
            )
            await session.commit()
            return seq

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            result = await session.execute(
                select(AgentMessage)
                .where(AgentMessage.session_id == session_id)
                .order_by(AgentMessage.seq)
            )
            return [
                {
                    "seq": row.seq,
                    "role": row.role,
                    "kind": row.kind,
                    "content": row.content,
                    "data": row.data,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in result.scalars()
            ]

    @staticmethod
    def _session_dict(row: AgentSession) -> dict[str, Any]:
        return {
            "id": row.id,
            "title": row.title,
            "model": row.model,
            "workspace": row.workspace,
            "status": row.status,
            "steps": row.steps,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
