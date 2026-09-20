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
    """Subset of the OpenAI Responses API accepted by ``/v1/responses``.

    Codex CLI (0.154+) only speaks this wire - ``wire_api = "chat"`` was
    removed - so the subset covers what a tool-calling agent replaying a full
    transcript actually sends: ``instructions``, ``input`` items of type
    ``message`` / ``function_call`` / ``function_call_output``, flat-format
    ``tools``, ``tool_choice`` and ``reasoning``. ``store`` / ``include`` /
    ``prompt_cache_key`` / ``previous_response_id`` are accepted and ignored:
    the gateway is stateless and always receives the whole conversation.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[dict[str, Any]]
    instructions: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    reasoning: dict[str, Any] | None = None
    #: Accepted-and-ignored stateful fields (see class docstring).
    store: bool | None = None
    include: list[str] | None = None
    prompt_cache_key: str | None = None
    previous_response_id: str | None = None
    metadata: dict[str, Any] | None = None

    def to_chat_request(self) -> ChatCompletionRequest:
        """Translate the Responses payload into a chat completion request."""
        messages: list[ChatMessage] = []
        if self.instructions:
            messages.append(ChatMessage(role="system", content=self.instructions))
        messages.extend(self._translate_input())
        request = ChatCompletionRequest(
            model=self.model,
            messages=messages,
            max_tokens=self.max_output_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            stream=self.stream,
            tools=translate_responses_tools(self.tools),
            tool_choice=translate_responses_tool_choice(self.tool_choice),
            parallel_tool_calls=self.parallel_tool_calls,
        )
        effort = (self.reasoning or {}).get("effort")
        if effort:
            # Same extra-param channel /v1/chat/completions clients use
            # (``reasoning_effort`` is forwarded, never required).
            request.__pydantic_extra__ = {
                **(request.__pydantic_extra__ or {}),
                "reasoning_effort": effort,
            }
        return request

    def _translate_input(self) -> list[ChatMessage]:
        """``input`` items -> chat messages.

        ``function_call`` becomes an assistant message carrying ``tool_calls``
        and ``function_call_output`` becomes a ``tool`` message, which is how a
        chat-completions upstream replays a tool round trip. Hosted-tool and
        reasoning items have no chat equivalent and are dropped.
        """
        if isinstance(self.input, str):
            return [ChatMessage(role="user", content=self.input)]
        messages: list[ChatMessage] = []
        for item in self.input:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "function_call":
                call_id = str(item.get("call_id") or item.get("id") or _new_call_id())
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id=call_id,
                                function=FunctionCall(
                                    name=str(item.get("name") or ""),
                                    arguments=str(item.get("arguments") or ""),
                                ),
                            )
                        ],
                    )
                )
                continue
            if kind == "function_call_output":
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=_flatten_text(item.get("output")),
                        tool_call_id=str(item.get("call_id") or ""),
                    )
                )
                continue
            if kind and kind != "message":
                continue  # reasoning / hosted-tool items: no chat equivalent
            role = str(item.get("role", "user"))
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            messages.append(ChatMessage(role=role, content=_flatten_text(item.get("content"))))
        return messages


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


def _flatten_text(content: Any) -> str | None:
    """Content parts (``input_text``/``output_text``/``text``/``refusal``) -> text."""
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"input_text", "output_text", "text", "refusal", None}:
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return str(content)


def translate_responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Flat Responses tools (``{"type":"function","name":...}``) -> chat format.

    Chat completions expects the nested ``{"type":"function","function":{...}}``
    envelope; Codex sends the flat one. Already-nested payloads pass through.
    Hosted tools (web_search, file_search, ...) have no equivalent and are
    dropped rather than forwarded into an upstream 400.
    """
    if not tools:
        return None
    translated: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        if "function" in tool:
            translated.append(tool)
            continue
        function = {
            key: tool[key] for key in ("name", "description", "parameters", "strict") if key in tool
        }
        translated.append({"type": "function", "function": function})
    return translated or None


def translate_responses_tool_choice(
    tool_choice: str | dict[str, Any] | None,
) -> str | dict[str, Any] | None:
    """``{"type":"function","name":...}`` -> chat's nested function choice."""
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        if "function" in tool_choice:
            return tool_choice
        name = tool_choice.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
        return "required"
    return tool_choice


def now_ts() -> int:
    """Unix timestamp helper (kept here so tests can freeze it)."""
    return int(time.time())
