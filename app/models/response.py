"""Canonical response shapes plus the internal streaming event model."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.request import (
    ChatMessage,
    FunctionCall,
    ResponsesRequest,
    ToolCall,
    new_request_id,
)

__all__ = [
    "AttemptOutcome",
    "ChatCompletionChoice",
    "ChatCompletionChunk",
    "ChatCompletionChunkChoice",
    "ChatCompletionResponse",
    "ChatMessage",
    "ChunkDelta",
    "FunctionCall",
    "ResponsesRequest",
    "ResponsesResponse",
    "ResponsesUsage",
    "RoutingMeta",
    "StreamEvent",
    "StreamEventType",
    "ToolCall",
    "Usage",
    "new_request_id",
]


class Usage(BaseModel):
    """Token accounting, normalised across providers."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    @classmethod
    def build(cls, prompt: int | None, completion: int | None, cached: int = 0) -> Usage:
        prompt_tokens = int(prompt or 0)
        completion_tokens = int(completion or 0)
        return cls(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cached_tokens=cached,
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


class RoutingMeta(BaseModel):
    """Explainability payload attached to every response (``zk_ai`` field)."""

    request_id: str
    requested_model: str
    alias: str | None = None
    resolved_model: str
    provider: str
    deployment_id: str | None = None
    credential_id: str | None = None
    attempt: int = 1
    routing_reason: str = ""
    capability_scores: dict[str, float] = Field(default_factory=dict)
    latency_ms: float = 0.0
    finish_reason: str | None = None
    fallback_used: bool = False
    cooldowns: dict[str, float] = Field(default_factory=dict)


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = None
    logprobs: dict[str, Any] | None = None


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible non-streaming response."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=new_request_id)
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage = Field(default_factory=Usage)
    system_fingerprint: str | None = None
    #: ZK-AI extension: routing explainability. Ignored by OpenAI SDK clients.
    zk_ai: RoutingMeta | None = None

    @classmethod
    def simple(
        cls,
        *,
        model: str,
        content: str,
        finish_reason: str = "stop",
        usage: Usage | None = None,
        tool_calls: list[ToolCall] | None = None,
        provider_id: str | None = None,
    ) -> ChatCompletionResponse:
        """Build a single-choice response - used by adapters and tests."""
        message = ChatMessage(role="assistant", content=content, tool_calls=tool_calls or None)
        response = cls(
            model=model,
            choices=[
                ChatCompletionChoice(index=0, message=message, finish_reason=finish_reason)
            ],
            usage=usage or Usage(),
        )
        if provider_id:
            response.system_fingerprint = f"zkai-{provider_id}"
        return response

    def text(self) -> str:
        """First choice rendered as plain text."""
        if not self.choices:
            return ""
        return self.choices[0].message.text()


class ChunkDelta(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str | None = None
    content: str | None = None
    tool_calls: list[ToolCall] | None = None

    @property
    def thinking(self) -> str | None:
        """Reasoning text carried by non-canonical providers.

        Several OpenAI-compatible gateways (ModelScope, SenseNova) stream the
        *entire* answer inside ``reasoning_content`` and leave ``content`` as an
        empty string. Reading it lets the adapter surface the text instead of
        streaming nothing at all.
        """
        extra = self.model_extra
        if not extra:
            return None
        for key in ("reasoning_content", "reasoning"):
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: ChunkDelta = Field(default_factory=ChunkDelta)
    finish_reason: str | None = None

    def recover_reasoning_only_delta(self) -> bool:
        """Move ``reasoning_content`` into ``content`` when content is empty.

        Returns ``True`` when a substitution happened. Mirrors
        :meth:`ChatCompletionResponse` recovery so streaming and non-streaming
        clients observe the same text for the same upstream response.
        """
        thinking = self.delta.thinking
        if thinking is None:
            return False
        if self.delta.content:
            return False
        self.delta.content = thinking
        extra = self.delta.model_extra
        if extra is not None:
            extra["content_recovered_from_reasoning"] = True
            # The text now lives in ``content``. Drop the source fields so
            # clients that read both do not render the same chunk twice
            # (once as body text, once as a "thinking" bubble).
            extra.pop("reasoning_content", None)
            extra.pop("reasoning", None)
        return True


class ChatCompletionChunk(BaseModel):
    """OpenAI-compatible streaming chunk."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:24]}")
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    choices: list[ChatCompletionChunkChoice] = Field(default_factory=list)
    usage: Usage | None = None


class ResponsesUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ResponsesResponse(BaseModel):
    """Minimal OpenAI Responses API shaped payload."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: f"resp_{uuid.uuid4().hex[:24]}")
    object: Literal["response"] = "response"
    created_at: int = Field(default_factory=lambda: int(time.time()))
    model: str
    status: Literal["completed", "failed", "in_progress"] = "completed"
    output: list[dict[str, Any]] = Field(default_factory=list)
    output_text: str = ""
    usage: ResponsesUsage = Field(default_factory=ResponsesUsage)
    zk_ai: RoutingMeta | None = None


# --------------------------------------------------------------------------- #
# Internal events
# --------------------------------------------------------------------------- #
StreamEventType = Literal["chunk", "usage", "error", "end", "meta"]


class StreamEvent(BaseModel):
    """Internal event emitted by the scheduler while streaming.

    Only events of type ``chunk`` are forwarded verbatim to the client; the rest
    drive gateway-side bookkeeping (usage accounting, error propagation).
    """

    type: StreamEventType
    chunk: ChatCompletionChunk | None = None
    usage: Usage | None = None
    #: OpenAI-shaped error envelope (``{"error": {...}}``), ready to serialise.
    error: dict[str, Any] | None = None
    meta: RoutingMeta | None = None
    raw: dict[str, Any] | None = None


class AttemptOutcome(BaseModel):
    """Result of a single upstream attempt, persisted to ``request_attempts``."""

    attempt_number: int
    provider: str
    model: str
    credential_id: str | None = None
    deployment_id: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    latency_ms: float = 0.0
    status: Literal["success", "error", "cancelled"] = "success"
    error_type: str | None = None
    http_status: int | None = None
    detail: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()
