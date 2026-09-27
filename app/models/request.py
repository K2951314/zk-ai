"""Message / request / response schemas.

The gateway is OpenAI-compatible on the wire, so these models *are* the internal
canonical format: providers translate their native payloads into these shapes and
the API layer serialises them back out unchanged.
"""

from __future__ import annotations

import base64
import json
import struct
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

    @property
    def _image_parts(self) -> list[dict[str, Any]]:
        """Image content parts of this message (chat-side shapes).

        注意与 :data:`_IMAGE_PART_TYPES`（Responses 侧的 ``input_image`` 等）不是
        同一个集合：Protocol 翻译后图片 part 的 type 是 **``image_url``**，与
        :meth:`has_image` 认的那三种（``image_url`` / ``image`` / ``input_image``）
        一致——这里必须覆盖三种，因为 ``/v1/chat/completions`` 的客户端可能直接
        发任何一种。
        """
        out: list[dict[str, Any]] = []
        if isinstance(self.content, list):
            out.extend(
                part for part in self.content
                if isinstance(part, dict)
                and part.get("type") in _CHAT_IMAGE_PART_TYPES
            )
        return out

    def estimated_image_tokens(self) -> int:
        """Estimated tokens the image parts will cost upstream.

        2026-09-25：图片发自上游后（``_translate_input`` 会把 Codex 的
        ``input_image`` 转成 content part），``estimated_input_tokens()`` 对这些
        base64 一字符都算不到——text() 只读 text part。实测一条 24 张图的请求
        上游 payload 29MB，而估算只报 50 tokens，token 闸整个失明。

        换算口径：按各厂公开的切块计价（OpenAI/Anthropic 都是 ~750,000 像素
        折 1,024 tokens，短边先缩到 768），这里用同一系数。**必须解图片头拿真实
        宽高**：base64 长度与像素数没有稳定关系——实测同一批 Codex 截图，每千字符
        从 3,193 px（纯色块）到 114,970 px 不等，差 36 倍；按长度估算会彻底失准。

        解不开（不是 data URL、格式不认识、base64 坏了）时退回按长度粗估，
        宁可高估也不能让闸失明。
        """
        total = 0
        for part in self._image_parts:
            # 三个键都要认：_image_parts 的 type 有三种（见 _CHAT_IMAGE_PART_TYPES），
            # 键名却只有两种的话，input_image 形态会被静默跳过——口径分叉会继续腐烂。
            image = part.get("image_url") or part.get("image") or part.get("input_image") or {}
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str):
                continue
            size = _image_pixel_size(url)
            if size is None or not all(size):
                # 钳上界：只解头部 4096 个 base64 字符，EXIF 超过 ~2.9KB 的 JPEG
                # 会解不出尺寸而走到这里。不钳的话按 base64 长度算，一张 1200 万像素
                # 照片能估出真实值的 132 倍——足够多张叠加就把 256K 窗口的活请求
                # 413 掉（实测上游真实只用了 1.26% 窗口）。
                total += min(len(url) // 16, 2_000)
                continue
            width, height = size
            long_side, short_side = max(width, height), min(width, height)
            if short_side > 768:  # 先按短边缩到 768
                long_side = int(long_side * 768 / short_side)
                short_side = 768
            total += int(long_side * short_side / 750_000 * 1024)
        return total

    def has_image(self) -> bool:
        """True when any content part carries an image."""
        if isinstance(self.content, list):
            for part in self.content:
                if isinstance(part, dict) and part.get("type") in _CHAT_IMAGE_PART_TYPES:
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
        """Cheap heuristic (~4 chars/token) used for long-context routing.

        四类内容都要算，漏一类就会低估、而低估会让 ``context_fits`` 空转
        （2026-09-25 实测：日志记 269,162 est，上游实报 719,090 prompt_tokens，
        低估 2.67 倍，中文负载 4.0 倍）：

        1. ``content`` 文本与 role 信封（原样保留）；
        2. ``tool_calls`` 的 ``name`` + ``arguments`` —— 工具调用的入参本来就占
           可观的量（实测一条 30 轮会话 1232 个 function_call 共 70 万字符），
           而 ``text()`` 只看 content，这部分以前**完全没被计入**；
        3. ``tools`` schema —— 改用 JSON 序列化长度。原来是 ``str(self.tools)``，
           Python dict 的字面量表示比 JSON 短（单引号、``True``/``None``），
           31 个工具的 schema 会被系统性低估。
        4. 图片 part —— 按宽高估算，见 :meth:`ChatMessage.estimated_image_tokens`。
           图片发自上游后这是真实成本（实测一条 24 张图的请求上游 payload 29MB），
           而 base64 字符不进 ``text()``，不算它就是 token 闸失明。

        图片 token **不参与**最后那个 ``// 4``：它已经是 token 量纲（按像素切块
        算的），再除一次等于重复折算——实测 25 张图会从 34,699 被压成 8,676。
        """
        total = 0
        for message in self.messages:
            total += len(message.text()) + len(message.role) + 4
            for call in message.tool_calls or []:
                total += len(call.function.name) + len(call.function.arguments) + 8
        if self.tools:
            total += len(json.dumps(self.tools, ensure_ascii=False)) // 3
        chars_based = max(1, total // 4)
        return chars_based + sum(m.estimated_image_tokens() for m in self.messages)


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

    def to_chat_request(self, *, forward_images: bool = True) -> ChatCompletionRequest:
        """Translate the Responses payload into a chat completion request.

        ``forward_images`` 由 ``ZKAI_FORWARD_IMAGES`` 决定（API 层传入）：false 时
        图片回落成一行占位文本，不上传 base64，省 token 但模型看不到图。
        """
        messages: list[ChatMessage] = []
        if self.instructions:
            messages.append(ChatMessage(role="system", content=self.instructions))
        messages.extend(self._translate_input(forward_images=forward_images))
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

    def _translate_input(self, *, forward_images: bool = True) -> list[ChatMessage]:
        """``input`` items -> chat messages.

        ``forward_images=False`` 时图片不转成 content part，只留一行占位文本
        （见 ``_flatten_text``）——省 token，但模型看不到图。

        Tool rounds are replayed the way a chat-completions upstream expects:
        consecutive ``function_call`` items merge into ONE assistant message
        carrying every ``tool_calls``, and each ``function_call_output``
        becomes the matching ``tool`` message.

        SenseNova validates that history strictly - it answers HTTP 400
        "inference request is invalid" for shapes OpenAI tolerates - so two
        repairs happen here: an output whose ``call_id`` matches no open call
        (orphan, e.g. an edited-away turn) is dropped, and a call that never
        received its output (dangling, e.g. an interrupted turn) gets a
        synthetic response right after its assistant message. Without this a
        replayed Codex conversation that once interrupted a tool call can never
        be served again.
        """
        if isinstance(self.input, str):
            return [ChatMessage(role="user", content=self.input)]
        messages: list[ChatMessage] = []
        #: Consecutive function_call items, merged into one assistant message.
        pending: list[ToolCall] = []
        #: call ids of the last assistant message still awaiting their output.
        open_calls: list[str] = []

        def flush_calls() -> None:
            if not pending:
                return
            messages.append(ChatMessage(role="assistant", content=None, tool_calls=pending))
            open_calls.extend(call.id for call in pending)
            pending.clear()

        def answer_open_calls() -> None:
            for call_id in open_calls:
                messages.append(
                    ChatMessage(
                        role="tool",
                        content="(no output recorded)",
                        tool_call_id=call_id,
                    )
                )
            open_calls.clear()

        #: 落在工具轮中间的图片/文本，等这一轮的 outputs 落地后再插入。
        #: 为什么必须延迟：提前插入有两条死路——① 先 answer_open_calls() 会抢在
        #: 真实 output 之前，等 output 到达时 open_calls 已空，真实返回值被当
        #: 孤儿整条丢弃（上游收到"工具没返回内容"，而真相是"返回了但网关丢了"）；
        #: ② 不 answer 就直接插 user，会形成 assistant→user→tool，商汤直接 400
        #: 且不故障转移。延迟是唯一两条都避开的路。
        deferred_images: list[dict[str, Any]] = []
        deferred_text: list[str] = []

        def flush_deferred() -> None:
            for image_part in deferred_images:
                messages.append(ChatMessage(role="user", content=[image_part]))
            deferred_images.clear()
            for text in deferred_text:
                messages.append(ChatMessage(role="user", content=text))
            deferred_text.clear()

        for item in self.input:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "function_call":
                call_id = str(item.get("call_id") or item.get("id") or _new_call_id())
                pending.append(
                    ToolCall(
                        id=call_id,
                        function=FunctionCall(
                            name=str(item.get("name") or ""),
                            arguments=str(item.get("arguments") or ""),
                        ),
                    )
                )
                continue
            if kind == "function_call_output":
                flush_calls()
                call_id = str(item.get("call_id") or "")
                if call_id in open_calls:
                    raw_output = item.get("output")
                    output_images = (
                        _normalise_image_parts(raw_output) if forward_images else None
                    )
                    if output_images is None:
                        tool_content: str | list[dict[str, Any]] | None = _flatten_text(raw_output)
                    else:
                        # 工具返回里混着图（view_image 之类）：文本与图片一起发。
                        # 这里用 ``_text_only`` 而不是 ``_flatten_text``——后者会给
                        # 每张图补一行占位符，而图本身已经转成 content part 了，
                        # 两处都写就成了「图在这」又说「图不可用」的自相矛盾。
                        tool_text = _text_only(raw_output)
                        tool_parts: list[dict[str, Any]] = []
                        if tool_text:
                            tool_parts.append({"type": "text", "text": tool_text})
                        tool_parts.extend(output_images)
                        tool_content = tool_parts
                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=tool_content,
                            tool_call_id=call_id,
                        )
                    )
                    open_calls.remove(call_id)
                    if not open_calls:
                        # 这一轮的 outputs 都落地了，攒着的图片此刻插入才安全：
                        # 既不会挡住还没到的真实 output，也不会把 user 楔进
                        # assistant 与其 tool 应答之间。
                        flush_deferred()
                continue  # orphan output (no open call): dropped, upstream would 400
            if kind in _IMAGE_PART_TYPES:
                # 裸图片 item（Codex 把截图直接放进 input 数组）。以前这里被
                # 「kind != message 就 continue」整条丢掉，连"这里有过图"都不留。
                # 转成一条 user 消息带上图片 part，视觉模型才真的看得见。
                #
                # 落在工具轮中间时**先攒着**，等这一轮的 outputs 落地再插入
                # （理由见 deferred_images 注释：提前插会丢真实返回值，或造出
                # assistant→user→tool 被商汤 400）。一轮干净时才立即插入。
                image_parts = _normalise_image_parts([item]) if forward_images else None
                if pending or open_calls:
                    if image_parts:
                        deferred_images.extend(image_parts)
                    elif forward_images:
                        deferred_text.append(_IMAGE_PLACEHOLDER)
                    continue
                if image_parts:
                    messages.append(ChatMessage(role="user", content=image_parts))
                elif forward_images:
                    # 开关开着却没能解析出图片——留一行说明，别静默吞掉。
                    messages.append(ChatMessage(role="user", content=_IMAGE_PLACEHOLDER))
                continue
            if kind and kind != "message":
                continue  # reasoning / hosted-tool items: no chat equivalent
            flush_calls()
            answer_open_calls()
            role = str(item.get("role", "user"))
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            raw_content = item.get("content")
            image_parts = _normalise_image_parts(raw_content) if forward_images else None
            if image_parts is not None:
                # message 的 content 里混着图：文本与图片一起发。同样用
                # ``_text_only``（不是 ``_flatten_text``），避免"图在这"与
                # "图不可用"同时出现在同一段文本里。
                text = _text_only(raw_content)
                parts: list[dict[str, Any]] = []
                if text:
                    parts.append({"type": "text", "text": text})
                parts.extend(image_parts)
                messages.append(ChatMessage(role=role, content=parts))
                continue
            messages.append(ChatMessage(role=role, content=_flatten_text(raw_content)))
        flush_calls()
        answer_open_calls()
        flush_deferred()
        return messages


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


#: chat 侧（OpenAI wire）表示图片的 part type。与 Responses 侧的
#: :data:`_IMAGE_PART_TYPES` 是**两个集合**：翻译之后图片 part 的 type 会变成
#: ``image_url``（见 ``_translate_input`` / ``_normalise_image_parts``），
#: 而客户端直连 /v1/chat/completions 时三种都可能出现（gemini 与 ollama 适配器
#: 也认这三种，见 app/providers/gemini.py:133、app/providers/ollama.py:105）。
_CHAT_IMAGE_PART_TYPES = frozenset({"image_url", "image", "input_image"})
#: Responses 侧表示图片的 part type（Codex 实测只发 input_image，另两种是宽容）。
_IMAGE_PART_TYPES = frozenset({"input_image", "output_image", "image"})
#: 图片被降级成文本时的占位行。必须**显式**说明"这里有一张图但内容不可用"，
#: 否则空字符串会让模型以为"工具没返回东西"——那是撒谎，比丢数据更糟。
_IMAGE_PLACEHOLDER = "[图片内容不可用：网关未转发该图像]"


def _flatten_text(content: Any) -> str | None:
    """Content parts -> plain text; image parts become a placeholder line.

    2026-09-25：以前这里**静默丢掉** `input_image`（只认 input_text /
    output_text / text / refusal），后果分两种，都不是好事：

    * 裸 `input_image` item 在 `_translate_input` 里本来就被
      `if kind and kind != "message": continue` 整条丢弃——连"这里有过图"都不留
      （现已单独转成带图片 part 的 user 消息）；
    * 混在 `function_call_output` 里的图变成**空字符串 tool 消息**——发到上游
      等于撒谎"这个工具没返回内容"，而真相是"返回了图但网关读不了"。模型据此
      判断会答错。

    现在留一行占位文本而不是丢干净。占位符只在图片**没能**转成 content part 时
    才出现（`_translate_input` 的三处调用都已经转上去了，所以正常路径看不到它；
    它是兜底，保证任何新增路径都不会静默吞图）。
    """
    return _text_only(content, image_placeholder=_IMAGE_PLACEHOLDER)


def _text_only(content: Any, *, image_placeholder: str | None = None) -> str | None:
    """Content parts -> text, skipping image parts.

    ``image_placeholder`` 为 None 时图片被彻底跳过；给出字符串则每个图片 part
    补一行该文本。调用方**已经把图片转成 content part** 时必须传 None，否则
    同一段内容会既说"图在这"又说"图不可用"。
    """
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in _IMAGE_PART_TYPES:
                if image_placeholder:
                    parts.append(image_placeholder)
                continue
            if kind in {"input_text", "output_text", "text", "refusal", None}:
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return str(content)


def _image_pixel_size(data_url: str) -> tuple[int, int] | None:
    """Pixel size of a ``data:`` URL image, without decoding the whole thing.

    Reads only the header (PNG IHDR / JPEG SOF / GIF / WebMP). Returns ``None``
    when the URL is remote, the base64 is broken, or the format is unknown — the
    caller then falls back to a length-based estimate.
    """
    if not data_url.startswith("data:"):
        return None  # 远程 URL：不为了估算去下载
    comma = data_url.find(",")
    if comma < 0:
        return None
    try:
        # 窗口开到 16384 个 base64 字符（=12KB）：JPEG 的 SOF 段可能排在 2.9KB
        # 以上的 EXIF 后面，4096 解不出就回落按长度粗估，而后者会把带 EXIF 的
        # 照片估高两个数量级（P1-5）。仍是 O(头) 成本，实测 0.22ms/张。
        raw = base64.b64decode(data_url[comma + 1:comma + 1 + 16384], validate=False)
    except (ValueError, TypeError):
        return None
    if len(raw) < 16:
        return None
    # PNG: 8B signature, then a chunk "IHDR" carrying width/height as big-endian u32
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        # 长度闸是必须的：struct.unpack 的错是 struct.error，而它不是 ValueError
        # 的子类，调用处的 except (ValueError, TypeError) 拦不住——一张只传到
        # 16~23 字节的坏图（上传中断/代理截断）会让三条对外路径全部 500。
        # 别用「统一直拦 len<30」：24~29 字节的 PNG 本来解得出来。
        if raw[12:16] == b"IHDR" and len(raw) >= 24:
            return struct.unpack(">II", raw[16:24])
        return None
    # JPEG: scan segments for SOF0-SOF15 (0xFFC0..0xFFCF, excluding C4/C8/CC)
    if raw[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(raw):
            if raw[index] != 0xFF:
                index += 1
                continue
            marker = raw[index + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height, width = struct.unpack(">HH", raw[index + 5:index + 9])
                if width and height:
                    return width, height
                return None
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            if index + 4 > len(raw):
                return None
            segment = struct.unpack(">H", raw[index + 2:index + 4])[0]
            index += 2 + segment
        return None
    # GIF: "GIF87a"/"GIF89a" then u16 width/height little-endian
    if raw[:6] in {b"GIF87a", b"GIF89a"} and len(raw) >= 10:
        return struct.unpack("<HH", raw[6:10])
    # WebP: "RIFF....WEBPVP8 " (+ 3 variants)
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        if raw[12:16] == b"VP8 ":
            # 同样要长度闸（16~29 字节的 VP8 会 struct.error）。
            # 别改 raw[22:26]：实测 raw[26:30] 落在宽高上（00 04 00 03 -> 1024x768），
            # raw[22:26] 落在帧同步字节上，会把每个正常 VP8 的估算毁掉。
            if len(raw) >= 30:
                return struct.unpack("<HH", raw[26:30])
            return None
        if raw[12:16] == b"VP8L":
            if len(raw) >= 25:
                bits = int.from_bytes(raw[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            return None
        if raw[12:16] == b"VP8X":
            if len(raw) >= 30:
                width = int.from_bytes(raw[24:27], "little") + 1
                height = int.from_bytes(raw[27:30], "little") + 1
                return width, height
            return None
    return None


def _normalise_image_parts(content: Any) -> list[dict[str, Any]] | None:
    """Extract image parts from Responses content into chat ``content`` parts.

    Returns ``None`` when there is nothing image-like to carry, so callers can
    keep the plain-text fast path (the overwhelming majority of turns).
    """
    if not isinstance(content, list):
        return None
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in _IMAGE_PART_TYPES:
            continue
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, dict) else image
        if isinstance(url, str) and url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts or None


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
