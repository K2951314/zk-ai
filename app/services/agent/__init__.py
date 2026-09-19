"""ZK-Agent: a Codex-style batch coding-task runner living inside the gateway.

The loop drives the gateway's own routing stack (``RequestService`` -> alias ->
credential pool), executes a small whitelist of workspace tools and parks on
user approval before any dangerous action. See ``/ui/agent`` for the console.
"""

from app.services.agent.service import AgentService

__all__ = ["AgentService"]
