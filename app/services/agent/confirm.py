"""ZK-Agent approval flow: dangerous tools park the loop until the user decides.

``write_file`` / ``run_command`` never execute without an explicit decision
from the console. A pending approval holds an :class:`asyncio.Event`; the API
resolves it, the loop wakes up and either performs the tool or feeds the model
a "user refused" tool message. ``remember`` whitelists one tool for the rest
of the session ("本会话内不再询问") — never across sessions.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PendingApproval:
    """One parked dangerous action."""

    id: str
    tool: str
    args: dict[str, Any]
    #: human-readable preview (unified diff / command line)
    preview: str
    display: dict[str, Any] | None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False
    remember: bool = False
    resolved: bool = False
    #: 只给「按角色记免打扰」的工具用（目前是 delegate_cli）；其余为空串。
    role: str = ""


class SessionApprovals:
    """Per-session pending approvals + "don't ask again" memory."""

    def __init__(self) -> None:
        self.pending: dict[str, PendingApproval] = {}
        self.always_allow: set[str] = set()

    def skipped(self, tool: str, *, role: str = "") -> bool:
        """这个工具（+ 可选角色）是否已在本会话被放行。

        ``role`` 只对 ``delegate_cli`` 有意义：按角色分别记忆，
        避免「放行一次免费档」变成「放行所有角色含收费 + bypass 档」。
        """
        return self._key(tool, role) in self.always_allow

    @staticmethod
    def _key(tool: str, role: str) -> str:
        """免打扰的记忆键。

        只有 ``delegate_cli`` 带角色；``write_file`` / ``run_command``
        仍按工具名记——它们的风险形状不随参数变化，拆细只会让审批变吵。
        MCP 工具同理带 ``tool`` 名（不同 MCP 工具风险不同）。
        """
        return f"{tool}:{role}" if role else tool

    def register(
        self,
        tool: str,
        args: dict[str, Any],
        preview: str,
        display: dict[str, Any] | None,
        *,
        role: str = "",
    ) -> PendingApproval:
        approval = PendingApproval(
            id=f"apr_{uuid.uuid4().hex[:12]}",
            tool=tool,
            args=args,
            preview=preview,
            display=display,
            role=role,
        )
        self.pending[approval.id] = approval
        return approval

    def resolve(
        self, approval_id: str, *, approved: bool, remember: bool = False
    ) -> PendingApproval | None:
        """Resolve a pending approval and wake the loop. Idempotent."""
        approval = self.pending.get(approval_id)
        if approval is None or approval.resolved:
            return None
        self.pending.pop(approval_id, None)
        approval.approved = approved
        approval.remember = approved and remember
        if approval.remember:
            self.always_allow.add(self._key(approval.tool, approval.role))
        approval.resolved = True
        approval.event.set()
        return approval

    def deny_all(self) -> None:
        """Used on cancel: wake the loop with a refusal for everything pending."""
        for approval_id in list(self.pending):
            self.resolve(approval_id, approved=False, remember=False)


__all__ = ["PendingApproval", "SessionApprovals"]
