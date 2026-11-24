"""Provider adapter behaviour that only shows up against real endpoints.

Every case here was found by pointing the gateway at a *real* OpenAI-compatible
provider (SenseNova) - the mock upstream answers exactly what the adapters
expect, so these shapes never appeared in the offline suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import AuthenticationError, ModelNotFoundError
from app.models.provider import DeploymentConfig, ProviderConfig
from app.providers.base import OpenAICompatibleAdapter


@pytest.fixture
def adapter() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        ProviderConfig(id="sensenova", type="openai_compatible", base_url="https://example.invalid/v1")
    )


def _payload(content: str | None, *, reasoning: str | None = None, finish: str | None = "stop") -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "sensenova-6.8-flash-lite",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class TestReasoningOnlyResponses:
    """A reasoning model whose answer was truncated leaves ``content`` empty.

    Observed live: ``sensenova-6.8-flash-lite`` with ``max_tokens=80`` returns
    ``content: ""``, ``finish_reason: "length"`` and the chain of thought in
    ``reasoning``. Returning that verbatim gives the caller HTTP 200 with an
    empty string and no explanation.
    """

    def test_reasoning_is_surfaced_when_content_is_empty(self, adapter) -> None:
        response = adapter.normalize_response(
            _payload("", reasoning="用户要求我回答两个字……", finish="length")
        )
        message = response.choices[0].message
        assert message.content == "用户要求我回答两个字……"
        assert message.model_extra["content_recovered_from_reasoning"] is True

    def test_reasoning_content_field_is_also_honoured(self, adapter) -> None:
        """DeepSeek / Kimi style naming."""
        payload = _payload(None, finish="length")
        payload["choices"][0]["message"]["reasoning_content"] = "思考过程"
        response = adapter.normalize_response(payload)
        assert response.choices[0].message.content == "思考过程"

    def test_a_real_answer_is_never_overwritten(self, adapter) -> None:
        response = adapter.normalize_response(_payload("成功", reasoning="一些思考"))
        message = response.choices[0].message
        assert message.content == "成功"
        assert "content_recovered_from_reasoning" not in message.model_extra

    def test_truncation_keeps_a_finish_reason(self, adapter) -> None:
        response = adapter.normalize_response(_payload("", reasoning="思考", finish=None))
        assert response.choices[0].finish_reason == "length"

    def test_an_explicit_finish_reason_is_preserved(self, adapter) -> None:
        response = adapter.normalize_response(_payload("", reasoning="思考", finish="stop"))
        assert response.choices[0].finish_reason == "stop"

    def test_no_reasoning_and_no_content_stays_empty(self, adapter) -> None:
        """Nothing to recover - do not invent content."""
        response = adapter.normalize_response(_payload(""))
        message = response.choices[0].message
        assert message.content == ""
        assert "content_recovered_from_reasoning" not in message.model_extra

    def test_whitespace_only_reasoning_is_not_used(self, adapter) -> None:
        response = adapter.normalize_response(_payload("", reasoning="   \n  "))
        assert response.choices[0].message.content == ""

    def test_non_string_reasoning_is_ignored(self, adapter) -> None:
        payload = _payload("")
        payload["choices"][0]["message"]["reasoning"] = {"unexpected": "shape"}
        response = adapter.normalize_response(payload)
        assert response.choices[0].message.content == ""


class TestBaseUrlHandling:
    def test_trailing_slash_is_trimmed(self) -> None:
        """ModelScope's base_url ends with ``/``; doubling it would 404."""
        adapter = OpenAICompatibleAdapter(
            ProviderConfig(
                id="modelscope",
                type="openai_compatible",
                base_url="https://api-inference.modelscope.cn/v1/",
            )
        )
        assert adapter.base_url == "https://api-inference.modelscope.cn/v1"
        assert adapter.chat_completions_url == "https://api-inference.modelscope.cn/v1/chat/completions"

    def test_nvidia_style_base_url_keeps_its_v1(self) -> None:
        adapter = OpenAICompatibleAdapter(
            ProviderConfig(
                id="nvidia",
                type="openai_compatible",
                base_url="https://integrate.api.nvidia.com/v1",
            )
        )
        assert adapter.chat_completions_url == "https://integrate.api.nvidia.com/v1/chat/completions"


class TestVendorPrefixedModelNames:
    """NVIDIA rejects bare model names - the prefix must survive verbatim."""

    def test_build_payload_keeps_the_vendor_prefix(self) -> None:
        from app.models.request import ChatCompletionRequest

        adapter = OpenAICompatibleAdapter(
            ProviderConfig(
                id="nvidia",
                type="openai_compatible",
                base_url="https://integrate.api.nvidia.com/v1",
            )
        )
        deployment = DeploymentConfig(
            id="kimi-k3-nvidia",
            provider_id="nvidia",
            model="moonshotai/kimi-k3",
        )
        request = ChatCompletionRequest(
            model="zk-reasoning",
            messages=[{"role": "user", "content": "hi"}],
        )
        payload, _unsupported = adapter.build_payload(request, deployment)
        assert payload["model"] == "moonshotai/kimi-k3"


class TestMaxTokensClamping:
    """客户端传非法 max_tokens 时网关必须兜底，不能原样透传给上游。

    WorkBuddy 实测传 >131072 的 max_tokens，商汤直接 400
    ``field MaxTokens invalid, should be in [1, 131072]``。
    """

    _DEPLOY = DeploymentConfig(
        id="sensenova-6.8-flash-sensenova",
        provider_id="sensenova",
        model="sensenova-6.8-flash",
        max_output_tokens=65536,
    )

    def _adapter(self) -> OpenAICompatibleAdapter:
        return OpenAICompatibleAdapter(
            ProviderConfig(
                id="sensenova",
                type="openai_compatible",
                base_url="https://api.sensenova.cn/v1",
            )
        )

    def test_over_limit_is_clamped_to_deployment_default(self) -> None:
        from app.models.request import ChatCompletionRequest

        adapter = self._adapter()
        request = ChatCompletionRequest(
            model="sensenova-6.8-flash",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=200000,
        )
        payload, _ = adapter.build_payload(request, self._DEPLOY)
        assert payload["max_tokens"] == 65536

    def test_zero_or_negative_falls_back_to_deployment_default(self) -> None:
        from app.models.request import ChatCompletionRequest

        adapter = self._adapter()
        request = ChatCompletionRequest(
            model="sensenova-6.8-flash",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=0,
        )
        payload, _ = adapter.build_payload(request, self._DEPLOY)
        assert payload["max_tokens"] == 65536

    def test_valid_value_is_passed_through(self) -> None:
        from app.models.request import ChatCompletionRequest

        adapter = self._adapter()
        request = ChatCompletionRequest(
            model="sensenova-6.8-flash",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=8192,
        )
        payload, _ = adapter.build_payload(request, self._DEPLOY)
        assert payload["max_tokens"] == 8192

    def test_anthropic_clamps_over_limit_too(self) -> None:
        """同一条不变量必须覆盖 Anthropic 适配器（2026-11-19 审查补）。

        截断保护原来只写在 OpenAI 兼容路径；Anthropic 的 max_tokens 是必填
        字段，客户端超限值原样透传同样会被上游 400。收敛到基类后这里钉住。
        """
        from app.models.request import ChatCompletionRequest
        from app.providers.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(
            ProviderConfig(id="anthropic", type="anthropic", base_url="https://api.anthropic.com")
        )
        request = ChatCompletionRequest(
            model="claude", messages=[{"role": "user", "content": "hi"}], max_tokens=200000
        )
        payload, _ = adapter.build_payload(request, self._DEPLOY)
        assert payload["max_tokens"] == 65536

    def test_gemini_clamps_over_limit_too(self) -> None:
        from app.models.request import ChatCompletionRequest
        from app.providers.gemini import GeminiAdapter

        adapter = GeminiAdapter(
            ProviderConfig(id="gemini", type="gemini", base_url="https://generativelanguage.googleapis.com")
        )
        request = ChatCompletionRequest(
            model="gemini", messages=[{"role": "user", "content": "hi"}], max_tokens=200000
        )
        payload, _ = adapter.build_payload(request, self._DEPLOY)
        assert payload["generationConfig"]["maxOutputTokens"] == 65536

    def test_ollama_clamps_over_limit_too(self) -> None:
        from app.models.request import ChatCompletionRequest
        from app.providers.ollama import OllamaAdapter

        adapter = OllamaAdapter(
            ProviderConfig(id="ollama", type="ollama", base_url="http://127.0.0.1:11434")
        )
        request = ChatCompletionRequest(
            model="llama", messages=[{"role": "user", "content": "hi"}], max_tokens=200000
        )
        payload, _ = adapter.build_payload(request, self._DEPLOY)
        assert payload["options"]["num_predict"] == 65536

    def test_zero_falls_back_on_anthropic(self) -> None:
        """0/负数在 Anthropic 侧也要回退（必填字段不能发 0）。"""
        from app.models.request import ChatCompletionRequest
        from app.providers.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(
            ProviderConfig(id="anthropic", type="anthropic", base_url="https://api.anthropic.com")
        )
        request = ChatCompletionRequest(
            model="claude", messages=[{"role": "user", "content": "hi"}], max_tokens=0
        )
        payload, _ = adapter.build_payload(request, self._DEPLOY)
        assert payload["max_tokens"] == 65536


class TestReasoningOnlyStreaming:
    """Streaming must not silently deliver nothing when a provider streams
    everything into ``reasoning_content``.

    Observed live on ModelScope (both ``Qwen/Qwen3.8-Flash-Next`` and
    ``deepseek-ai/DeepSeek-V4-Flash-0731``): every SSE chunk carries an empty
    ``delta.content`` and puts the real answer in ``delta.reasoning_content``.
    Before the fix the gateway forwarded those empty deltas verbatim, so a
    client rendering the stream showed a blank message for a call that had
    actually succeeded.
    """

    def _chunk(self, delta: dict) -> Any:
        from app.models.response import ChatCompletionChunk

        return ChatCompletionChunk.model_validate(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 1789045978,
                "model": "Qwen/Qwen3.8-Flash-Next",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
        )

    def test_reasoning_content_is_promoted_to_content(self) -> None:
        chunk = self._chunk(
            {"role": None, "content": "", "reasoning_content": "用户要求数到三"}
        )
        OpenAICompatibleAdapter._recover_reasoning_only_chunks(chunk)
        assert chunk.choices[0].delta.content == "用户要求数到三"
        assert chunk.choices[0].delta.model_extra["content_recovered_from_reasoning"] is True

    def test_promotion_drops_the_source_fields(self) -> None:
        """After recovery the text must not ride along in ``reasoning*`` too:
        clients reading both fields render the same chunk twice."""
        chunk = self._chunk(
            {"content": "", "reasoning_content": "答案全文", "reasoning": "答案全文"}
        )
        OpenAICompatibleAdapter._recover_reasoning_only_chunks(chunk)
        extra = chunk.choices[0].delta.model_extra
        assert chunk.choices[0].delta.content == "答案全文"
        assert "reasoning_content" not in extra
        assert "reasoning" not in extra

    def test_coexisting_content_keeps_reasoning_intact(self) -> None:
        """A genuine thinking + answer chunk is NOT recovered, so both fields stay."""
        chunk = self._chunk({"content": "答案", "reasoning_content": "思考"})
        OpenAICompatibleAdapter._recover_reasoning_only_chunks(chunk)
        assert chunk.choices[0].delta.content == "答案"
        assert chunk.choices[0].delta.model_extra["reasoning_content"] == "思考"

    @pytest.mark.parametrize("key", ["reasoning_content", "reasoning"])
    def test_both_reasoning_key_spellings_are_honoured(self, key: str) -> None:
        chunk = self._chunk({"content": "", key: "思考中"})
        OpenAICompatibleAdapter._recover_reasoning_only_chunks(chunk)
        assert chunk.choices[0].delta.content == "思考中"

    def test_real_content_is_never_overwritten(self) -> None:
        chunk = self._chunk({"content": "真正答案", "reasoning_content": "思考"})
        OpenAICompatibleAdapter._recover_reasoning_only_chunks(chunk)
        assert chunk.choices[0].delta.content == "真正答案"

    def test_delta_without_reasoning_stays_empty(self) -> None:
        chunk = self._chunk({"content": "", "tool_calls": None})
        OpenAICompatibleAdapter._recover_reasoning_only_chunks(chunk)
        assert chunk.choices[0].delta.content == ""

    def test_whitespace_only_reasoning_is_ignored(self) -> None:
        chunk = self._chunk({"content": "", "reasoning_content": "   \n  "})
        OpenAICompatibleAdapter._recover_reasoning_only_chunks(chunk)
        assert chunk.choices[0].delta.content == ""

    def test_first_chunk_role_only_is_untouched(self) -> None:
        """The opening chunk carries ``role`` and no text - leave it alone."""
        chunk = self._chunk({"role": "assistant", "content": ""})
        OpenAICompatibleAdapter._recover_reasoning_only_chunks(chunk)
        assert chunk.choices[0].delta.content == ""
        assert chunk.choices[0].delta.role == "assistant"

    def test_multiple_choices_are_all_processed(self) -> None:
        from app.models.response import ChatCompletionChunk

        chunk = ChatCompletionChunk.model_validate(
            {
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": "", "reasoning_content": "A"}},
                    {"index": 1, "delta": {"content": "", "reasoning_content": "B"}},
                ],
            }
        )
        OpenAICompatibleAdapter._recover_reasoning_only_chunks(chunk)
        assert [c.delta.content for c in chunk.choices] == ["A", "B"]


class TestStreamingToolCallTolerance:
    """Regression: SenseNova-style tool-call deltas used to crash the parser.

    Continuation deltas repeat the envelope with empty ``type``/``name``/``id``
    (captured live from ``token.sensenova.cn``), and ``ToolCall.type`` was a
    strict ``Literal["function"]`` - so every agent request with ``tools``
    produced a gateway ValidationError that blacked out the whole key pool.
    """

    def _chunk(self, tool_call: dict) -> dict:
        return {
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"tool_calls": [tool_call]}}],
        }

    def test_empty_type_and_name_parse_and_normalise(self) -> None:
        from app.models.response import ChatCompletionChunk

        chunk = ChatCompletionChunk.model_validate(
            self._chunk(
                {
                    "index": 0,
                    "id": "",
                    "type": "",
                    "function": {"name": "", "arguments": '{"city": "北京"}'},
                }
            )
        )
        call = chunk.choices[0].delta.tool_calls[0]
        assert call.type == "function"
        assert call.index == 0
        assert call.function.arguments == '{"city": "北京"}'

    def test_omitted_optional_fields_parse(self) -> None:
        """OpenAI itself sends bare ``{index, function:{arguments}}`` continuation deltas."""
        from app.models.response import ChatCompletionChunk

        chunk = ChatCompletionChunk.model_validate(
            self._chunk({"index": 1, "function": {"arguments": '{"a"'}})
        )
        call = chunk.choices[0].delta.tool_calls[0]
        assert call.type == "function"
        assert call.function.name == ""
        assert call.function.arguments == '{"a"'

    def test_null_ids_are_tolerated(self) -> None:
        from app.models.response import ChatCompletionChunk

        chunk = ChatCompletionChunk.model_validate(
            self._chunk(
                {"index": 0, "id": None, "type": "function", "function": None}
            )
        )
        call = chunk.choices[0].delta.tool_calls[0]
        assert call.id == ""
        assert call.function.arguments == ""

    def test_first_delta_keeps_its_real_id(self) -> None:
        from app.models.response import ChatCompletionChunk

        chunk = ChatCompletionChunk.model_validate(
            self._chunk(
                {"index": 0, "id": "call_abc", "type": "function",
                 "function": {"name": "get_weather", "arguments": ""}}
            )
        )
        call = chunk.choices[0].delta.tool_calls[0]
        assert call.id == "call_abc"
        assert call.function.name == "get_weather"


class TestToolChoiceWithoutTools:
    """``tool_choice`` without ``tools`` must never reach the provider."""

    def test_tool_choice_is_dropped_when_no_tools(self) -> None:
        from app.models.request import ChatCompletionRequest

        adapter = OpenAICompatibleAdapter(
            ProviderConfig(id="p", type="openai_compatible", base_url="https://x.invalid/v1")
        )
        deployment = DeploymentConfig(id="d", provider_id="p", model="m")
        request = ChatCompletionRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tool_choice="auto",   # no tools declared
        )
        payload, _ = adapter.build_payload(request, deployment)
        assert "tool_choice" not in payload

    def test_tool_choice_survives_with_tools(self) -> None:
        from app.models.request import ChatCompletionRequest

        adapter = OpenAICompatibleAdapter(
            ProviderConfig(id="p", type="openai_compatible", base_url="https://x.invalid/v1")
        )
        deployment = DeploymentConfig(id="d", provider_id="p", model="m")
        request = ChatCompletionRequest(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tool_choice="auto",
            tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        )
        payload, _ = adapter.build_payload(request, deployment)
        assert payload["tool_choice"] == "auto"


class TestGeminiThoughtParts:
    """Gemini thinking models: parts flagged ``thought: true`` never reach content."""

    def _adapter(self):
        from app.providers.gemini import GeminiAdapter

        return GeminiAdapter(
            ProviderConfig(id="gemini", type="gemini", base_url="https://x.invalid")
        )

    def test_thought_parts_are_not_merged_into_the_answer(self) -> None:
        payload = {
            "candidates": [{
                "content": {"parts": [
                    {"text": "用户要先算出总数", "thought": True},
                    {"text": "总数是 42。"},
                ]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        }
        response = self._adapter().normalize_response(payload)
        assert response.choices[0].message.content == "总数是 42。"
        assert "总数" not in (response.choices[0].message.content or "")[:0]  # sanity

    @pytest.mark.asyncio
    async def test_streaming_blocked_prompt_raises_content_filter(self) -> None:
        from app.core.errors import ContentFilterError
        from app.models.provider import ModelConfig
        from app.models.request import ChatCompletionRequest
        from app.providers.base import ProviderContext

        adapter = self._adapter()
        ctx = ProviderContext(
            request_id="t",
            deployment=DeploymentConfig(id="d", provider_id="gemini", model="m"),
            model=ModelConfig(id="m"),
        )
        request = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])

        class FakeResp:
            status_code = 200

            async def aiter_lines(self):
                yield 'data: {"promptFeedback": {"blockReason": "SAFETY"}}'
                yield ""
                yield "data: [DONE]"
                yield ""

            async def aread(self):
                return b""

            async def aclose(self):
                return None

        class FakeClient:
            is_closed = False

            def build_request(self, *a, **k):
                return None

            async def send(self, *a, **k):
                return FakeResp()

        adapter._client = FakeClient()
        stream = adapter.stream(request, ctx)
        with pytest.raises(ContentFilterError):
            async for _ in stream:
                pass


class TestAnthropicModelListingAuth:
    """Anthropic-compatible third parties authenticate ``/models`` differently.

    Observed live with StepFun: ``POST /step_plan/v1/messages`` accepts
    ``x-api-key``, but ``GET /step_plan/v1/models`` answers 401 for it and only
    accepts ``Authorization: Bearer``. Listing thus failed for a provider whose
    chat path works, which surfaces in the console as "this provider has no models".
    """

    async def _list_with(self, statuses: list[int]) -> tuple[list[str], list[dict[str, str]]]:
        import httpx

        from app.models.credential import CredentialRuntime
        from app.providers.anthropic import AnthropicAdapter

        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.headers))
            status = statuses[min(len(seen) - 1, len(statuses) - 1)]
            if status >= 400:
                return httpx.Response(status, json={"error": {"message": "Incorrect API key"}})
            return httpx.Response(200, json={"data": [{"id": "step-3.7-flash"}, {"id": "step-5-preview"}]})

        adapter = AnthropicAdapter(
            ProviderConfig(
                id="stepfun", type="anthropic", base_url="https://api.stepfun.com/step_plan/v1"
            )
        )
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            cred = CredentialRuntime(id="c1", provider_id="stepfun", secret="sk-test")
            models = await adapter.list_models(cred)
        finally:
            await adapter.aclose()
        return models, seen

    async def test_falls_back_to_bearer_on_401(self) -> None:
        models, seen = await self._list_with([401, 200])
        assert models == ["step-3.7-flash", "step-5-preview"]
        assert len(seen) == 2, "should retry once"
        assert seen[0].get("x-api-key") == "sk-test"
        assert seen[1].get("authorization") == "Bearer sk-test"

    async def test_no_retry_when_first_attempt_succeeds(self) -> None:
        models, seen = await self._list_with([200])
        assert models == ["step-3.7-flash", "step-5-preview"]
        assert len(seen) == 1
        assert "authorization" not in seen[0]


class TestAnthropicThinkingBlocks:
    """Anthropic-style ``thinking`` blocks must not be dropped on the floor.

    Observed live with StepFun ``step-3.7-flash``: every reply arrives as
    ``[{type: thinking}, {type: text}]``. The adapter only read ``text`` blocks, so
    a reply cut off by a low ``max_tokens`` came back as an empty string with no
    explanation - HTTP 200 and nothing in it.
    """

    def _adapter(self):
        from app.providers.anthropic import AnthropicAdapter

        return AnthropicAdapter(
            ProviderConfig(id="stepfun", type="anthropic", base_url="https://api.stepfun.com/step_plan/v1")
        )

    def _payload(self, blocks: list[dict], stop_reason: str) -> dict:
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "step-3.7-flash",
            "content": blocks,
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 17, "output_tokens": 20},
        }

    def test_thinking_is_surfaced_when_answer_was_cut_off(self) -> None:
        response = self._adapter().normalize_response(
            self._payload([{"type": "thinking", "thinking": "用户要我回两个字……"}], "max_tokens")
        )
        message = response.choices[0].message
        assert message.content == "用户要我回两个字……"
        assert message.model_extra["content_recovered_from_reasoning"] is True
        assert response.choices[0].finish_reason == "length"

    def test_thinking_travels_as_reasoning_when_answer_exists(self) -> None:
        response = self._adapter().normalize_response(
            self._payload(
                [{"type": "thinking", "thinking": "思考过程"}, {"type": "text", "text": "你好"}],
                "end_turn",
            )
        )
        message = response.choices[0].message
        assert message.content == "你好"
        assert message.model_extra["reasoning"] == "思考过程"
        assert "content_recovered_from_reasoning" not in message.model_extra

    def test_tool_use_blocks_still_parse(self) -> None:
        response = self._adapter().normalize_response(
            self._payload(
                [
                    {"type": "thinking", "thinking": "要调用工具"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {"city": "北京"},
                    },
                ],
                "tool_use",
            )
        )
        message = response.choices[0].message
        assert response.choices[0].finish_reason == "tool_calls"
        assert message.tool_calls[0].function.name == "get_weather"
        assert message.model_extra["reasoning"] == "要调用工具"
    @pytest.mark.asyncio
    async def test_stream_thinking_delta_is_not_dropped(self) -> None:
        """Anthropic streaming thinking_delta events must surface as reasoning extras."""
        import json

        from app.models.provider import ModelConfig
        from app.models.request import ChatCompletionRequest
        from app.providers.base import ProviderContext

        adapter = self._adapter()
        ctx = ProviderContext(
            request_id="t",
            deployment=DeploymentConfig(id="d", provider_id="stepfun", model="step-3.7-flash"),
            model=ModelConfig(id="step-3.7-flash"),
        )
        request = ChatCompletionRequest(
            model="step-3.7-flash", messages=[{"role": "user", "content": "hi"}]
        )

        def _sse(payload: dict) -> str:
            return "data: " + json.dumps(payload, ensure_ascii=False)

        class FakeResp:
            status_code = 200

            async def aiter_lines(self):
                events = [
                    {"type": "message_start",
                     "message": {"id": "msg_1", "usage": {"input_tokens": 5}}},
                    {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "thinking", "thinking": ""}},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "thinking_delta", "thinking": "先想"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "content_block_start", "index": 1,
                     "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 1,
                     "delta": {"type": "text_delta", "text": "你好"}},
                    {"type": "content_block_stop", "index": 1},
                    {"type": "message_delta",
                     "delta": {"stop_reason": "end_turn"},
                     "usage": {"output_tokens": 10}},
                    {"type": "message_stop"},
                ]
                for event in events:
                    yield _sse(event)
                    yield ""

            async def aread(self):
                return b""

            async def aclose(self):
                return None

        class FakeClient:
            is_closed = False

            def build_request(self, *a, **k):
                return None

            async def send(self, *a, **k):
                return FakeResp()

        adapter._client = FakeClient()
        chunks = [chunk async for chunk in adapter.stream(request, ctx)]

        reasoning_chunks = [
            c for c in chunks
            if c.choices and (c.choices[0].delta.model_extra or {}).get("reasoning")
        ]
        text_chunks = [c for c in chunks if c.choices and c.choices[0].delta.content]
        assert len(reasoning_chunks) == 1
        assert reasoning_chunks[0].choices[0].delta.model_extra["reasoning"] == "先想"
        assert any("你好" in (c.choices[0].delta.content or "") for c in text_chunks)



class TestAnthropicStreamingCacheTokens:
    """流式 Anthropic 的 ``cache_read_input_tokens`` 必须落到收尾的 ``Usage``。

    2026-11-24 真机：DeepSeek Harness 桌面档走 ``anthropic-messages``，网关
    日志里上游命中 90%+ 而客户端恒显 0%。两处一起漏——适配器的流式分支只
    取了 input/output，``/v1/messages`` 的 assembler 又只发两个字段。
    """

    def _adapter(self):
        from app.providers.anthropic import AnthropicAdapter

        return AnthropicAdapter(
            ProviderConfig(id="stepfun", type="anthropic", base_url="https://api.stepfun.com/step_plan/v1")
        )

    def _ctx_request(self):
        from app.models.provider import ModelConfig
        from app.models.request import ChatCompletionRequest
        from app.providers.base import ProviderContext

        ctx = ProviderContext(
            request_id="t",
            deployment=DeploymentConfig(id="d", provider_id="stepfun", model="step-3.7-flash"),
            model=ModelConfig(id="step-3.7-flash"),
        )
        request = ChatCompletionRequest(
            model="step-3.7-flash", messages=[{"role": "user", "content": "hi"}]
        )
        return ctx, request

    def _wire(self, adapter, events: list[dict]) -> None:
        import json

        def _sse(payload: dict) -> str:
            return "data: " + json.dumps(payload, ensure_ascii=False)

        class FakeResp:
            status_code = 200

            async def aiter_lines(self):
                for event in events:
                    yield _sse(event)
                    yield ""

            async def aread(self):
                return b""

            async def aclose(self):
                return None

        class FakeClient:
            is_closed = False

            def build_request(self, *a, **k):
                return None

            async def send(self, *a, **k):
                return FakeResp()

        adapter._client = FakeClient()

    @pytest.mark.asyncio
    async def test_cache_read_input_tokens_reaches_the_final_usage(self) -> None:
        adapter = self._adapter()
        self._wire(
            adapter,
            [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        # Anthropic 语义：input_tokens **不含**缓存部分
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 0,
                            "cache_read_input_tokens": 900,
                        },
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hi"},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 10},
                },
                {"type": "message_stop"},
            ],
        )
        ctx, request = self._ctx_request()
        chunks = [chunk async for chunk in adapter.stream(request, ctx)]

        final_usage = next(c.usage for c in reversed(chunks) if c.usage is not None)
        assert final_usage.cached_tokens == 900, "流式路径不许再把缓存数丢掉"
        # 网关口径（OpenAI 语义）：prompt_tokens 是**含缓存**的总输入，
        # 所以 Anthropic 的 input_tokens=100 要折算成 100 + 900 = 1000。
        # 照抄 100 会让 /v1/messages 的 input_tokens 变成 100-900 → 夹到 0，
        # 命中率显示 100%，而真值是 90%。
        assert final_usage.prompt_tokens == 1000
        assert final_usage.completion_tokens == 10

    @pytest.mark.asyncio
    async def test_cache_read_input_tokens_survives_a_message_delta_only_report(
        self,
    ) -> None:
        """``message_start`` 没报、``message_delta`` 补报时也要接住。"""
        adapter = self._adapter()
        self._wire(
            adapter,
            [
                {
                    "type": "message_start",
                    "message": {"id": "msg_2", "usage": {"input_tokens": 100}},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 10, "cache_read_input_tokens": 640},
                },
                {"type": "message_stop"},
            ],
        )
        ctx, request = self._ctx_request()
        chunks = [chunk async for chunk in adapter.stream(request, ctx)]
        final_usage = next(c.usage for c in reversed(chunks) if c.usage is not None)
        assert final_usage.cached_tokens == 640

    @pytest.mark.asyncio
    async def test_no_cache_field_means_zero_not_a_crash(self) -> None:
        """上游完全不报缓存（StepFun 常见）时按 0 处理，不影响正常字段。"""
        adapter = self._adapter()
        self._wire(
            adapter,
            [
                {
                    "type": "message_start",
                    "message": {"id": "msg_3", "usage": {"input_tokens": 100}},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 10},
                },
                {"type": "message_stop"},
            ],
        )
        ctx, request = self._ctx_request()
        chunks = [chunk async for chunk in adapter.stream(request, ctx)]
        final_usage = next(c.usage for c in reversed(chunks) if c.usage is not None)
        assert final_usage.cached_tokens == 0
        assert final_usage.prompt_tokens == 100
        assert final_usage.completion_tokens == 10


# ---------------------------------------------------------------------------
# 健康检查必须测「推理」，不能只测「目录」（2026-09-29）
# ---------------------------------------------------------------------------

class _CatalogueOnlyAdapter(OpenAICompatibleAdapter):
    """目录能列、推理必挂——NVIDIA 的真实形态。

    实测：`/v1/models` 返回 200（81 个模型），但**所有**推理请求要么挂死
    （glm-5.3，121s 无响应）要么 404（`Function '...' not found`）。
    旧健康检查只调 `list_models`，于是把一个 100% 不可用的供应商一直报成绿灯，
    它因此留在所有故障转移链上，累计烧掉 60.9 小时纯等待。
    """

    def __init__(self, *args: Any, models: list[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._models = models

    async def list_models(self, credential: Any = None) -> list[str]:
        return list(self._models)

    async def chat(self, request: Any, ctx: Any) -> Any:
        raise ModelNotFoundError("Function 'abc-123' not found", model=ctx.deployment.model)


def _catalogue_only(models: list[str]) -> _CatalogueOnlyAdapter:
    return _CatalogueOnlyAdapter(
        ProviderConfig(id="nvidia", type="openai_compatible",
                       base_url="https://example.invalid/v1"),
        models=models,
    )


async def test_health_check_flags_a_provider_that_cannot_infer() -> None:
    """目录可达但推理全失败 → 必须报不健康，并说清「目录里有 ≠ 能调用」。"""
    adapter = _catalogue_only(["z-ai/glm-5.3", "moonshotai/kimi-k3"])
    result = await adapter.health_check(None)
    assert result.ok is False, "目录通了就报健康——这正是 NVIDIA 假绿的成因"
    assert result.models, "仍应带上目录，便于对照"
    assert "目录可达" in (result.detail or "")
    assert "不等于" in (result.detail or "")


async def test_health_check_passes_when_any_probe_model_works() -> None:
    """目录里混着无权限条目时，只要有一个能推理就算健康（别误杀）。"""
    class _Mixed(OpenAICompatibleAdapter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.tried: list[str] = []

        async def list_models(self, credential: Any = None) -> list[str]:
            return ["dead-model", "live-model"]

        async def chat(self, request: Any, ctx: Any) -> Any:
            self.tried.append(ctx.deployment.model)
            if ctx.deployment.model == "dead-model":
                raise ModelNotFoundError("nope", model=ctx.deployment.model)
            return _live_response()

    adapter = _Mixed(ProviderConfig(id="sensenova", type="openai_compatible",
                                    base_url="https://example.invalid/v1"))
    result = await adapter.health_check(None)
    assert result.ok is True
    assert adapter.tried == ["dead-model", "live-model"], "要逐个试到成功为止"


async def test_health_check_probe_budget_zero_restores_catalogue_only() -> None:
    """``health_probe_models=0`` 回到旧行为（只查目录）——给不想付探测成本的场景。"""
    adapter = _catalogue_only(["a"])
    adapter.health_probe_models = 0
    result = await adapter.health_check(None)
    assert result.ok is True
    assert result.models == ["a"]


async def test_health_check_reports_the_catalogue_failure_first() -> None:
    """目录本身就不通时，报的是目录错误（鉴权/网络），不是推理错误。"""
    class _Dead(OpenAICompatibleAdapter):
        async def list_models(self, credential: Any = None) -> list[str]:
            raise AuthenticationError("bad key")

    adapter = _Dead(ProviderConfig(id="x", type="openai_compatible",
                                   base_url="https://example.invalid/v1"))
    result = await adapter.health_check(None)
    assert result.ok is False
    assert result.error_type == "authentication_error"


def _live_response() -> Any:
    from app.models.request import ChatMessage
    from app.models.response import (
        ChatCompletionChoice,
        ChatCompletionResponse,
        Usage,
    )

    return ChatCompletionResponse(
        id="x", model="live-model", created=0,
        choices=[ChatCompletionChoice(
            index=0,
            message=ChatMessage(role="assistant", content="ok"),
            finish_reason="stop",
        )],
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def test_assistant_tool_calls_message_always_carries_content() -> None:
    """assistant + tool_calls 的消息必须带 ``content`` 键。

    2026-09-29 第三轮审查抓到的真 bug：``model_dump(exclude_none=True)`` 把这个键
    整个删掉（canonical 模型里它是 ``content=None``）。OpenAI 自家 API 容忍，
    NVIDIA NIM 的严格反序列化不容忍：``missing field `content```
    ——而 400 被分类成「调用方的错」，不重试、不换 Key、不故障转移，
    于是整条请求死在链上最后一个部署。2026-09-27 有三个真实请求这么死的。
    """
    from app.models.provider import DeploymentConfig, ProviderConfig, ProviderType
    from app.models.request import (
        ChatCompletionRequest,
        ChatMessage,
        FunctionCall,
        ToolCall,
    )
    from app.providers.base import OpenAICompatibleAdapter

    adapter = OpenAICompatibleAdapter(
        ProviderConfig(id="p", type=ProviderType.OPENAI_COMPATIBLE, base_url="http://127.0.0.1:9/v1")
    )
    request = ChatCompletionRequest(
        model="m",
        messages=[
            ChatMessage(role="user", content="读一下这个文件"),
            # 这个形状由 app/models/request.py 的 flush_calls() 造出来——Responses
            # 路径的每一个工具轮都必然产生它，客户端输入里根本没有 assistant 消息
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(id="c1", function=FunctionCall(name="read_file", arguments='{"p":"a.py"}'))
                ],
            ),
            ChatMessage(role="tool", tool_call_id="c1", content="文件内容"),
        ],
    )
    deployment = DeploymentConfig(
        id="d", provider_id="p", model="m", context_window=128000
    )

    payload, _ = adapter.build_payload(request, deployment)

    for message in payload["messages"]:
        assert "content" in message, (
            f"{message['role']} 消息缺 content 键——严格反序列化的上游会 400 整条请求"
        )
    assert payload["messages"][1]["content"] == "", "只说工具调用时 content 是空串"
    assert payload["messages"][1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert payload["messages"][1]["tool_calls"][0]["id"] == "c1"


class TestDeveloperRoleMapping:
    """OpenAI 新版 developer 角色 → system（很多上游不认 developer）。"""

    def test_developer_role_becomes_system_on_the_wire(self) -> None:
        from app.models.request import ChatMessage
        from app.providers.base import _wire_message

        msg = ChatMessage(role="developer", content="you are helpful")
        wire = _wire_message(msg)
        assert wire["role"] == "system"
        assert wire["content"] == "you are helpful"

    def test_other_roles_pass_through(self) -> None:
        from app.models.request import ChatMessage
        from app.providers.base import _wire_message

        for role in ("system", "user", "assistant", "tool"):
            wire = _wire_message(ChatMessage(role=role, content="x"))  # type: ignore[arg-type]
            assert wire["role"] == role


class TestAnthropicThinkingEffort:
    """``reasoning_effort`` -> Anthropic 上游认的 ``thinking`` / ``output_config``.

    2026-11-24 之前这里是一条空路：``AnthropicMessagesRequest`` 从不读
    ``thinking``，适配器也不认 ``reasoning_effort``。DeepSeek Harness 桌面档
    配的 ``reasoningEffort: max`` 因此静默作废，按上游默认档烧额度，而两边
    都不报错。这一组钉住「档位要么生效产出正确字段，要么留下明确痕迹」。

    上游形状取自 ``app/models/presets.py`` 的 spec_notes：Messages 侧档位是
    ``output_config.effort``，关思考只能走 ``thinking.type = "disabled"``。
    """

    def _adapter(self, efforts=("low", "medium", "high"), default="high"):
        from app.providers.anthropic import AnthropicAdapter

        return AnthropicAdapter(
            ProviderConfig(
                id="stepfun",
                type="anthropic",
                base_url="https://api.stepfun.com/step_plan/v1",
            )
        ), DeploymentConfig(
            id="step-5-preview-stepfun",
            provider_id="stepfun",
            model="step-5-preview",
            max_output_tokens=32768,
            reasoning_efforts=efforts,
            reasoning_effort=default,
        )

    def _payload(self, adapter, deployment, effort):
        from app.models.request import ChatCompletionRequest, ChatMessage

        request = ChatCompletionRequest(
            model="ds",
            messages=[ChatMessage(role="user", content="hi")],
            max_tokens=32,
        )
        request.__pydantic_extra__ = {"reasoning_effort": effort}
        payload, unsupported = adapter.build_payload(request, deployment)
        return payload, unsupported

    def test_effort_lands_on_output_config_not_as_a_bare_field(self) -> None:
        adapter, deployment = self._adapter()
        payload, _ = self._payload(adapter, deployment, "high")

        assert payload["output_config"] == {"effort": "high"}
        assert "reasoning_effort" not in payload


    def test_off_effort_becomes_the_disabled_switch(self) -> None:
        """网关的 ``"none"`` -> Anthropic 的 ``thinking.type="disabled"``。

        ``output_config.effort="none"`` 在 Messages 侧关不掉思考（presets 的
        spec_notes 明写），只有开关写法生效。
        """
        adapter, deployment = self._adapter()
        payload, _ = self._payload(adapter, deployment, "none")

        assert payload["thinking"] == {"type": "disabled"}
        assert "output_config" not in payload

    def test_an_off_effort_survives_a_vocabulary_that_has_no_none(self) -> None:
        """部署词表里没写 ``none``，不代表「关思考」这条请求要失效。

        词表列的是思考强度（low/medium/high），关是开关语义，不该被词表驳回。
        2026-11-24 踩过：先归一化再判关，结果 ``none`` 被当未知档丢掉，
        客户端要求"别思考"时上游照样思考。
        """
        adapter, deployment = self._adapter(efforts=("low", "medium", "high"))
        payload, unsupported = self._payload(adapter, deployment, "none")

        assert payload["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in unsupported

    def test_an_effort_outside_the_vocabulary_is_dropped(self) -> None:
        """词表外的档位宁可省略（上游会 400），也不能原样透传。"""
        adapter, deployment = self._adapter(efforts=("low", "medium", "high"))
        payload, _ = self._payload(adapter, deployment, "max")

        assert "output_config" not in payload
        assert "thinking" not in payload

    def test_a_client_effort_beats_the_deployment_default(self) -> None:
        """客户端 > 部署默认 > 不指定：选了 ``low`` 就不能被默认 ``high`` 盖掉。"""
        adapter, deployment = self._adapter(default="high")
        payload, _ = self._payload(adapter, deployment, "low")

        assert payload["output_config"] == {"effort": "low"}

    def test_no_client_effort_leaves_the_deployment_default_alone(self) -> None:
        """客户端没说档位时，适配器不替它编一个（默认档由部署配置自己负责）。"""
        from app.models.request import ChatCompletionRequest, ChatMessage

        adapter, deployment = self._adapter(default="medium")
        request = ChatCompletionRequest(
            model="ds",
            messages=[ChatMessage(role="user", content="hi")],
            max_tokens=32,
        )
        payload, unsupported = adapter.build_payload(request, deployment)

        assert "output_config" not in payload
        assert "thinking" not in payload
        assert "reasoning_effort" not in unsupported

    def test_chat_wire_thinking_switches_do_not_leak_upstream(self) -> None:
        """``thinking: true`` 是 chat wire 的开关，不是 Anthropic 的 thinking 对象。

        原样透传会把布尔值变成上游的非法结构（400），所以要摘掉；但摘掉不该
        报成"未映射"——它本来就不是这个协议里的东西。
        """
        from app.models.request import ChatCompletionRequest, ChatMessage

        adapter, deployment = self._adapter()
        request = ChatCompletionRequest(
            model="ds",
            messages=[ChatMessage(role="user", content="hi")],
            max_tokens=32,
        )
        request.__pydantic_extra__ = {"thinking": True, "enable_thinking": True}
        payload, unsupported = adapter.build_payload(request, deployment)

        assert "thinking" not in payload
        assert "reasoning_effort" not in unsupported

