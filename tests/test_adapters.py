"""Provider adapter behaviour that only shows up against real endpoints.

Every case here was found by pointing the gateway at a *real* OpenAI-compatible
provider (SenseNova) - the mock upstream answers exactly what the adapters
expect, so these shapes never appeared in the offline suite.
"""

from __future__ import annotations

from typing import Any

import pytest

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

