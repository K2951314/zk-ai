"""Google Gemini (Generative Language API) adapter.

Verified contract:

* ``POST {base}/models/{model}:generateContent``
* ``POST {base}/models/{model}:streamGenerateContent?alt=sse``
* base URL default ``https://generativelanguage.googleapis.com/v1beta``
* auth: ``x-goog-api-key: <key>`` header (``?key=`` also works; header preferred
  because query strings leak into proxy logs)
* body: ``contents`` (``[{role: "user"|"model", parts: [...]}]``),
  ``systemInstruction``, ``generationConfig`` (``temperature``, ``topP``,
  ``topK``, ``maxOutputTokens``, ``stopSequences``, ``responseMimeType``,
  ``responseSchema``), ``tools[].functionDeclarations[]``,
  ``toolConfig.functionCallingConfig.mode`` (``AUTO`` | ``ANY`` | ``NONE``)
* response: ``candidates[].content.parts[]`` (``text`` / ``functionCall``),
  ``candidates[].finishReason``, ``usageMetadata.{promptTokenCount,
  candidatesTokenCount, totalTokenCount}``

Capability gaps reported instead of silently dropped: ``presence_penalty``,
``frequency_penalty``, ``seed``, ``logprobs``, ``user``, ``n`` > 1,
``top_logprobs``. ``response_format`` maps onto ``responseMimeType`` /
``responseSchema``.
"""

from __future__ import annotations

import secrets
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

logger = get_logger("provider.gemini")

UNMAPPABLE = frozenset(
    {
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "logprobs",
        "top_logprobs",
        "logit_bias",
        "user",
        "n",
        "parallel_tool_calls",
    }
)

FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "MALFORMED_FUNCTION_CALL": "tool_calls",
    "OTHER": "stop",
}


class GeminiAdapter(ProviderAdapter):
    """Adapter for Google's Generative Language API."""

    provider_type = ProviderType.GEMINI
    openai_compatible = False
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
            "tools",
            "tool_choice",
            "response_format",
            "stream",
        }
    )

    def __init__(self, config) -> None:
        super().__init__(config)

    # ------------------------------------------------------------------ #
    # HTTP surface
    # ------------------------------------------------------------------ #
    def _auth_headers(self, credential: CredentialRuntime | None) -> dict[str, str]:
        if credential and credential.secret:
            return {"x-goog-api-key": credential.secret}
        return {}

    def _endpoint(self, model: str, *, stream: bool) -> str:
        method = "streamGenerateContent" if stream else "generateContent"
        return f"{self.base_url}/models/{model}:{method}"

    # ------------------------------------------------------------------ #
    # Payload construction
    # ------------------------------------------------------------------ #
    @staticmethod
    def _split_data_url(url: str) -> tuple[str, str] | None:
        if not url.startswith("data:"):
            return None
        header, _, payload = url.partition(",")
        mime = header[5:].split(";")[0] or "image/png"
        return mime, payload

    def _convert_parts(self, content: Any) -> list[dict[str, Any]]:
        """OpenAI content parts -> Gemini ``parts``."""
        if isinstance(content, str):
            return [{"text": content}] if content else []
        parts: list[dict[str, Any]] = []
        for part in content or []:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"text", "input_text"}:
                parts.append({"text": part.get("text", "")})
            elif part_type in {"image_url", "image", "input_image"}:
                image = part.get("image_url") or part.get("image") or {}
                url = image.get("url") if isinstance(image, dict) else str(image)
                if not isinstance(url, str):
                    continue
                inline = self._split_data_url(url)
                if inline:
                    mime, data = inline
                    parts.append({"inlineData": {"mimeType": mime, "data": data}})
                else:
                    parts.append({"fileData": {"fileUri": url}})
            else:
                parts.append({"text": str(part.get("text") or "")})
        return parts

    def _convert_contents(
        self, request: ChatCompletionRequest
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        system_chunks: list[str] = []
        contents: list[dict[str, Any]] = []
        # Gemini function-call ids are optional; keep a mapping for tool results.
        pending_names: dict[str, str] = {}

        for message in request.messages:
            if message.role in {"system", "developer"}:
                text = message.text()
                if text:
                    system_chunks.append(text)
                continue

            if message.role == "tool":
                name = pending_names.get(message.tool_call_id or "", message.name or "function")
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": name,
                                    "response": {"result": message.text()},
                                }
                            }
                        ],
                    }
                )
                continue

            role = "model" if message.role == "assistant" else "user"
            parts = self._convert_parts(message.content)
            for call in message.tool_calls or []:
                pending_names[call.id] = call.function.name
                parts.append(
                    {
                        "functionCall": {
                            "name": call.function.name,
                            "args": self._parse_json_object(call.function.arguments),
                        }
                    }
                )
            if not parts:
                parts = [{"text": " "}]
            contents.append({"role": role, "parts": parts})

        system_instruction = (
            {"parts": [{"text": "\n\n".join(system_chunks)}]} if system_chunks else None
        )
        return contents, system_instruction

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

    def _convert_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        declarations: list[dict[str, Any]] = []
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if not isinstance(function, dict):
                continue
            declaration: dict[str, Any] = {
                "name": function.get("name", "unnamed"),
                "description": function.get("description") or "",
            }
            parameters = function.get("parameters")
            if parameters:
                declaration["parameters"] = parameters
            declarations.append(declaration)
        return [{"functionDeclarations": declarations}] if declarations else None

    def _convert_tool_choice(self, choice: Any) -> dict[str, Any] | None:
        if choice is None:
            return None
        mode = {"auto": "AUTO", "required": "ANY", "any": "ANY", "none": "NONE"}
        if isinstance(choice, str):
            mapped = mode.get(choice)
            return {"functionCallingConfig": {"mode": mapped}} if mapped else None
        if isinstance(choice, dict) and choice.get("type") == "function":
            name = (choice.get("function") or {}).get("name")
            config: dict[str, Any] = {"mode": "ANY"}
            if name:
                config["allowedFunctionNames"] = [name]
            return {"functionCallingConfig": config}
        return None

    def _generation_config(
        self, request: ChatCompletionRequest, deployment: DeploymentConfig, unsupported: list[str]
    ) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if request.temperature is not None:
            config["temperature"] = request.temperature
        if request.top_p is not None:
            config["topP"] = request.top_p
        if request.top_k is not None:
            config["topK"] = request.top_k
        max_tokens = request.effective_max_tokens or deployment.max_output_tokens
        if max_tokens:
            config["maxOutputTokens"] = int(max_tokens)
        stop = request.stop
        if stop:
            config["stopSequences"] = [stop] if isinstance(stop, str) else list(stop)

        response_format = request.response_format or {}
        fmt = response_format.get("type")
        if fmt == "json_object":
            config["responseMimeType"] = "application/json"
        elif fmt == "json_schema":
            schema = (response_format.get("json_schema") or {}).get("schema")
            config["responseMimeType"] = "application/json"
            if schema:
                config["responseSchema"] = schema

        for key in ("presence_penalty", "frequency_penalty", "seed", "user", "logprobs"):
            if getattr(request, key, None) is not None:
                unsupported.append(key)
        if request.n and request.n > 1:
            unsupported.append("n")
        if request.parallel_tool_calls is not None:
            unsupported.append("parallel_tool_calls")
        return config

    def build_payload(
        self, request: ChatCompletionRequest, deployment: DeploymentConfig
    ) -> tuple[dict[str, Any], list[str]]:
        unsupported: list[str] = []
        contents, system_instruction = self._convert_contents(request)
        payload: dict[str, Any] = {"contents": contents}
        if system_instruction:
            payload["systemInstruction"] = system_instruction

        generation_config = self._generation_config(request, deployment, unsupported)
        if generation_config:
            payload["generationConfig"] = generation_config

        tools = self._convert_tools(request.tools)
        if tools:
            payload["tools"] = tools
            tool_config = self._convert_tool_choice(request.tool_choice)
            if tool_config:
                payload["toolConfig"] = tool_config

        unsupported.extend(request.extra_params().keys())
        payload.update(deployment.options)
        return payload, sorted(set(unsupported))

    # ------------------------------------------------------------------ #
    # Normalisation
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_finish(reason: str | None) -> str:
        if not reason:
            return "stop"
        return FINISH_REASON_MAP.get(reason.upper(), reason.lower())

    def normalize_response(self, payload: dict[str, Any]) -> ChatCompletionResponse:
        candidates = payload.get("candidates") or []
        if not candidates:
            feedback = payload.get("promptFeedback") or {}
            blocked = feedback.get("blockReason")
            if blocked:
                from app.core.errors import ContentFilterError

                raise ContentFilterError(f"Gemini blocked the prompt: {blocked}")
            raise BadGatewayError("Gemini returned no candidates", provider=self.provider_id)

        candidate = candidates[0]
        text_chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        for index, part in enumerate((candidate.get("content") or {}).get("parts") or []):
            if not isinstance(part, dict):
                continue
            if part.get("thought"):
                # Gemini thinking models (2.5+) return the chain of thought as
                # parts flagged ``thought: true`` — they must not leak into the
                # user-visible answer.
                continue
            if part.get("text"):
                text_chunks.append(str(part["text"]))
            elif part.get("functionCall"):
                call = part["functionCall"]
                tool_calls.append(
                    ToolCall(
                        id=f"call_{secrets.token_hex(6)}",
                        index=index,
                        function=FunctionCall(
                            name=str(call.get("name") or ""),
                            arguments=self._dumps(call.get("args") or {}),
                        ),
                    )
                )

        usage_raw = payload.get("usageMetadata") or {}
        usage = Usage.build(
            usage_raw.get("promptTokenCount"),
            usage_raw.get("candidatesTokenCount"),
            usage_raw.get("cachedContentTokenCount") or 0,
        )
        finish = self._normalize_finish(candidate.get("finishReason"))
        if tool_calls and finish == "stop":
            finish = "tool_calls"
        response = ChatCompletionResponse.simple(
            model=str(payload.get("modelVersion") or self.provider_id),
            content="".join(text_chunks),
            finish_reason=finish,
            usage=usage,
            tool_calls=tool_calls or None,
            provider_id=self.provider_id,
        )
        if payload.get("responseId"):
            response.id = str(payload["responseId"])
        return response

    @staticmethod
    def _dumps(value: Any) -> str:
        import json

        return json.dumps(value, ensure_ascii=False)

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    async def chat(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> ChatCompletionResponse:
        payload, unsupported = self.build_payload(request, ctx.deployment)
        if unsupported:
            logger.info(
                "provider=gemini unmapped params (model=%s): %s",
                ctx.upstream_model,
                ",".join(unsupported),
            )
        data = await self._post_json(self._endpoint(ctx.upstream_model, stream=False), payload, ctx=ctx)
        response = self.normalize_response(data)
        response.model = ctx.upstream_model
        return response

    async def stream(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> AsyncIterator[ChatCompletionChunk]:
        payload, unsupported = self.build_payload(request, ctx.deployment)
        if unsupported:
            logger.info(
                "provider=gemini unmapped params (stream, model=%s): %s",
                ctx.upstream_model,
                ",".join(unsupported),
            )
        url = self._endpoint(ctx.upstream_model, stream=True)
        emitted_role = False
        tool_index = -1
        finish_reason: str | None = None
        usage: Usage | None = None

        async for _event, data in self._stream_lines(
            url, payload, ctx=ctx, params={"alt": "sse"}
        ):
            parsed = self._safe_json(data)
            if not isinstance(parsed, dict):
                continue
            if parsed.get("error"):
                message = (parsed.get("error") or {}).get("message", "gemini stream error")
                raise BadGatewayError(str(message), provider=self.provider_id)
            feedback = parsed.get("promptFeedback") or {}
            if feedback.get("blockReason"):
                # Same safety gate as the non-streaming path: a blocked prompt
                # must surface as an error, not as a 200 with empty content.
                from app.core.errors import ContentFilterError

                raise ContentFilterError(
                    f"Gemini blocked the prompt: {feedback['blockReason']}",
                    provider=self.provider_id,
                )

            if not emitted_role:
                emitted_role = True
                yield self._chunk(model=ctx.upstream_model, role="assistant")

            for candidate in parsed.get("candidates") or []:
                for part in (candidate.get("content") or {}).get("parts") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("text"):
                        yield self._chunk(model=ctx.upstream_model, content=str(part["text"]))
                    elif part.get("functionCall"):
                        tool_index += 1
                        call = part["functionCall"]
                        yield self._chunk(
                            model=ctx.upstream_model,
                            tool_calls=[
                                ToolCall(
                                    id=f"call_{secrets.token_hex(6)}",
                                    index=tool_index,
                                    function=FunctionCall(
                                        name=str(call.get("name") or ""),
                                        arguments=self._dumps(call.get("args") or {}),
                                    ),
                                )
                            ],
                        )
                if candidate.get("finishReason"):
                    finish_reason = self._normalize_finish(candidate["finishReason"])

            if parsed.get("usageMetadata"):
                meta = parsed["usageMetadata"]
                usage = Usage.build(
                    meta.get("promptTokenCount"),
                    meta.get("candidatesTokenCount"),
                    meta.get("cachedContentTokenCount") or 0,
                )

        if tool_index >= 0 and (finish_reason in (None, "stop")):
            finish_reason = "tool_calls"
        yield self._chunk(
            model=ctx.upstream_model,
            finish_reason=finish_reason or "stop",
            usage=usage,
        )

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
                f"{self.base_url}/models",
                headers=self.headers(credential),
                timeout=self.request_timeout(ctx),
                params={"pageSize": 200},
            )
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError("gemini timeout", provider=self.provider_id) from exc
        except httpx.TransportError as exc:
            raise ConnectionFailureError("gemini unreachable", provider=self.provider_id) from exc
        if response.status_code >= 400:
            self._raise_for_status(
                response.status_code, self._safe_json(response.content), dict(response.headers)
            )
        data = self._safe_json(response.content) or {}
        models: list[str] = []
        for item in data.get("models") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            models.append(name.split("/")[-1] if name else "")
        return [m for m in models if m]
