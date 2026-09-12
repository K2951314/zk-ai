"""Message / request / response schemas.

The gateway is OpenAI-compatible on the wire, so these models *are* the internal
canonical format: providers translate their native payloads into these shapes and
the API layer serialises them back out unchanged.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["system", "user", "assistant", "tool", "developer", "function"]

#: Parameters the gateway understands and can map for at least one provider.
KNOWN_PARAMS: frozenset[str] = frozenset(
    {
        "model",
        "messages",
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "max_completion_tokens",
        "stream",
        "tools",
        "tool_choice",
        "response_format",
        "stop",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "n",
        "user",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "parallel_tool_calls",
    }
)


def new_request_id() -> str:
    """Return an OpenAI-looking request id (``chatcmpl-<hex>``)."""
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


class FunctionCall(BaseModel):
    """``function`` half of a tool call.

    ``name`` is optional on purpose: OpenAI-compatible *streaming* deltas repeat
    the envelope on every chunk but only fill ``arguments`` (SenseNova sends
    ``""``, DeepSeek omits the key entirely). A required ``name`` here turned
    every successful tool-call stream into a gateway-side ValidationError.
    """

    name: str = ""
    arguments: str = ""

    @field_validator("name", "arguments", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        return "" if value is None else value


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")
    type: Literal["function"] = "function"
    index: int | None = None
    function: FunctionCall = Field(default_factory=FunctionCall)

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> Any:
        """Coerce the empty/omitted ``type`` that streaming deltas carry.

        Providers only send ``"function"`` on the first delta; later chunks use
        ``""`` (SenseNova) or omit the field (OpenAI itself). The field is
        informational - ``index`` is what clients merge on - so normalise it
        instead of rejecting the chunk.
        """
        if value is None or value == "":
            return "function"
        return value

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, value: Any) -> Any:
        # Continuation deltas repeat ``id: ""``; keep it empty rather than
        # minting a fresh uuid per chunk (clients merge by ``index`` anyway).
        if value is None or value == "":
            return ""
        return value

    @field_validator("function", mode="before")
    @classmethod
    def _normalize_function(cls, value: Any) -> Any:
        # An explicit ``function: null`` would bypass the default_factory.
        return {} if value is None else value


class ChatMessage(BaseModel):
    """A single conversation turn. ``content`` may be a string or content parts."""

    model_config = ConfigDict(extra="allow")

    role: Role
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def text(self) -> str:
        """Flatten the message to plain text (used for capability inference)."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            chunks: list[str] = []
            for part in self.content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
            return "\n".join(chunks)
        return ""

    def has_image(self) -> bool:
        """True when any content part carries an image."""
        if isinstance(self.content, list):
            for part in self.content:
                if isinstance(part, dict) and part.get("type") in {"image_url", "image", "input_image"}:
                    return True
        return False


class ChatCompletionRequest(BaseModel):
    """OpenAI ``/v1/chat/completions`` request body (permissive superset)."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    n: int | None = None
    user: str | None = None
    parallel_tool_calls: bool | None = None
    #: Optional client-supplied conversation identity. When present it is the
    #: session key for credential affinity (the pool pins this conversation to
    #: the key that last served it); otherwise a stable fingerprint of the
    #: conversation is derived. Never forwarded upstream.
    session_id: str | None = None

    @property
    def effective_max_tokens(self) -> int | None:
        """``max_completion_tokens`` wins over the legacy ``max_tokens``."""
        return self.max_completion_tokens or self.max_tokens

    def extra_params(self) -> dict[str, Any]:
        """Unknown top-level fields sent by the client."""
        return {
            key: value
            for key, value in (self.model_extra or {}).items()
            if key not in KNOWN_PARAMS
        }

    def estimated_input_tokens(self) -> int:
        """Cheap heuristic (~4 chars/token) used for long-context routing."""
        total = 0
        for message in self.messages:
            total += len(message.text()) + len(message.role) + 4
        if self.tools:
            total += len(str(self.tools)) // 3
        return max(1, total // 4)


class ResponsesRequest(BaseModel):
    """Subset of the OpenAI Responses API accepted by ``/v1/responses``."""

    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[dict[str, Any]]
    instructions: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False

    def to_chat_request(self) -> ChatCompletionRequest:
        """Translate the Responses payload into a chat completion request."""
        messages: list[ChatMessage] = []
        if self.instructions:
            messages.append(ChatMessage(role="system", content=self.instructions))
        if isinstance(self.input, str):
            messages.append(ChatMessage(role="user", content=self.input))
        else:
            for item in self.input:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", "user"))
                if role not in {"system", "user", "assistant", "tool", "developer"}:
                    role = "user"
                content = item.get("content")
                if isinstance(content, list):
                    text_parts = [
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") in {"input_text", "text"}
                    ]
                    content = "\n".join(p for p in text_parts if p)
                messages.append(ChatMessage(role=role, content=content))
        return ChatCompletionRequest(
            model=self.model,
            messages=messages,
            max_tokens=self.max_output_tokens,
            temperature=self.temperature,
            stream=self.stream,
        )


def now_ts() -> int:
    """Unix timestamp helper (kept here so tests can freeze it)."""
    return int(time.time())
