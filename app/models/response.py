"""Canonical response shapes plus the internal streaming event model."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    "ResponsesInputTokensDetails",
    "ResponsesRequest",
    "ResponsesResponse",
    "ResponsesUsage",
    "RoutingMeta",
    "StreamEvent",
    "StreamEventType",
    "ToolCall",
    "Usage",
    "function_call_output_item",
    "message_output_item",
    "new_request_id",
]


class Usage(BaseModel):
    """Token accounting, normalised across providers."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    @model_validator(mode="before")
    @classmethod
    def _lift_openai_cached_tokens(cls, data: Any) -> Any:
        """OpenAI 系把缓存数埋在 ``usage.prompt_tokens_details.cached_tokens``。

        不提上来的话：缓存折扣完全不可见，控制台的 token 统计少了缓存维度，
        成本估算还要按全价计——DeepSeek 官方说「缓存命中的费用往往占比较高」，
        Agent 类长上下文重复前缀的工作负载，成本会高估近一个数量级。
        Anthropic 路径本来就传 ``cache_read_input_tokens``（见 anthropic.py），
        这条 before 校验让 OpenAI 兼容链路（DeepSeek/魔搭/商汤…）对齐。
        """
        if isinstance(data, dict) and "cached_tokens" not in data:
            details = data.get("prompt_tokens_details")
            if isinstance(details, dict):
                cached = details.get("cached_tokens")
                if cached:
                    data = {**data, "cached_tokens": cached}
        return data

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


class ResponsesInputTokensDetails(BaseModel):
    """Responses 协议的输入 token 明细。

    OpenAI Responses 的缓存命中数**只**出现在这里（``usage.input_tokens_details.
    cached_tokens``），没有 chat completions 那个 ``prompt_tokens_details`` 位置的
    兼容义务。DeepSeek Harness 的 Responses 客户端正是按这个路径读的
    （``response.usage.input_tokens_details.cached_tokens``，读不到就当 0）——
    2026-11-24 实测：harness 走 /v1/responses，网关此前只下发三个整数字段，
    于是它显示的缓存命中恒为 0%，而上游其实命中 90%+。
    """

    cached_tokens: int = 0


class ResponsesUsage(BaseModel):
    """Responses 协议的 usage。

    ``input_tokens`` 沿用 OpenAI 语义 = **含缓存命中**的总输入（与 chat
    completions 的 ``prompt_tokens`` 同一口径），客户端会自己减掉
    ``input_tokens_details.cached_tokens`` 得到真正新算的部分。
    """

    input_tokens: int = 0
    input_tokens_details: ResponsesInputTokensDetails = Field(
        default_factory=ResponsesInputTokensDetails
    )
    output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_usage(cls, usage: Usage | None) -> ResponsesUsage:
        """把内部规范化的 ``Usage`` 映射成 Responses 形状（唯一的转换入口）。

        /v1/responses 的三个构造点（assembler 收 chunk.usage / assembler 收 usage
        事件 / request_service 的非流式响应）全部走这里，
        免得哪一处又漏掉缓存字段——这正是本次 bug 的成因。

        ⚠ **不做 ``cached <= input`` 的夹取**。看着像个该有的保护，实际会**改错数**：
        本项目内部的 ``Usage.prompt_tokens`` 对 OpenAI 系是「含缓存的输入」
        （``app/providers/base.py`` 直接透传上游 ``prompt_tokens``），对 Anthropic 系
        却是「不含缓存的输入」（``app/providers/anthropic.py:364-368`` 把
        ``input_tokens`` 原样当 prompt、``cache_read_input_tokens`` 当 cached）。
        按 prompt 当上界夹取会把 StepFun 这类 Anthropic 上游的缓存数**削减到错值**。
        客户端本来就有 ``Math.max(0, input - cached)`` 这种防护（DSH 的 Responses
        客户端实测如此），夹取收益为零、代价是静默改数。
        顺带：某条日志出现 >100% 的命中率正是在提示「这个上游不是 OpenAI 语义」，
        夹掉就看不见这个信号了。
        """
        if usage is None:
            return cls()
        return cls(
            input_tokens=usage.prompt_tokens,
            input_tokens_details=ResponsesInputTokensDetails(
                cached_tokens=max(0, usage.cached_tokens)
            ),
            output_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
        )


def message_output_item(text: str, *, item_id: str | None = None) -> dict[str, Any]:
    """Assistant text turn as a Responses ``output`` item."""
    return {
        "id": item_id or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def function_call_output_item(call: ToolCall, *, item_id: str | None = None) -> dict[str, Any]:
    """Chat tool call as a Responses ``function_call`` output item."""
    return {
        "id": item_id or call.id or f"fc_{uuid.uuid4().hex[:24]}",
        "type": "function_call",
        "status": "completed",
        "arguments": call.function.arguments,
        "call_id": call.id or f"call_{uuid.uuid4().hex[:24]}",
        "name": call.function.name,
    }


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
    # 默认取当前墙钟：0.0 会被 `deployment_summary` 的 7 天 cutoff 静默过滤，
    # 一条漏传 started_at 的 attempt 就从「各部署真实接单」里消失了（2026-11-26 审查）。
    started_at: float = Field(default_factory=time.time)
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
