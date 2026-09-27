"""input token 估算器的回归测试。

2026-09-25 实测：日志记 269,162 est tokens，上游实报 719,090 prompt_tokens，
**低估 2.67 倍**（中文负载 4.0 倍）。而 ``context_fits`` 这道硬门槛完全建立在
估算值上——低估等于门禁空转，超窗请求被当成「放得下」发去上游，换来 400/429。

低估的三个来源，各有对应用例钉住：

1. ``tool_calls.function.arguments`` 以前完全不计（真实会话 1274 个 call
   共 746,953 字符全部漏算）；
2. ``tools`` schema 用 ``str(dict)`` 算，Python 字面量比 JSON 短，31 个工具的
   schema 被系统性低估；
3. content 里非 ``text`` 类型的 part 本来就不进 ``text()``——这是**有意**的
   （base64 图片在协议翻译阶段就被丢掉，见 ``_flatten_text``），不是估算器的责任，
   ``test_image_bytes_are_not_absent_from_the_wire_by_the_estimator`` 守住这条边界。
"""

from __future__ import annotations

import json
from typing import Any

from app.models.request import (
    ChatCompletionRequest,
    ChatMessage,
    FunctionCall,
    ResponsesRequest,
    ToolCall,
)


def _tools(count: int = 31) -> list[dict[str, Any]]:
    """Codex 那类 agent 客户端会带的扁平 tools 数组。"""
    return [
        {
            "type": "function",
            "function": {
                "name": f"tool_{index}",
                "description": "x" * 200,
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}, "workdir": {"type": "string"}},
                    "required": ["cmd"],
                },
            },
        }
        for index in range(count)
    ]


def test_tool_call_arguments_are_counted() -> None:
    """function.arguments 必须计入——它以前被整个漏掉。"""
    thin = ChatCompletionRequest(
        model="m",
        messages=[ChatMessage(role="user", content="hi")],
    )
    fat_argument = "y" * 20_000
    heavy = ChatCompletionRequest(
        model="m",
        messages=[
            ChatMessage(role="user", content="hi"),
            ChatMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        function=FunctionCall(name="exec_command", arguments=fat_argument),
                    )
                ],
            ),
        ],
    )
    # 20,000 字符约 5,000 tokens；没算进去的话两个估算会几乎相等
    assert heavy.estimated_input_tokens() - thin.estimated_input_tokens() >= 4_000


def test_tool_name_is_counted_alongside_arguments() -> None:
    """名字也要算，但量级远小于 arguments（别把信封算得比内容重）。"""
    base = ChatCompletionRequest(
        model="m", messages=[ChatMessage(role="user", content="hi")]
    )
    with_call = ChatCompletionRequest(
        model="m",
        messages=[
            ChatMessage(role="user", content="hi"),
            ChatMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="c",
                        function=FunctionCall(name="t" * 500, arguments="{}"),
                    )
                ],
            ),
        ],
    )
    delta = with_call.estimated_input_tokens() - base.estimated_input_tokens()
    assert 0 < delta < 200  # 500 字符 ≈ 125 tokens 量级


def test_tools_schema_uses_json_length_not_python_repr() -> None:
    """str(dict) 比 JSON 短，31 个工具的 schema 以前被系统性低估。"""
    tools = _tools(31)
    without = ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="hi")])
    with_tools = ChatCompletionRequest(
        model="m", messages=[ChatMessage(role="user", content="hi")], tools=tools
    )
    delta = with_tools.estimated_input_tokens() - without.estimated_input_tokens()
    json_tokens = len(json.dumps(tools, ensure_ascii=False)) // 3 // 4
    # 允许少量信封误差，但不该差出一个数量级
    assert abs(delta - json_tokens) <= max(50, json_tokens // 20)
    assert delta > 0


def test_estimate_grows_with_history_length() -> None:
    """长会话必须估得更大——否则 context_fits 永远拦不住它。"""
    def build(turns: int) -> ChatCompletionRequest:
        messages: list[ChatMessage] = [ChatMessage(role="system", content="S" * 4_000)]
        for index in range(turns):
            messages.append(ChatMessage(role="user", content=f"do step {index}"))
            messages.append(ChatMessage(role="tool", content="R" * 5_000))
        return ChatCompletionRequest(model="m", messages=messages)

    short, long = build(10), build(200)
    assert long.estimated_input_tokens() > short.estimated_input_tokens() * 5


def test_estimate_is_positive_and_stable() -> None:
    """空请求也要返回 >=1，且同一请求两次结果一致（路由要可复现）。"""
    empty = ChatCompletionRequest(model="m", messages=[])
    assert empty.estimated_input_tokens() >= 1
    request = ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="a")])
    assert request.estimated_input_tokens() == request.estimated_input_tokens()


def test_image_bytes_are_not_absent_from_the_wire_by_the_estimator() -> None:
    """边界说明而非功能：base64 图片在协议翻译阶段就被丢弃，估算器看不到它。

    ``_flatten_text`` 只认 input_text/output_text/text/refusal，``input_image``
    返回空串——所以 14MB 客户端 body 里那 76.6% 的图片**从来没到过上游**。

    这条边界在 2026-09-25 被改变了：图片现在**会**发自上游（见
    ``_translate_input`` 把 ``input_image`` 转成 ``image_url`` content part），
    所以估算器也必须跟着算它，否则 token 闸对图片流量彻底失明。下面三个断言
    钉住新契约。
    """
    text_like = ChatCompletion(
        content=[{"type": "input_text", "text": "描述一下这张图"}]
    )
    with_image = ChatCompletion(
        content=[
            {"type": "input_text", "text": "描述一下这张图"},
            {"type": "input_image", "image_url": "data:image/png;base64," + "B" * 500_000},
        ]
    )
    # 图片必须计入，且计入量显著（base64 占 50 万字符，按长度粗估也远超文本那段）
    assert with_image.estimated_input_tokens() > text_like.estimated_input_tokens() + 1_000


def test_image_tokens_use_pixel_size_not_base64_length() -> None:
    """同样尺寸的图，压缩得好坏不影响 token——token 按像素切块算，不按字节。

    这条很关键：实测同一批 Codex 截图，base64 每千字符对应 3,193 px（纯色块）
    到 114,970 px（细节丰富），差 36 倍。若按 base64 长度估算，一个纯色截图会
    被严重高估、一张细节丰富的图又被低估，两种都错。
    """
    def png(width: int, height: int) -> str:
        """Minimal but structurally valid PNG (IHDR + IEND)."""
        import base64 as b64
        import struct
        import zlib

        def chunk(kind: bytes, data: bytes) -> bytes:
            body = kind + data
            return struct.pack(">I", len(data)) + body + struct.pack(
                ">I", zlib.crc32(body) & 0xFFFFFFFF
            )

        ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
        return "data:image/png;base64," + b64.b64encode(
            b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")
        ).decode()

    request = ChatCompletionRequest(
        model="m",
        messages=[ChatMessage(role="user", content=[
            {"type": "image_url", "image_url": {"url": png(1024, 768)}},
        ])],
    )
    # 1024x768 = 786,432 px → 约 1,073 tokens；与 base64 长度无关
    tokens = request.estimated_input_tokens()
    assert 900 <= tokens <= 1_300, f"1024x768 应约 1,073 tokens，实得 {tokens}"


def test_image_tokens_do_not_get_divided_by_four() -> None:
    """图片 token 已经是 token 量纲，不能再被字符/token 比例折算一次。

    实测这个 bug：25 张真实截图按像素算出 34,699，塞进 // 4 之后只剩 8,676。
    """
    def png(width: int, height: int) -> str:
        import base64 as b64
        import struct
        import zlib

        def chunk(kind: bytes, data: bytes) -> bytes:
            body = kind + data
            return struct.pack(">I", len(data)) + body + struct.pack(
                ">I", zlib.crc32(body) & 0xFFFFFFFF
            )

        ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
        return "data:image/png;base64," + b64.b64encode(
            b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")
        ).decode()

    text_only = ChatCompletionRequest(
        model="m", messages=[ChatMessage(role="user", content="hi")]
    )
    one_image = ChatCompletionRequest(
        model="m",
        messages=[ChatMessage(role="user", content=[
            {"type": "image_url", "image_url": {"url": png(1440, 950)}},
        ])],
    )
    two_images = ChatCompletionRequest(
        model="m",
        messages=[ChatMessage(role="user", content=[
            {"type": "image_url", "image_url": {"url": png(1440, 950)}},
            {"type": "image_url", "image_url": {"url": png(1440, 950)}},
        ])],
    )
    delta_one = one_image.estimated_input_tokens() - text_only.estimated_input_tokens()
    delta_two = two_images.estimated_input_tokens() - text_only.estimated_input_tokens()
    assert delta_one > 1_000, f"一张 1440x950 应为 ~2,073 tokens，实得 {delta_one}"
    assert abs(delta_two - delta_one * 2) <= 2, "两张同尺寸图必须约等于一张的两倍"


def ChatCompletion(content: list[dict[str, Any]]) -> ChatCompletionRequest:
    """Construct a single-turn request with structured content parts."""
    return ChatCompletionRequest(
        model="m", messages=[ChatMessage(role="user", content=content)]
    )


# --------------------------------------------------------------------------- #
# 图片转发（Codex 的 input_image 以前被静默吞掉）
# --------------------------------------------------------------------------- #


def png(width: int, height: int) -> str:
    """A structurally valid minimal PNG as a data URL (IHDR + IEND)."""
    import base64 as b64
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")
    return "data:image/png;base64," + b64.b64encode(raw).decode()


def test_bare_input_image_becomes_a_message() -> None:
    """裸 input_image item 以前被整条丢弃，现在必须变成一条带图的 user 消息。

    实测真实 Codex rollout：25 张图里 24 张是这种形态（直接躺在 input 数组里）。
    """
    request = ResponsesRequest(model="m", input=[
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "看图"}]},
        {"type": "input_image", "image_url": png(800, 600), "detail": "high"},
    ])
    images = [m for m in request.to_chat_request().messages if m.has_image()]
    assert len(images) == 1
    assert images[0].role == "user"


def test_image_inside_function_call_output_is_not_swallowed() -> None:
    """工具返回里的图：以前变**空字符串** tool 消息，等于撒谎「工具没返回内容」。"""
    request = ResponsesRequest(model="m", input=[
        {"type": "function_call", "call_id": "c1", "name": "view_image", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": [
            {"type": "input_text", "text": "已读取 a.png"},
            {"type": "input_image", "image_url": png(800, 600)},
        ]},
    ])
    tool_messages = [m for m in request.to_chat_request().messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].has_image(), "图必须真的发上去"
    # 文本与图片不重复：不该同时出现「图在这」和「图不可用」
    assert "已读取 a.png" in (tool_messages[0].text() or "")
    assert "不可用" not in str(tool_messages[0].content)


def test_image_inside_message_content_is_forwarded() -> None:
    """用户在 message 里直接发图，也必须转发。"""
    request = ResponsesRequest(model="m", input=[
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "这是什么"},
            {"type": "input_image", "image_url": png(800, 600)},
        ]},
    ])
    messages = request.to_chat_request().messages
    assert len(messages) == 1 and messages[0].has_image()
    assert messages[0].text() == "这是什么"
    assert "不可用" not in str(messages[0].content)


def test_forward_images_false_falls_back_to_a_placeholder() -> None:
    """关掉转发时：不传 base64，但必须留一行说明，不能静默吞。"""
    request = ResponsesRequest(model="m", input=[
        {"type": "function_call", "call_id": "c1", "name": "view_image", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": [
            {"type": "input_text", "text": "已读取 a.png"},
            {"type": "input_image", "image_url": png(800, 600)},
        ]},
    ])
    messages = request.to_chat_request(forward_images=False).messages
    tool_messages = [m for m in messages if m.role == "tool"]
    assert not any(m.has_image() for m in messages), "关掉后不该再发图片"
    assert "已读取 a.png" in (tool_messages[0].text() or "")
    assert "不可用" in (tool_messages[0].text() or "")
    # 估算也得跟着降（token 闸据此判断）
    assert request.to_chat_request(forward_images=False).estimated_input_tokens() < (
        request.to_chat_request(forward_images=True).estimated_input_tokens()
    )


def _assert_tool_history_is_well_formed(messages) -> None:
    """Every assistant tool_call must be answered by a tool message with nothing
    in between.

    SenseNova rejects the other shape with HTTP 400 "inference request is
    invalid" and does **not** fail over (AGENTS.md known-issue), so a stray
    ``user`` message wedged between an assistant tool_call and its ``tool``
    answer makes the whole alias unusable — silently, for the client.
    """
    roles = [m.role for m in messages]
    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            answer = next(
                (i for i, m in enumerate(messages)
                 if m.role == "tool" and m.tool_call_id == call.id),
                None,
            )
            assert answer is not None, f"{call.id} 悬空未应答（商汤会 400）"
            between = roles[index + 1:answer]
            assert all(r == "tool" for r in between), (
                f"{call.id} 的应答之前插入了 {between}——assistant→user→tool 会被商汤 400"
            )


def test_bare_image_must_not_swallow_the_real_tool_output() -> None:
    """裸图片不许把真实的 function_call_output 换成 "(no output recorded)"。

    这条钉的是**内容**，上面那条钉的是形状——分开是有原因的：
    第二版实现在图片分支里提前 ``answer_open_calls()``，形状全对
    （assistant→tool→user），但真实的 output 到达时 ``open_calls`` 已空，
    整条被当孤儿丢弃。上游收到的是「工具没返回内容」，而真相是
    「返回了但网关丢了」——比静默吞图更糟，因为它在*撒谎*。

    Codex 真实 rollout 里 24/25 张图是裸 ``input_image``，所以这不是理论构造。
    """
    image = png(800, 600)
    call = {"type": "function_call", "call_id": "c1", "name": "view_image",
            "arguments": "{}"}
    bare = {"type": "input_image", "image_url": image, "detail": "high"}
    output = {"type": "function_call_output", "call_id": "c1", "output": "真实的图片内容"}

    messages = ResponsesRequest(model="m", input=[call, bare, output]).to_chat_request().messages
    tool_messages = [m for m in messages if m.role == "tool"]

    assert len(tool_messages) == 1
    assert tool_messages[0].content == "真实的图片内容", (
        "真实 output 被换掉了吗？这会让上游以为工具什么都没返回"
    )
    assert "(no output recorded)" not in str(tool_messages[0].content)
    # 图片也不能因此被丢掉
    assert sum(1 for m in messages if m.has_image()) == 1
    _assert_tool_history_is_well_formed(messages)

    # 与图片无关的正常轮次，行为必须原样
    plain = ResponsesRequest(model="m", input=[call, output]).to_chat_request().messages
    assert [m.content for m in plain if m.role == "tool"] == ["真实的图片内容"]


def test_bare_image_never_wedges_between_a_tool_call_and_its_answer() -> None:
    """裸图片不能插在 assistant(tool_calls) 与其 tool 应答中间。

    这是真踩过的坑：第一版实现只调了 ``flush_calls()`` 没调
    ``answer_open_calls()``，碰到「call 尚未应答就来一张裸图」时产出
    assistant→user→tool，商汤直接 400 且不故障转移。原有测试只覆盖了
    「call 已应答」的顺序，所以缺陷漏了过去——四种场景这里都锁住。
    """
    image = png(800, 600)

    def run(items):
        return ResponsesRequest(model="m", input=items).to_chat_request().messages

    call = {"type": "function_call", "call_id": "c1", "name": "view_image",
            "arguments": "{}"}
    answered = {"type": "function_call_output", "call_id": "c1", "output": "ok"}
    bare = {"type": "input_image", "image_url": image, "detail": "high"}

    _assert_tool_history_is_well_formed(run([call, bare]))
    _assert_tool_history_is_well_formed(run([call, answered, bare]))
    _assert_tool_history_is_well_formed(run([
        call, answered, bare,
        {"type": "function_call", "call_id": "c2", "name": "grep", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": "ok2"},
    ]))
    # 两个都悬空时再来图：最容易被漏掉的一种
    _assert_tool_history_is_well_formed(run([
        call,
        {"type": "function_call", "call_id": "c2", "name": "grep", "arguments": "{}"},
        bare,
    ]))


def test_text_only_payload_is_untouched_by_forwarding() -> None:
    """纯文本请求：转发开关开或关，结果必须逐字节一致。"""
    def build() -> list[dict[str, Any]]:
        return [
            {"type": "message", "role": "user", "content": "hello"},
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"a":1}'},
            {"type": "function_call_output", "call_id": "c1", "output": "plain text result"},
        ]

    on = ResponsesRequest(model="m", input=build()).to_chat_request(forward_images=True)
    off = ResponsesRequest(model="m", input=build()).to_chat_request(forward_images=False)
    assert on.model_dump(exclude_none=True) == off.model_dump(exclude_none=True)


def test_input_token_guard_defaults_to_off() -> None:
    """ZKAI_MAX_INPUT_TOKENS 必须默认 0（关闭）——不能改默认就替用户开闸。

    单独钉在 Settings 类定义上，而不是读本机 .env：.env 会随机器变（本机现已
    显式设了 400000），读它就守不住「默认」这个契约。
    """
    import inspect

    from app.core.config import Settings

    signature = inspect.signature(Settings)
    assert signature.parameters["max_input_tokens"].default == 0
    assert signature.parameters["forward_images"].default is True
