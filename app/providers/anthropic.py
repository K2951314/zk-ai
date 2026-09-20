"""Anthropic Messages API adapter.

Verified against the official Messages API contract:

* ``POST {base_url}/messages`` (base URL default ``https://api.anthropic.com/v1``)
* auth: ``x-api-key: <key>`` + ``anthropic-version: 2023-06-01`` (no Bearer)
* body: ``model`` (required), ``max_tokens`` (**required**), ``messages``,
  ``system``, ``temperature``, ``top_p``, ``stop_sequences``, ``tools``,
  ``tool_choice`` (``auto`` | ``any`` | ``none`` | ``tool``), ``stream``
* tools use ``input_schema`` (not ``function.parameters``)
* tool calls come back as ``content`` blocks of type ``tool_use``
* tool results are sent as a **user** message containing ``tool_result`` blocks
* ``usage`` exposes ``input_tokens`` / ``output_tokens``
* streaming events: ``message_start``, ``content_block_start``,
  ``content_block_delta`` (``text_delta`` / ``input_json_delta``),
  ``content_block_stop``, ``message_delta``, ``message_stop``

Known capability gaps versus OpenAI (reported, never silently dropped):
``top_k``, ``seed``, ``presence_penalty``, ``frequency_penalty``, ``n``,
``logprobs``, ``parallel_tool_calls``. ``response_format`` has no native
equivalent, so it is *emulated* with a system instruction (documented behaviour).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from app.core.errors import BadGatewayError, InvalidRequestError
from app.core.logging import get_logger
from app.models.credential import CredentialRuntime
from app.models.provider import DeploymentConfig, ProviderType
from app.models.request import ChatCompletionRequest
from app.models.response import (
    ChatCompletionChunk,
    ChatCompletionResponse,
    FunctionCall,
    ToolCall,
    Usage,
)
from app.providers.base import ProviderAdapter, ProviderContext

logger = get_logger("provider.anthropic")

DEFAULT_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024

#: OpenAI parameters Anthropic has no equivalent for.
UNMAPPABLE = frozenset(
    {
        "top_k",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "n",
        "logprobs",
        "top_logprobs",
        "logit_bias",
        "parallel_tool_calls",
    }
)


class AnthropicAdapter(ProviderAdapter):
    """Adapter for the Anthropic Messages API."""

    provider_type = ProviderType.ANTHROPIC
    openai_compatible = False
    supported_params = frozenset(
        {
            "model",
            "messages",
            "max_tokens",
            "max_completion_tokens",
            "system",
            "temperature",
            "top_p",
            "stop",
            "tools",
            "tool_choice",
            "stream",
        }
    )

    # ------------------------------------------------------------------ #
    # HTTP surface
    # ------------------------------------------------------------------ #
    @property
    def messages_url(self) -> str:
        return f"{self.base_url}/messages"

    def _auth_headers(self, credential: CredentialRuntime | None) -> dict[str, str]:
        headers = {
            "anthropic-version": self.config.api_version or DEFAULT_VERSION,
            "accept": "application/json",
        }
        if credential and credential.secret:
            headers["x-api-key"] = credential.secret
        return headers

    # ------------------------------------------------------------------ #
    # Payload construction
    # ------------------------------------------------------------------ #
    def _convert_content(self, content: Any) -> Any:
        """OpenAI content parts -> Anthropic content blocks."""
        if isinstance(content, str) or content is None:
            return content
        blocks: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"text", "input_text"}:
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif part_type in {"image_url", "image"}:
                image = part.get("image_url") or part.get("image") or {}
                url = image.get("url") if isinstance(image, dict) else str(image)
                if isinstance(url, str) and url.startswith("data:"):
                    header, _, payload = url.partition(",")
                    media_type = header[5:].split(";")[0] or "image/png"
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": payload,
                            },
                        }
                    )
                elif url:
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            else:
                blocks.append({"type": "text", "text": json.dumps(part, ensure_ascii=False)})
        return blocks

    def _convert_messages(
        self, request: ChatCompletionRequest
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Split OpenAI messages into (anthropic messages, system prompt)."""
        system_chunks: list[str] = []
        messages: list[dict[str, Any]] = []

        for message in request.messages:
            if message.role in {"system", "developer"}:
                text = message.text()
                if text:
                    system_chunks.append(text)
                continue

            if message.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "unknown",
                    "content": message.text() or "",
                }
                if messages and messages[-1]["role"] == "user" and isinstance(
                    messages[-1]["content"], list
                ):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
                continue

            role = "assistant" if message.role == "assistant" else "user"
            if role == "assistant" and message.tool_calls:
                blocks: list[dict[str, Any]] = []
                text = message.text()
                if text:
                    blocks.append({"type": "text", "text": text})
                for call in message.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.function.name,
                            "input": self._parse_json_object(call.function.arguments),
                        }
                    )
                messages.append({"role": role, "content": blocks})
                continue

            content = self._convert_content(message.content)
            if content in (None, "", []):
                content = " "  # Anthropic rejects empty content
            messages.append({"role": role, "content": content})

        if not messages:
            raise InvalidRequestError("at least one non-system message is required")
        system = "\n\n".join(system_chunks) if system_chunks else None
        return messages, system

    @staticmethod
    def _parse_json_object(raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"_raw": raw}

    def _convert_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        converted: list[dict[str, Any]] = []
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if not isinstance(function, dict):
                continue
            converted.append(
                {
                    "name": function.get("name", "unnamed"),
                    "description": function.get("description") or "",
                    "input_schema": function.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
        return converted or None

    def _convert_tool_choice(self, choice: Any) -> dict[str, Any] | None:
        if choice is None:
            return None
        if isinstance(choice, str):
            mapping = {
                "auto": {"type": "auto"},
                "required": {"type": "any"},
                "any": {"type": "any"},
                "none": {"type": "none"},
            }
            return mapping.get(choice)
        if isinstance(choice, dict):
            if choice.get("type") == "function":
                name = (choice.get("function") or {}).get("name")
                if name:
                    return {"type": "tool", "name": name}
                return {"type": "any"}
            if "type" in choice:
                return {"type": choice["type"]}
        return None

    def _json_instruction(self, response_format: dict[str, Any] | None) -> str | None:
        """Emulate OpenAI JSON mode - Anthropic has no native ``response_format``."""
        if not response_format:
            return None
        fmt = response_format.get("type")
        if fmt == "json_object":
            return "Respond with a single valid JSON object and nothing else."
        if fmt == "json_schema":
            schema = (response_format.get("json_schema") or {}).get("schema")
            if schema:
                return (
                    "Respond with a single valid JSON value that strictly conforms to "
                    f"this JSON Schema: {json.dumps(schema, ensure_ascii=False)}"
                )
        return None

    def build_payload(
        self, request: ChatCompletionRequest, deployment: DeploymentConfig
    ) -> tuple[dict[str, Any], list[str]]:
        messages, system = self._convert_messages(request)
        unsupported: list[str] = []

        max_tokens = request.effective_max_tokens or deployment.max_output_tokens
        if not max_tokens:
            max_tokens = int(self.config.options.get("default_max_tokens", DEFAULT_MAX_TOKENS))

        payload: dict[str, Any] = {
            "model": deployment.model,
            "messages": messages,
            "max_tokens": int(max_tokens),
        }
        if system:
            payload["system"] = system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop:
            payload["stop_sequences"] = [request.stop] if isinstance(request.stop, str) else request.stop

        tools = self._convert_tools(request.tools)
        if tools:
            payload["tools"] = tools
            tool_choice = self._convert_tool_choice(request.tool_choice)
            if tool_choice:
                payload["tool_choice"] = tool_choice

        instruction = self._json_instruction(request.response_format)
        if instruction:
            payload["system"] = (
                f"{payload.get('system')}\n\n{instruction}" if payload.get("system") else instruction
            )
            logger.info(
                "provider=anthropic emulating response_format via system instruction (model=%s)",
                deployment.model,
            )

        for key, value in {
            "top_k": request.top_k,
            "seed": request.seed,
            "presence_penalty": request.presence_penalty,
            "frequency_penalty": request.frequency_penalty,
            "n": request.n,
            "parallel_tool_calls": request.parallel_tool_calls,
        }.items():
            if value is not None:
                unsupported.append(key)

        extra = request.extra_params()
        unsupported.extend(extra.keys())
        payload.update(deployment.options)
        return payload, sorted(set(unsupported))

    # ------------------------------------------------------------------ #
    # Non-streaming
    # ------------------------------------------------------------------ #
    async def chat(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> ChatCompletionResponse:
        payload, unsupported = self.build_payload(request, ctx.deployment)
        if unsupported:
            logger.info(
                "provider=anthropic unmapped params (model=%s): %s",
                ctx.upstream_model,
                ",".join(unsupported),
            )
        data = await self._post_json(self.messages_url, payload, ctx=ctx)
        response = self.normalize_response(data)
        response.model = ctx.upstream_model
        return response

    def normalize_response(self, payload: dict[str, Any]) -> ChatCompletionResponse:
        """Anthropic ``Message`` -> canonical chat completion."""
        if payload.get("type") == "error" or "content" not in payload:
            raise BadGatewayError(
                f"unexpected Anthropic payload: {str(payload)[:200]}", provider=self.provider_id
            )
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in payload.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") in {"thinking", "redacted_thinking"}:
                thinking_parts.append(str(block.get("thinking") or ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id") or ""),
                        function=FunctionCall(
                            name=str(block.get("name") or ""),
                            arguments=json.dumps(block.get("input") or {}, ensure_ascii=False),
                        ),
                    )
                )
        usage_raw = payload.get("usage") or {}
        usage = Usage.build(
            usage_raw.get("input_tokens"),
            usage_raw.get("output_tokens"),
            usage_raw.get("cache_read_input_tokens") or 0,
        )
        finish = self._normalize_finish_reason(payload.get("stop_reason"))
        response = ChatCompletionResponse.simple(
            model=str(payload.get("model") or self.provider_id),
            content="".join(text_parts),
            finish_reason=finish or ("tool_calls" if tool_calls else "stop"),
            usage=usage,
            tool_calls=tool_calls or None,
            provider_id=self.provider_id,
        )
        if payload.get("id"):
            response.id = str(payload["id"])
        # Carry the thinking the way the rest of the gateway does (a ``reasoning``
        # extra): /v1/messages re-emits it as a thinking block, ZKAI_STRIP_REASONING
        # drops it, and _recover_reasoning_only_content surfaces it as the answer
        # when a low max_tokens cut the reply off before any text block existed -
        # otherwise the client gets an empty response and no explanation.
        thinking = "".join(thinking_parts).strip()
        if thinking:
            extra = response.choices[0].message.model_extra
            if extra is not None:
                extra["reasoning"] = thinking
        self._recover_reasoning_only_content(response)
        return response

    # ------------------------------------------------------------------ #
    # Streaming
    # ------------------------------------------------------------------ #
    async def stream(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> AsyncIterator[ChatCompletionChunk]:
        payload, unsupported = self.build_payload(request, ctx.deployment)
        if unsupported:
            logger.info(
                "provider=anthropic unmapped params (stream, model=%s): %s",
                ctx.upstream_model,
                ",".join(unsupported),
            )
        payload["stream"] = True

        message_id: str | None = None
        input_tokens = 0
        output_tokens = 0
        finish_reason: str | None = None
        tool_index = -1

        async for event, data in self._stream_lines(self.messages_url, payload, ctx=ctx):
            parsed = self._safe_json(data)
            if not isinstance(parsed, dict):
                continue
            event_type = parsed.get("type") or event

            if event_type == "error":
                message = (parsed.get("error") or {}).get("message", "anthropic stream error")
                raise BadGatewayError(str(message), provider=self.provider_id)

            if event_type == "message_start":
                message = parsed.get("message") or {}
                message_id = message.get("id")
                usage = message.get("usage") or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                yield self._chunk(
                    model=ctx.upstream_model, role="assistant", chunk_id=message_id
                )
                continue

            if event_type == "content_block_start":
                block = parsed.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tool_index += 1
                    yield self._chunk(
                        model=ctx.upstream_model,
                        tool_calls=[
                            ToolCall(
                                id=str(block.get("id") or f"call_{tool_index}"),
                                index=tool_index,
                                function=FunctionCall(
                                    name=str(block.get("name") or ""), arguments=""
                                ),
                            )
                        ],
                        chunk_id=message_id,
                    )
                continue

            if event_type == "content_block_delta":
                delta = parsed.get("delta") or {}
                delta_type = delta.get("type")
                if delta_type == "text_delta" and delta.get("text"):
                    yield self._chunk(
                        model=ctx.upstream_model,
                        content=str(delta["text"]),
                        chunk_id=message_id,
                    )
                elif delta_type == "input_json_delta":
                    yield self._chunk(
                        model=ctx.upstream_model,
                        tool_calls=[
                            ToolCall(
                                id="",
                                index=tool_index,
                                function=FunctionCall(
                                    name="", arguments=str(delta.get("partial_json") or "")
                                ),
                            )
                        ],
                        chunk_id=message_id,
                    )
                continue

            if event_type == "message_delta":
                delta = parsed.get("delta") or {}
                if delta.get("stop_reason"):
                    finish_reason = self._normalize_finish_reason(delta["stop_reason"])
                usage = parsed.get("usage") or {}
                if usage.get("output_tokens") is not None:
                    output_tokens = int(usage["output_tokens"])
                continue

            if event_type == "message_stop":
                break

        usage = Usage.build(input_tokens, output_tokens)
        yield self._chunk(
            model=ctx.upstream_model,
            finish_reason=finish_reason or "stop",
            usage=usage,
            chunk_id=message_id,
        )

    async def list_models(self, credential: CredentialRuntime | None = None) -> list[str]:
        """Anthropic exposes ``GET /v1/models``; auth is the same key header.

        Anthropic-compatible third parties (StepFun, ...) often accept ``x-api-key``
        on ``/messages`` but only ``Authorization: Bearer`` on ``/models``, which
        would make the console's model marketplace show an auth error for a provider
        that works fine. Retry once with Bearer before giving up.
        """
        ctx = ProviderContext(
            request_id="model-list",
            deployment=DeploymentConfig(id="_probe", provider_id=self.provider_id, model=""),
            model=self._probe_model(),
            credential=credential,
            timeout=min(20.0, self.config.timeout),
        )
        import httpx

        from app.core.errors import ConnectionFailureError, GatewayTimeoutError

        try:
            response = await self.client.get(
                f"{self.base_url}/models",
                headers=self.headers(credential),
                timeout=self.request_timeout(ctx),
            )
            if response.status_code in {401, 403} and credential and credential.secret:
                bearer = dict(self._base_headers())
                bearer["Authorization"] = f"Bearer {credential.secret}"
                response = await self.client.get(
                    f"{self.base_url}/models",
                    headers=bearer,
                    timeout=self.request_timeout(ctx),
                )
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError("anthropic timeout", provider=self.provider_id) from exc
        except httpx.TransportError as exc:
            raise ConnectionFailureError("anthropic unreachable", provider=self.provider_id) from exc
        if response.status_code >= 400:
            self._raise_for_status(
                response.status_code, self._safe_json(response.content), dict(response.headers)
            )
        data = self._safe_json(response.content) or {}
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")]

    async def model_catalogue(
        self, credential: CredentialRuntime | None = None
    ) -> dict[str, dict[str, Any]]:
        """Same request as :meth:`list_models`, returning each entry with its extras.

        StepFun answers ``/models`` with ``max_input_tokens``,
        ``enable_vision_input``, ``enable_reason`` and
        ``reasoning_effort_support_list`` per model - exactly the facts the console
        needs to fill a form instead of asking the operator to know them.
        """
        ctx = ProviderContext(
            request_id="model-catalogue",
            deployment=DeploymentConfig(id="_probe", provider_id=self.provider_id, model=""),
            model=self._probe_model(),
            credential=credential,
            timeout=min(20.0, self.config.timeout),
        )
        import httpx

        from app.core.errors import ConnectionFailureError, GatewayTimeoutError

        async def fetch(headers: dict[str, str]) -> httpx.Response:
            return await self.client.get(
                f"{self.base_url}/models", headers=headers, timeout=self.request_timeout(ctx)
            )

        try:
            response = await fetch(self.headers(credential))
            if response.status_code in {401, 403} and credential and credential.secret:
                bearer = dict(self._base_headers())
                bearer["Authorization"] = f"Bearer {credential.secret}"
                response = await fetch(bearer)
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError("anthropic timeout", provider=self.provider_id) from exc
        except httpx.TransportError as exc:
            raise ConnectionFailureError("anthropic unreachable", provider=self.provider_id) from exc
        if response.status_code >= 400:
            self._raise_for_status(
                response.status_code, self._safe_json(response.content), dict(response.headers)
            )
        data = self._safe_json(response.content) or {}
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return {}
        return {
            str(item["id"]): item
            for item in items
            if isinstance(item, dict) and item.get("id")
        }

    def _probe_model(self):  # pragma: no cover - tiny helper for type checkers
        from app.models.provider import ModelConfig

        return ModelConfig(id="_probe")
