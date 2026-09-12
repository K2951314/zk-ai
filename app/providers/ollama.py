"""Ollama (local models) adapter.

Verified contract (docs: ``/api/chat``):

* ``POST {base}/api/chat`` - base URL default ``http://localhost:11434``
* no API key required; an ``Authorization: Bearer`` header is still forwarded when
  a credential is configured (useful for Ollama behind a reverse proxy)
* body: ``model``, ``messages`` (``role``: system|user|assistant|tool, plus
  ``images`` for vision), ``stream`` (default true), ``format`` (``"json"`` or a
  JSON schema), ``options`` (``temperature``, ``top_p``, ``num_predict``,
  ``stop``, ``num_ctx``, ``seed``), ``tools``, ``keep_alive``
* streaming is **NDJSON**, not SSE: one JSON object per line, terminated by a
  line with ``done: true`` that also carries ``prompt_eval_count`` / ``eval_count``
* ``GET /api/tags`` lists local models

Capability gaps reported: ``top_k`` (Ollama uses ``top_k`` inside ``options``,
so it *is* mapped), ``presence_penalty``, ``frequency_penalty``, ``logprobs``,
``user``, ``n`` > 1.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.core.errors import BadGatewayError
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

logger = get_logger("provider.ollama")

UNMAPPABLE = frozenset(
    {
        "presence_penalty",
        "frequency_penalty",
        "logprobs",
        "top_logprobs",
        "logit_bias",
        "user",
        "n",
        "parallel_tool_calls",
    }
)


class OllamaAdapter(ProviderAdapter):
    """Adapter for a local Ollama server."""

    provider_type = ProviderType.OLLAMA
    openai_compatible = True  # Ollama also serves /v1/chat/completions
    supported_params = frozenset(
        {
            "model",
            "messages",
            "temperature",
            "top_p",
            "top_k",
            "max_tokens",
            "max_completion_tokens",
            "stop",
            "seed",
            "tools",
            "response_format",
            "stream",
        }
    )

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/api/chat"

    @property
    def tags_url(self) -> str:
        return f"{self.base_url}/api/tags"

    def _auth_headers(self, credential: CredentialRuntime | None) -> dict[str, str]:
        if credential and credential.secret:
            return {"authorization": f"Bearer {credential.secret}"}
        return {}

    # ------------------------------------------------------------------ #
    # Payload
    # ------------------------------------------------------------------ #
    def _convert_messages(self, request: ChatCompletionRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            item: dict[str, Any] = {"role": message.role, "content": message.text()}
            if message.role == "tool" and message.tool_call_id:
                item["tool_name"] = message.name or "function"
            images: list[str] = []
            if isinstance(message.content, list):
                for part in message.content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in {"image_url", "image", "input_image"}:
                        image = part.get("image_url") or part.get("image") or {}
                        url = image.get("url") if isinstance(image, dict) else str(image)
                        if isinstance(url, str) and url.startswith("data:"):
                            images.append(url.partition(",")[2])
                        elif isinstance(url, str) and url.startswith(("http://", "https://")):
                            # Remote URLs can't be forwarded as-is: surface the drop
                            # instead of answering without ever seeing the image.
                            logger.warning(
                                "ollama: dropping remote image_url %s (only data: URLs are supported; "
                                "fetch it and inline it as base64 first)",
                                url[:120],
                            )
            if images:
                item["images"] = images
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "function": {
                            "name": call.function.name,
                            "arguments": self._parse_json_object(call.function.arguments),
                        }
                    }
                    for call in message.tool_calls
                ]
            messages.append(item)
        return messages

    @staticmethod
    def _parse_json_object(raw: str | None) -> dict[str, Any]:
        import json

        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"_raw": raw}

    def build_payload(
        self, request: ChatCompletionRequest, deployment: DeploymentConfig
    ) -> tuple[dict[str, Any], list[str]]:
        unsupported: list[str] = []
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.top_p is not None:
            options["top_p"] = request.top_p
        if request.top_k is not None:
            options["top_k"] = request.top_k
        if request.seed is not None:
            options["seed"] = request.seed
        if request.stop:
            options["stop"] = [request.stop] if isinstance(request.stop, str) else request.stop
        max_tokens = request.effective_max_tokens or deployment.max_output_tokens
        if max_tokens:
            options["num_predict"] = int(max_tokens)

        payload: dict[str, Any] = {
            "model": deployment.model,
            "messages": self._convert_messages(request),
            "stream": bool(request.stream),
        }
        if options:
            payload["options"] = options
        if request.tools:
            payload["tools"] = request.tools
        response_format = request.response_format or {}
        if response_format.get("type") == "json_object":
            payload["format"] = "json"
        elif response_format.get("type") == "json_schema":
            payload["format"] = (response_format.get("json_schema") or {}).get("schema") or "json"

        for key in ("presence_penalty", "frequency_penalty", "logprobs", "user"):
            if getattr(request, key, None) is not None:
                unsupported.append(key)
        if request.n and request.n > 1:
            unsupported.append("n")
        if request.parallel_tool_calls is not None:
            unsupported.append("parallel_tool_calls")

        unsupported.extend(request.extra_params().keys())
        payload.update(deployment.options)
        return payload, sorted(set(unsupported))

    # ------------------------------------------------------------------ #
    # Normalisation
    # ------------------------------------------------------------------ #
    def _normalize_tool_calls(self, raw_calls: Any) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index, call in enumerate(raw_calls or []):
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            calls.append(
                ToolCall(
                    id=str(call.get("id") or f"call_{index}"),
                    index=index,
                    function=FunctionCall(
                        name=str(function.get("name") or ""),
                        arguments=self._parse_arguments(function.get("arguments")),
                    ),
                )
            )
        return calls

    def normalize_response(self, payload: dict[str, Any]) -> ChatCompletionResponse:
        if payload.get("error"):
            raise BadGatewayError(str(payload["error"]), provider=self.provider_id)
        message = payload.get("message") or {}
        tool_calls = self._normalize_tool_calls(message.get("tool_calls"))
        usage = Usage.build(payload.get("prompt_eval_count"), payload.get("eval_count"))
        finish = self._normalize_finish_reason(payload.get("done_reason"))
        if tool_calls and not finish:
            finish = "tool_calls"
        return ChatCompletionResponse.simple(
            model=str(payload.get("model") or self.provider_id),
            content=str(message.get("content") or ""),
            finish_reason=finish or ("tool_calls" if tool_calls else "stop"),
            usage=usage,
            tool_calls=tool_calls or None,
            provider_id=self.provider_id,
        )

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    async def chat(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> ChatCompletionResponse:
        payload, unsupported = self.build_payload(request, ctx.deployment)
        payload["stream"] = False
        if unsupported:
            logger.info(
                "provider=ollama unmapped params (model=%s): %s",
                ctx.upstream_model,
                ",".join(unsupported),
            )
        data = await self._post_json(self.chat_url, payload, ctx=ctx)
        response = self.normalize_response(data)
        response.model = ctx.upstream_model
        return response

    async def stream(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> AsyncIterator[ChatCompletionChunk]:
        payload, unsupported = self.build_payload(request, ctx.deployment)
        payload["stream"] = True
        if unsupported:
            logger.info(
                "provider=ollama unmapped params (stream, model=%s): %s",
                ctx.upstream_model,
                ",".join(unsupported),
            )
        emitted_role = False
        finish_reason: str | None = None
        usage = Usage()

        async for _event, data in self._stream_lines(self.chat_url, payload, ctx=ctx):
            if not data:
                continue
            parsed = self._safe_json(data)
            if not isinstance(parsed, dict):
                continue
            if parsed.get("error"):
                raise BadGatewayError(str(parsed["error"]), provider=self.provider_id)
            if not emitted_role:
                emitted_role = True
                yield self._chunk(model=ctx.upstream_model, role="assistant")

            message = parsed.get("message") or {}
            if message.get("content"):
                yield self._chunk(model=ctx.upstream_model, content=str(message["content"]))
            tool_calls = self._normalize_tool_calls(message.get("tool_calls"))
            if tool_calls:
                yield self._chunk(model=ctx.upstream_model, tool_calls=tool_calls)

            if parsed.get("done"):
                finish_reason = self._normalize_finish_reason(parsed.get("done_reason"))
                usage = Usage.build(parsed.get("prompt_eval_count"), parsed.get("eval_count"))
                break

        if not finish_reason:
            finish_reason = "stop"
        yield self._chunk(model=ctx.upstream_model, finish_reason=finish_reason, usage=usage)

    async def list_models(self, credential: CredentialRuntime | None = None) -> list[str]:
        import httpx

        from app.core.errors import ConnectionFailureError, GatewayTimeoutError
        from app.models.provider import ModelConfig

        ctx = ProviderContext(
            request_id="model-list",
            deployment=DeploymentConfig(id="_probe", provider_id=self.provider_id, model=""),
            model=ModelConfig(id="_probe"),
            credential=credential,
        )
        try:
            response = await self.client.get(
                self.tags_url,
                headers=self.headers(credential),
                timeout=self.request_timeout(ctx),
            )
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError("ollama timeout", provider=self.provider_id) from exc
        except httpx.TransportError as exc:
            raise ConnectionFailureError("ollama unreachable", provider=self.provider_id) from exc
        if response.status_code >= 400:
            self._raise_for_status(
                response.status_code, self._safe_json(response.content), dict(response.headers)
            )
        data = self._safe_json(response.content) or {}
        models: list[str] = []
        for item in data.get("models") or []:
            if isinstance(item, dict):
                name = item.get("model") or item.get("name")
                if name:
                    models.append(str(name))
        return models
