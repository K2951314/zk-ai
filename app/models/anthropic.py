"""Anthropic Messages API wire models for ``/v1/messages``.

The gateway speaks OpenAI internally; this module defines the *inbound* Anthropic
protocol surface (Claude Code, CC Switch in anthropic-passthrough mode, ...) and
translates it into the canonical :class:`ChatCompletionRequest`. The response
side (including the streaming block assembly that must always emit
``content_block_stop``) lives in :mod:`app.api.messages`.

Only what clients actually send is modelled; unknown fields are kept
(``extra="allow"``) and deliberately ignored: ``thinking``, ``context_management``,
``metadata`` and friends have no cross-provider meaning yet.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.request import ChatCompletionRequest, ChatMessage, FunctionCall, ToolCall


def _blocks(content: Any) -> list[dict[str, Any]]:
    """Normalise Anthropic ``content`` (string or block list) into block dicts."""
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _text_of(content: Any) -> str:
    """Flatten text-bearing blocks (``text``/``input_text``) into one string."""
    parts = [
        str(block.get("text", ""))
        for block in _blocks(content)
        if block.get("type") in {"text", "input_text"}
    ]
    return "\n".join(part for part in parts if part)


def _image_part(block: dict[str, Any]) -> dict[str, Any] | None:
    """Map an Anthropic ``image`` block onto an OpenAI ``image_url`` part."""
    source = block.get("source") or {}
    if source.get("type") == "base64" and source.get("data"):
        url = f"data:{source.get('media_type', 'image/png')};base64,{source['data']}"
        return {"type": "image_url", "image_url": {"url": url}}
    if source.get("type") == "url" and source.get("url"):
        return {"type": "image_url", "image_url": {"url": source["url"]}}
    return None


def strip_context_suffix(model: str) -> str:
    """Drop the ``[1M]``-style context-window suffix clients append to model ids."""
    return model.split("[", 1)[0].strip()


#: ``thinking.type`` 里明确表示「关掉」的取值。
_THINKING_OFF_TYPES = frozenset({"disabled", "off"})

#: ``thinking.budget_tokens`` -> 网关档位。表取自 DeepSeek Harness 的
#: ``DEFAULT_THINKING_BUDGETS``（mini 1024 / low 2048 / medium 8192 / high 16384）：
#: 网关收到的预算就是按这张表算出来的，用同一张表反推才可能还原客户端意图。
#: 升序排列，:func:`_tier_for_budget` 取不超过所给预算的**最高**档。
_THINKING_BUDGET_TIERS: tuple[tuple[str, int], ...] = (
    ("minimal", 1024),
    ("low", 2048),
    ("medium", 8192),
    ("high", 16384),
)


def _thinking_budget(thinking: dict[str, Any]) -> int | None:
    """取 ``budget_tokens``；缺失或非法返回 ``None``（= 客户端只说了要思考）。"""
    raw = thinking.get("budget_tokens")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw <= 0:
        return None
    return int(raw)


def _tier_for_budget(budget: int) -> str:
    """预算 -> 不超过它的最高档；比最小档还小按最小档算。"""
    tier = _THINKING_BUDGET_TIERS[0][0]
    for name, threshold in _THINKING_BUDGET_TIERS:
        if budget >= threshold:
            tier = name
    return tier


def _switch_is_off(value: Any) -> bool:
    """True only for an explicit off switch (missing/``None`` is not "off")."""
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"false", "0", "no", "off", "disabled", "none"}
    return False


def _effort_of(config: dict[str, Any] | None) -> str | None:
    """``output_config: {"effort": "high"}`` -> ``"high"``；取不到返回 ``None``."""
    if not isinstance(config, dict):
        return None
    effort = config.get("effort")
    if not isinstance(effort, str):
        return None
    return effort.strip().lower() or None


class AnthropicMessagesRequest(BaseModel):
    """Anthropic ``POST /v1/messages`` request body (permissive superset)."""

    model_config = ConfigDict(extra="allow")

    model: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    system: str | list[dict[str, Any]] | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stream: bool = False
    stop_sequences: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    #: Messages 侧的思考开关。官方形状是
    #: ``{"type": "enabled", "budget_tokens": N}`` / ``{"type": "disabled"}``；
    #: 客户端也可能发 ``{"type": "adaptive"}`` 或 ``{"type": "enabled"}`` 不带预算
    #: （DeepSeek Harness 的 ``streamSimple`` 就往这个分支走）。
    thinking: dict[str, Any] | None = None
    #: Messages 侧的档位字段（``{"effort": "high"}``）。与 ``thinking`` 二选一，
    #: 见 :meth:`reasoning_effort` 的优先级说明。
    output_config: dict[str, Any] | None = None

    def reasoning_effort(self) -> str | None:
        """Messages 请求里的思考强度 -> 网关的 ``reasoning_effort`` 档位。

        这是 ``/v1/messages`` 上唯一一处把「Anthropic 的思考写法」翻成
        「网关的档位词」的地方。**不翻就会静默丢弃**：2026-11-24 实测
        DeepSeek Harness 桌面档 ``reasoningEffort: max`` 落在
        ``thinking.budget_tokens`` 上，网关老版本既不认识也没报错，客户端以为
        生效了，实际按上游默认档跑。

        优先级沿用全仓同一套（2026-11-09 起，见 ``/v1/chat/completions`` 的
        ``thinking`` / ``enable_thinking`` 处理）：开关优先于档位。
        ``thinking.type == "disabled"`` 一律 ``"none"``——即便客户端顺手还带了
        ``output_config.effort``，关掉是不可能被"顺手"打开的。

        ``budget_tokens`` 折档位用的是 ``_THINKING_BUDGET_TIERS`` 这张**客户端
        同款**表（DeepSeek Harness 的 ``DEFAULT_THINKING_BUDGETS``），取不超过
        所给预算的最高档。这是有损映射，但方向正确：预算越大档位越高，
        而且是"就低"而不是"就高"——宁可让上游少想一点，也不要偷偷把成本抬上去。
        """
        thinking = self.thinking
        if isinstance(thinking, dict):
            kind = str(thinking.get("type") or "").strip().lower()
            if kind in _THINKING_OFF_TYPES or _switch_is_off(thinking.get("enabled")):
                return "none"
            budget = _thinking_budget(thinking)
            if budget is None:
                # ``{"type": "enabled"}`` / ``{"type": "adaptive"}`` 不带预算：
                # 客户端只说了"要思考"没说多深，交给部署默认档而不是替它选。
                return ""
            return _tier_for_budget(budget)
        return _effort_of(self.output_config)

    def to_chat_request(
        self, *, default_model: str, fallback_max_tokens: int
    ) -> ChatCompletionRequest:
        """Translate into the canonical OpenAI-shaped request.

        Model handling: the ``[1M]`` context suffix is stripped, and unknown
        model ids (Claude Code sends ``claude-opus-5`` & friends regardless of
        provider mapping) fall back to ``default_model`` so the alias router
        can still serve the request.
        """
        model = strip_context_suffix(self.model)
        if not model or model.startswith("claude"):
            model = default_model

        messages: list[ChatMessage] = []
        system_text = _text_of(self.system) if not isinstance(self.system, str) else self.system
        if system_text:
            messages.append(ChatMessage(role="system", content=system_text))
        for raw in self.messages:
            messages.extend(self._translate_message(raw))

        tool_choice: str | dict[str, Any] | None = None
        if isinstance(self.tool_choice, dict):
            choice_type = self.tool_choice.get("type")
            if choice_type == "auto":
                tool_choice = "auto"
            elif choice_type == "any":
                tool_choice = "required"
            elif choice_type == "none":
                tool_choice = "none"
            elif choice_type == "tool" and self.tool_choice.get("name"):
                tool_choice = {
                    "type": "function",
                    "function": {"name": str(self.tool_choice["name"])},
                }

        tools: list[dict[str, Any]] | None = None
        if self.tools:
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": str(tool.get("name", "")),
                        "description": str(tool.get("description", "")),
                        "parameters": tool.get("input_schema") or {"type": "object"},
                    },
                }
                for tool in self.tools
                if isinstance(tool, dict) and tool.get("name")
            ]

        request = ChatCompletionRequest(
            model=model,
            messages=messages,
            max_tokens=self.max_tokens or fallback_max_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            top_k=self.top_k,
            stream=self.stream,
            stop=self.stop_sequences,
            tools=tools,
            tool_choice=tool_choice,
            stream_options={"include_usage": True} if self.stream else None,
        )
        effort = self.reasoning_effort()
        if effort:
            # Same extra-param channel as /v1/responses (see
            # ``ResponsesRequest.to_chat_request``): the effort rides along on
            # ``model_extra`` so every adapter sees it through ``extra_params()``.
            request.__pydantic_extra__ = {
                **(request.__pydantic_extra__ or {}),
                "reasoning_effort": effort,
            }
        return request

    def estimate_input_tokens(self, *, default_model: str, fallback_max_tokens: int) -> int:
        """Cheap heuristic token estimate for ``/v1/messages/count_tokens``."""
        return self.to_chat_request(
            default_model=default_model, fallback_max_tokens=fallback_max_tokens
        ).estimated_input_tokens()

    # ------------------------------------------------------------------ #
    # Message translation
    # ------------------------------------------------------------------ #
    def _translate_message(self, raw: dict[str, Any]) -> list[ChatMessage]:
        """One Anthropic turn -> one or more canonical messages.

        ``tool_result`` blocks arrive inside the *user* turn but must become
        OpenAI ``role="tool"`` messages; trailing user text is kept as a
        separate ``user`` message so the tool messages directly follow the
        assistant ``tool_calls`` turn, which is what OpenAI-compatible
        upstreams require.
        """
        role = str(raw.get("role", "user"))
        blocks = _blocks(raw.get("content"))

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            for block in blocks:
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block_type == "tool_use":
                    tool_calls.append(
                        ToolCall(
                            id=str(block.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                            function=FunctionCall(
                                name=str(block.get("name", "")),
                                arguments=json.dumps(
                                    block.get("input") or {}, ensure_ascii=False
                                ),
                            ),
                        )
                    )
                # ``thinking`` blocks in history are deliberately dropped.
            return [
                ChatMessage(
                    role="assistant",
                    content="\n".join(part for part in text_parts if part) or None,
                    tool_calls=tool_calls or None,
                )
            ]

        if role == "user":
            translated: list[ChatMessage] = []
            pending_text: list[str] = []
            pending_parts: list[dict[str, Any]] = []
            for block in blocks:
                block_type = block.get("type")
                if block_type == "tool_result":
                    if pending_text or pending_parts:
                        translated.append(self._user_message(pending_text, pending_parts))
                        pending_text, pending_parts = [], []
                    translated.append(
                        ChatMessage(
                            role="tool",
                            tool_call_id=str(block.get("tool_use_id") or ""),
                            content=_text_of(block.get("content")) or None,
                        )
                    )
                elif block_type == "image":
                    part = _image_part(block)
                    if part is not None:
                        pending_parts.append(part)
                elif block_type == "text":
                    pending_text.append(str(block.get("text", "")))
            if pending_text or pending_parts:
                translated.append(self._user_message(pending_text, pending_parts))
            return translated or [ChatMessage(role="user", content="")]

        # ``system`` inside messages (rare) is treated as user context.
        return [ChatMessage(role="user", content=_text_of(blocks) or None)]

    @staticmethod
    def _user_message(
        text_parts: list[str], image_parts: list[dict[str, Any]]
    ) -> ChatMessage:
        if image_parts:
            content: str | list[dict[str, Any]] = [
                {"type": "text", "text": "\n".join(p for p in text_parts if p)},
                *image_parts,
            ]
        else:
            content = "\n".join(part for part in text_parts if part)
        return ChatMessage(role="user", content=content or "")
