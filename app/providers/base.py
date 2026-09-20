"""Provider abstraction.

``ProviderAdapter`` is the single seam between the router and the outside world.
Every adapter must:

* hold **no** credentials itself (they come from the pool, per call);
* translate an OpenAI-shaped :class:`ChatCompletionRequest` into the provider's
  native payload and report parameters it could not map;
* translate native responses / errors back into the canonical shapes.

The canonical shapes are the OpenAI ones, which makes ``/v1/chat/completions``
compatibility a property of the adapters rather than of the API layer.
"""

from __future__ import annotations

import abc
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.errors import (
    BadGatewayError,
    ConnectionFailureError,
    GatewayTimeoutError,
    UpstreamUnknownError,
    ZKAIError,
)
from app.core.logging import get_logger
from app.core.security import redact_mapping
from app.models.credential import CredentialRuntime
from app.models.provider import DeploymentConfig, ModelConfig, ProviderConfig, ProviderType
from app.models.request import ChatCompletionRequest
from app.models.response import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionResponse,
    ChunkDelta,
    Usage,
)
from app.retry.classifier import ErrorClassifier, ErrorInfo

logger = get_logger("provider")


def _sse_buffer_complete(lines: list[str]) -> bool:
    """True when the buffered ``data:`` lines already form a complete SSE payload.

    Used to flush a buffered event when the *next* ``data:`` line arrives without
    a blank separator: a buffer that parses as JSON (or is ``[DONE]``) is by
    definition complete, so the new line starts a fresh event.
    """
    text = "\n".join(lines).strip()
    if text == "[DONE]":
        return True
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


@dataclass(slots=True)
class ProviderContext:
    """Everything an adapter needs to know about the current attempt."""

    request_id: str
    deployment: DeploymentConfig
    model: ModelConfig
    credential: CredentialRuntime | None = None
    timeout: float = 60.0
    stream: bool = False
    attempt: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def upstream_model(self) -> str:
        return self.deployment.model

    @property
    def credential_id(self) -> str | None:
        return self.credential.id if self.credential else None

    @property
    def credential_secret(self) -> str | None:
        return self.credential.secret if self.credential else None


@dataclass(slots=True)
class HealthCheckResult:
    """Outcome of a provider/credential probe."""

    provider_id: str
    ok: bool
    credential_id: str | None = None
    latency_ms: float = 0.0
    models: list[str] = field(default_factory=list)
    error_type: str | None = None
    detail: str | None = None
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "credential_id": self.credential_id,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 2),
            "models": self.models[:50],
            "error_type": self.error_type,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


class UnsupportedParams(Exception):
    """Raised by callers that opt into strict parameter handling."""

    def __init__(self, params: list[str], provider_id: str) -> None:
        super().__init__(
            f"provider '{provider_id}' cannot map parameters: {', '.join(sorted(params))}"
        )
        self.params = params


class ProviderAdapter(abc.ABC):
    """Base class for every provider implementation."""

    provider_type: ProviderType = ProviderType.OPENAI_COMPATIBLE
    #: Parameters the adapter knows how to map. ``None`` means "everything it
    #: receives", subclasses should override with an explicit set.
    supported_params: frozenset[str] = frozenset()
    supports_streaming: bool = True
    #: True when the provider can also reach the OpenAI wire protocol.
    openai_compatible: bool = False

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.provider_id = config.id
        self.base_url = config.base_url.rstrip("/")
        self.classifier = ErrorClassifier()
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> httpx.AsyncClient:
        """Lazily created shared ``httpx.AsyncClient`` (connection pooling)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_config(self.config.timeout),
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
                follow_redirects=True,
                headers={"user-agent": "ZK-AI/0.1 (+personal-ai-gateway)"},
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    @staticmethod
    def _timeout_config(seconds: float) -> httpx.Timeout:
        """Connect timeout is short; read timeout carries the long generation."""
        return httpx.Timeout(seconds, connect=min(10.0, seconds))

    def request_timeout(self, ctx: ProviderContext) -> httpx.Timeout:
        """Per-attempt timeout (streams use a read timeout as an idle guard).

        The provider's configured ``timeout`` wins over the process-wide
        ``request_timeout``: reasoning models (K3, GLM-5.2) legitimately run for
        minutes, and a global default - or a smoke-test override - must not
        silently cap them. The larger of the two is the effective read timeout.
        """
        if ctx.stream:
            return httpx.Timeout(
                self.config.timeout,
                connect=min(10.0, self.config.connect_timeout),
                read=self.config.timeout,
            )
        timeout = max(ctx.timeout or 0.0, self.config.timeout)
        return httpx.Timeout(timeout, connect=min(10.0, self.config.connect_timeout))

    # ------------------------------------------------------------------ #
    # Interface
    # ------------------------------------------------------------------ #
    @abc.abstractmethod
    async def chat(
        self,
        request: ChatCompletionRequest,
        ctx: ProviderContext,
    ) -> ChatCompletionResponse:
        """Execute a non-streaming completion."""

    @abc.abstractmethod
    def stream(
        self,
        request: ChatCompletionRequest,
        ctx: ProviderContext,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Execute a streaming completion (async generator)."""

    @abc.abstractmethod
    async def list_models(self, credential: CredentialRuntime | None = None) -> list[str]:
        """List provider-native model identifiers."""

    async def model_catalogue(
        self, credential: CredentialRuntime | None = None
    ) -> dict[str, dict[str, Any]]:
        """Model ids mapped to the raw upstream entry, when the provider offers one.

        The default keeps only the ids (most providers answer with nothing more),
        so callers must not assume an entry exists. Adapters that receive rich
        per-model metadata (context window, vision, reasoning…) override this so
        the console can fill a form from measured data instead of a guess.
        """
        return {model_id: {} for model_id in await self.list_models(credential)}

    @abc.abstractmethod
    def build_payload(
        self, request: ChatCompletionRequest, deployment: DeploymentConfig
    ) -> tuple[dict[str, Any], list[str]]:
        """Return ``(native_payload, unsupported_parameter_names)``."""

    @abc.abstractmethod
    def normalize_response(self, payload: dict[str, Any]) -> ChatCompletionResponse:
        """Convert a native non-streaming payload into the canonical shape."""

    # ------------------------------------------------------------------ #
    # Thinking / answer recovery (protocol-independent)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _recover_reasoning_only_content(response: ChatCompletionResponse) -> None:
        """Substitute the thinking text when a reasoning model produced no answer.

        Reasoning models (SenseNova ``*-flash-lite``, DeepSeek ``*-pro``, Kimi K3,
        GLM-5.2, StepFun ``step-*`` …) emit their chain of thought first and the
        answer second. When the answer is cut off by ``max_tokens``, ``content``
        comes back as an empty string while the thinking sits in ``reasoning`` /
        ``reasoning_content``. Returning that empty string silently is a bad
        failure mode - the caller sees HTTP 200 with nothing in it. Surface the
        reasoning instead and mark the finish reason so the truncation stays visible.
        """
        for choice in response.choices:
            message = choice.message
            if message.content:
                continue
            # A tool-call turn legitimately has no text: the answer *is* the call.
            # Promoting the thinking here would staple commentary onto a tool call.
            # Keep it as ``reasoning`` - Anthropic clients expect a thinking block
            # ahead of ``tool_use``, and the strip switch removes it on demand.
            if message.tool_calls:
                continue
            extra = message.model_extra
            if not extra:
                continue
            reasoning = extra.get("reasoning") or extra.get("reasoning_content")
            if not isinstance(reasoning, str) or not reasoning.strip():
                continue
            message.content = reasoning
            extra["content_recovered_from_reasoning"] = True
            # Text moved into ``content``; drop the source fields so clients
            # that read both do not render the same text twice.
            extra.pop("reasoning_content", None)
            extra.pop("reasoning", None)
            if choice.finish_reason is None:
                choice.finish_reason = "length"

    @staticmethod
    def _recover_reasoning_only_chunks(chunk: ChatCompletionChunk) -> None:
        """Surface ``reasoning_content`` when a provider streams no ``content``.

        ModelScope and SenseNova put the whole answer in ``reasoning_content``
        during streaming and leave ``delta.content`` empty. Without this the
        client receives a stream of empty deltas and renders nothing, even
        though the upstream call succeeded. Mirrors the non-streaming
        :meth:`_recover_reasoning_only_content` behaviour.
        """
        for choice in chunk.choices:
            choice.recover_reasoning_only_delta()

    # ------------------------------------------------------------------ #
    # Optional overrides
    # ------------------------------------------------------------------ #
    async def health_check(self, credential: CredentialRuntime | None = None) -> HealthCheckResult:
        """Cheap probe: list models and measure latency."""
        started = time.perf_counter()
        try:
            models = await self.list_models(credential)
        except ZKAIError as exc:
            return HealthCheckResult(
                provider_id=self.provider_id,
                credential_id=credential.id if credential else None,
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_type=exc.error_type,
                detail=exc.message,
            )
        return HealthCheckResult(
            provider_id=self.provider_id,
            credential_id=credential.id if credential else None,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000,
            models=models,
        )

    def normalize_error(self, exc: BaseException) -> ZKAIError:
        """Translate any exception into the gateway's error hierarchy."""
        info = self.classifier.classify(exc)
        return info.to_error(provider=self.provider_id)

    # ------------------------------------------------------------------ #
    # Helpers shared by HTTP adapters
    # ------------------------------------------------------------------ #
    def _base_headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "application/json"}
        headers.update(self.config.headers)
        return headers

    @abc.abstractmethod
    def _auth_headers(self, credential: CredentialRuntime | None) -> dict[str, str]:
        """Provider specific authentication headers."""

    def headers(self, credential: CredentialRuntime | None) -> dict[str, str]:
        headers = self._base_headers()
        headers.update(self._auth_headers(credential))
        return headers

    def _log_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Redacted payload used in debug logs (never contains keys)."""
        safe = redact_mapping(payload)
        if isinstance(safe, dict) and isinstance(safe.get("messages"), list):
            safe["messages"] = f"<{len(safe['messages'])} messages>"
        return safe if isinstance(safe, dict) else {}

    def _raise_for_status(
        self,
        status: int,
        body: Any,
        headers: dict[str, str] | None = None,
        *,
        deployment: DeploymentConfig | None = None,
    ) -> None:
        """Raise a typed :class:`ZKAIError` for a non-2xx upstream response."""
        if 200 <= status < 300:
            return
        info: ErrorInfo = self.classifier.classify_status(status, body=body, headers=headers)
        error = info.to_error(
            provider=self.provider_id,
            model=deployment.model if deployment else None,
        )
        # Attach the upstream detail for diagnostics; the scheduler classifies the
        # exception itself, so no routing hints are smuggled through the object.
        error.raw = {"upstream_status": status, "error_type": info.error_type}
        raise error

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        ctx: ProviderContext,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST helper with unified error + timeout handling."""
        try:
            response = await self.client.post(
                url,
                json=payload,
                headers=self.headers(ctx.credential),
                timeout=self.request_timeout(ctx),
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(
                f"供应商 {self.provider_id} 超过 {self.config.timeout} 秒无响应（超时）：{exc}",
                provider=self.provider_id,
                model=ctx.upstream_model,
            ) from exc
        except httpx.TransportError as exc:
            raise ConnectionFailureError(
                f"供应商 {self.provider_id} 连接失败：{exc}",
                provider=self.provider_id,
                model=ctx.upstream_model,
            ) from exc

        if response.status_code >= 400:
            body = self._safe_json(await response.aread())
            self._raise_for_status(
                response.status_code,
                body,
                dict(response.headers),
                deployment=ctx.deployment,
            )
        data = self._safe_json(response.content)
        if data is None:
            raise BadGatewayError(
                f"供应商 {self.provider_id} 返回了非 JSON 内容",
                provider=self.provider_id,
            )
        return data

    def _open_stream(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        ctx: ProviderContext,
        params: dict[str, Any] | None = None,
        method: str = "POST",
    ) -> httpx.Response:
        """Placeholder documenting intent; see :meth:`_stream_lines`."""
        raise NotImplementedError

    async def _stream_lines(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        ctx: ProviderContext,
        params: dict[str, Any] | None = None,
        method: str = "POST",
    ) -> AsyncIterator[tuple[str | None, str]]:
        """Yield ``(event, data)`` pairs from an SSE / NDJSON response.

        The caller must consume this generator to completion or close it; both
        paths release the upstream connection (``async with`` inside).
        """
        request = self.client.build_request(
            method,
            url,
            json=payload,
            headers={**self.headers(ctx.credential), "accept": "text/event-stream"},
            timeout=self.request_timeout(ctx),
            params=params,
        )
        try:
            response = await self.client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(
                f"供应商 {self.provider_id} 建立流式连接超时：{exc}",
                provider=self.provider_id,
                model=ctx.upstream_model,
            ) from exc
        except httpx.TransportError as exc:
            raise ConnectionFailureError(
                f"供应商 {self.provider_id} 流式连接失败：{exc}",
                provider=self.provider_id,
                model=ctx.upstream_model,
            ) from exc

        try:
            if response.status_code >= 400:
                body = self._safe_json(await response.aread())
                self._raise_for_status(
                    response.status_code,
                    body,
                    dict(response.headers),
                    deployment=ctx.deployment,
                )
            event: str | None = None
            event_buf: str | None = None
            data_buf: list[str] = []
            # Mid-stream failures are the common failure mode for long
            # generations; keep them classified (typed) instead of leaking raw
            # httpx exceptions past the error classifier.
            try:
                async for line in response.aiter_lines():
                    line = line.rstrip("\r")
                    if not line:
                        event = None
                        if data_buf:
                            # SSE multi-line `data:` fields concatenate with \n.
                            yield event_buf, "\n".join(data_buf)
                            data_buf.clear()
                        continue
                    if line.startswith(":"):
                        continue  # SSE comment / keep-alive
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if line.startswith("data:"):
                        # Two layouts exist in the wild: spec-compliant SSE puts a
                        # blank line between events, but some providers emit
                        # back-to-back `data:` lines with no blank. Buffer until a
                        # blank line *or* until the buffer already holds a complete
                        # event (valid JSON / [DONE]) — then flush before starting
                        # the next one. Multi-line `data:` fields join with \n.
                        if data_buf and _sse_buffer_complete(data_buf):
                            yield event_buf, "\n".join(data_buf)
                            data_buf.clear()
                        data_buf.append(line[5:].strip())
                        event_buf = event
                        continue
                    # NDJSON (Ollama native streaming) has no prefix.
                    yield None, line.strip()
            except httpx.TimeoutException as exc:
                raise GatewayTimeoutError(
                    f"供应商 {self.provider_id} 流式读取中途超时：{exc}",
                    provider=self.provider_id,
                    model=ctx.upstream_model,
                ) from exc
            except httpx.TransportError as exc:
                raise ConnectionFailureError(
                    f"供应商 {self.provider_id} 流式连接中断：{exc}",
                    provider=self.provider_id,
                    model=ctx.upstream_model,
                ) from exc
            if data_buf:
                # Flush a final event that never got its terminating blank line.
                yield event_buf, "\n".join(data_buf)
        finally:
            await response.aclose()

    @staticmethod
    def _safe_json(raw: bytes | str | None) -> Any:
        """Parse a body defensively - never raise on malformed upstream data."""
        if raw is None:
            return None
        if isinstance(raw, bytes):
            if not raw:
                return None
            raw = raw.decode("utf-8", errors="replace")
        text = raw.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text[:2000]

    # ------------------------------------------------------------------ #
    # Streaming helpers
    # ------------------------------------------------------------------ #
    def _chunk(
        self,
        *,
        model: str,
        content: str | None = None,
        role: str | None = None,
        finish_reason: str | None = None,
        tool_calls: list[Any] | None = None,
        usage: Usage | None = None,
        chunk_id: str | None = None,
    ) -> ChatCompletionChunk:
        chunk = ChatCompletionChunk(
            model=model,
            choices=[
                ChatCompletionChunkChoice(
                    index=0,
                    delta=ChunkDelta(role=role, content=content, tool_calls=tool_calls),
                    finish_reason=finish_reason,
                )
            ],
            usage=usage,
        )
        if chunk_id:
            chunk.id = chunk_id
        return chunk

    @staticmethod
    def _parse_arguments(raw: Any) -> str:
        """Tool call arguments must always be a JSON string on the wire."""
        if raw is None:
            return "{}"
        if isinstance(raw, str):
            return raw
        try:
            return json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return "{}"

    @staticmethod
    def _normalize_finish_reason(reason: str | None) -> str | None:
        """Map provider finish reasons onto the OpenAI vocabulary."""
        if reason is None:
            return None
        mapping = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
            "max_output_tokens": "length",
            "tool_use": "tool_calls",
            "function_call": "tool_calls",
            "content_filter": "content_filter",
            "safety": "content_filter",
            "recitation": "content_filter",
            "length": "length",
            "stop": "stop",
            "tool_calls": "tool_calls",
        }
        return mapping.get(reason, reason)


class OpenAICompatibleAdapter(ProviderAdapter):
    """Shared implementation for the OpenAI ``/chat/completions`` wire format.

    Used directly by the OpenAI provider and subclassed by OpenRouter (which adds
    attribution headers) and by ``openai_compatible`` custom endpoints.
    """

    provider_type = ProviderType.OPENAI
    openai_compatible = True
    supported_params = frozenset(
        {
            "model",
            "messages",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "stream",
            "stream_options",
            "tools",
            "tool_choice",
            "response_format",
            "stop",
            "seed",
            "presence_penalty",
            "frequency_penalty",
            "n",
            "user",
            "parallel_tool_calls",
        }
    )

    def _auth_headers(self, credential: CredentialRuntime | None) -> dict[str, str]:
        if credential and credential.secret:
            return {"authorization": f"Bearer {credential.secret}"}
        if self.config.requires_credential:
            return {}
        return {}

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"

    # ------------------------------------------------------------------ #
    def build_payload(
        self, request: ChatCompletionRequest, deployment: DeploymentConfig
    ) -> tuple[dict[str, Any], list[str]]:
        payload: dict[str, Any] = {
            "model": deployment.model,
            "messages": [message.model_dump(exclude_none=True) for message in request.messages],
        }
        unsupported: list[str] = []

        optional = {
            "temperature": request.temperature,
            "top_p": request.top_p,
            "stop": request.stop,
            "seed": request.seed,
            "presence_penalty": request.presence_penalty,
            "frequency_penalty": request.frequency_penalty,
            "n": request.n,
            "user": request.user,
            "tools": request.tools,
            "tool_choice": request.tool_choice,
            "response_format": request.response_format,
            "parallel_tool_calls": request.parallel_tool_calls,
        }
        for key, value in optional.items():
            if value is None:
                continue
            payload[key] = value
            if key not in self.allowed_params(deployment):
                unsupported.append(key)

        max_tokens = request.effective_max_tokens
        if max_tokens is not None:
            token_param = self.token_param_name(deployment)
            payload[token_param] = max_tokens
            if token_param not in self.allowed_params(deployment):
                unsupported.append(token_param)
        elif deployment.max_output_tokens:
            payload[self.token_param_name(deployment)] = deployment.max_output_tokens

        if request.stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}

        if request.tool_choice is not None and not request.tools:
            # ``tool_choice`` without tools is rejected by several providers —
            # drop it rather than let the request fail upstream.
            payload.pop("tool_choice", None)

        payload.update(deployment.options)

        extra = request.extra_params()
        if extra:
            unsupported.extend(extra.keys())
            for key, value in extra.items():
                payload.setdefault(key, value)
        return payload, sorted(set(unsupported))

    def allowed_params(self, deployment: DeploymentConfig) -> set[str]:
        """Which parameters this deployment accepts."""
        if deployment.supported_params is not None:
            return set(deployment.supported_params)
        return set(self.supported_params)

    def token_param_name(self, deployment: DeploymentConfig) -> str:
        """``max_tokens`` unless the deployment opts into the newer naming."""
        return str(deployment.options.get("max_tokens_param", "max_tokens"))

    # ------------------------------------------------------------------ #
    async def chat(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> ChatCompletionResponse:
        payload, unsupported = self.build_payload(request, ctx.deployment)
        if unsupported:
            logger.info(
                "provider=%s model=%s unmapped params: %s",
                self.provider_id,
                ctx.upstream_model,
                ",".join(unsupported),
            )
        data = await self._post_json(self.chat_completions_url, payload, ctx=ctx)
        response = self.normalize_response(data)
        response.model = ctx.upstream_model
        response.system_fingerprint = response.system_fingerprint or f"zkai-{self.provider_id}"
        return response

    async def stream(
        self, request: ChatCompletionRequest, ctx: ProviderContext
    ) -> AsyncIterator[ChatCompletionChunk]:
        payload, unsupported = self.build_payload(request, ctx.deployment)
        if unsupported:
            logger.info(
                "provider=%s model=%s unmapped params (stream): %s",
                self.provider_id,
                ctx.upstream_model,
                ",".join(unsupported),
            )
        payload["stream"] = True
        payload.setdefault("stream_options", {"include_usage": True})

        async for _event, data in self._stream_lines(self.chat_completions_url, payload, ctx=ctx):
            if data == "[DONE]":
                break
            parsed = self._safe_json(data)
            if not isinstance(parsed, dict):
                continue
            if parsed.get("error"):
                # A mid-stream ``{"error": ...}`` carries no HTTP status; map the
                # body's own error type so a 429-looking failure still cools the
                # credential instead of being treated as a bad-gateway blip.
                body_error = parsed.get("error")
                hint = ""
                if isinstance(body_error, dict):
                    hint = str(body_error.get("type") or body_error.get("code") or "").lower()
                status = 429 if "rate_limit" in hint or "too_many" in hint else 502
                info = self.classifier.classify_status(
                    status, body=parsed, message=self.classifier._extract_message(parsed)
                )
                raise info.to_error(provider=self.provider_id, model=ctx.upstream_model)
            chunk = ChatCompletionChunk.model_validate(parsed)
            self._recover_reasoning_only_chunks(chunk)
            yield chunk

    async def list_models(self, credential: CredentialRuntime | None = None) -> list[str]:
        ctx = ProviderContext(
            request_id="model-list",
            deployment=DeploymentConfig(id="_probe", provider_id=self.provider_id, model=""),
            model=ModelConfig(id="_probe"),
            credential=credential,
            timeout=min(20.0, self.config.timeout),
        )
        try:
            response = await self.client.get(
                self.models_url,
                headers=self.headers(credential),
                timeout=self.request_timeout(ctx),
            )
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(f"HTTP {self.provider_id} timeout", provider=self.provider_id) from exc
        except httpx.TransportError as exc:
            raise ConnectionFailureError(
                f"HTTP {self.provider_id} unreachable", provider=self.provider_id
            ) from exc
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
        """Same request as :meth:`list_models`, but keeps each entry's extra fields.

        Providers that add per-model metadata beyond ``id`` (context window, vision
        support, supported parameters) therefore cost no second round-trip: the
        console reads it straight off the response. Anything not announced simply
        maps to ``{}``, which callers treat as "unknown, fall back to presets".
        """
        ctx = ProviderContext(
            request_id="model-catalogue",
            deployment=DeploymentConfig(id="_probe", provider_id=self.provider_id, model=""),
            model=ModelConfig(id="_probe"),
            credential=credential,
            timeout=min(20.0, self.config.timeout),
        )
        try:
            response = await self.client.get(
                self.models_url,
                headers=self.headers(credential),
                timeout=self.request_timeout(ctx),
            )
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(f"HTTP {self.provider_id} timeout", provider=self.provider_id) from exc
        except httpx.TransportError as exc:
            raise ConnectionFailureError(
                f"HTTP {self.provider_id} unreachable", provider=self.provider_id
            ) from exc
        if response.status_code >= 400:
            self._raise_for_status(
                response.status_code, self._safe_json(response.content), dict(response.headers)
            )
        data = self._safe_json(response.content) or {}
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return {}
        catalogue: dict[str, dict[str, Any]] = {}
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                catalogue[str(item["id"])] = item
        return catalogue

    # ------------------------------------------------------------------ #
    def normalize_response(self, payload: dict[str, Any]) -> ChatCompletionResponse:
        """OpenAI payloads already match the canonical shape - validate + pass through."""
        try:
            response = ChatCompletionResponse.model_validate(payload)
        except Exception as exc:  # pragma: no cover - malformed upstream
            raise UpstreamUnknownError(
                f"cannot parse response from provider '{self.provider_id}': {exc}",
                provider=self.provider_id,
            ) from exc
        self._recover_reasoning_only_content(response)
        return response
