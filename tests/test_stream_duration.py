"""流式请求的总墙钟（stream_duration_ms，2026-11-13）。

背景：``latency_ms`` 记的是**首块延迟**（路由决策有多快），而
``client_disconnected`` 那批行显示 ``latency_ms=0.0`` —— 运营者分不清
「首字节慢」和「生成慢」，两者的对策完全不同（前者换渠道，后者减上下文）。

所以新增独立字段而不是改 ``latency_ms`` 的语义：后者被控制台与既有统计按
「首字节多快」读着，改它会静默改变所有历史对比。
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.request import ChatCompletionRequest
from tests.conftest import Behavior


async def _latest_request_row(harness) -> dict:
    rows = await harness.container.request_repository.recent(limit=5)
    assert rows, "requests 表里应该有这次请求"
    return rows[0]


async def test_stream_records_the_whole_stream_duration(harness) -> None:
    """流跑完 → stream_duration_ms > 0，且这条记录标记为 stream。"""
    harness.adapter.queue(Behavior(chunks=3, delay=0.05))

    await harness.request(stream=True)

    row = await _latest_request_row(harness)
    assert row["stream"] is True
    assert row["stream_duration_ms"] > 0, "流式请求必须记录整条流的总时长"
    # 三次 0.05s 的间隔，至少 100ms
    assert row["stream_duration_ms"] >= 100


async def test_non_stream_request_leaves_the_field_at_zero(harness) -> None:
    """非流式不写这个字段（0 = 没测量，不是「瞬时」）。"""
    harness.adapter.queue(Behavior(text="ok"))

    await harness.request(stream=False)

    row = await _latest_request_row(harness)
    assert row["stream"] is False
    assert row["stream_duration_ms"] == 0


async def test_stream_duration_is_not_smaller_than_first_byte_latency(harness) -> None:
    """两个字段量的是不同的东西：总时长 >= 首块延迟。

    这条是「别把两个字段合成一个」的理由：慢生成（长 tail）与慢首字节
    （长 gate）本就不同，合并后其中一个信息必然丢失。
    """
    harness.adapter.queue(Behavior(chunks=4, delay=0.06))

    await harness.request(stream=True)

    row = await _latest_request_row(harness)
    assert row["stream_duration_ms"] >= row["latency_ms"]


async def test_cancelled_stream_still_records_how_long_it_ran(harness) -> None:
    """客户端中途断开时 ``meta`` 可能还没有，但「跑了多久才断」必须留下。

    这是排查「为什么老断开」最需要的那一个数：``latency_ms=0.0`` 只说明
    没拿到首块，说明不了它在断开前已经耗了多久。
    """
    harness.adapter.queue(Behavior(chunks=5, delay=0.4))
    stream = harness.service.stream(
        ChatCompletionRequest(
            model="fake-model",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        ),
        request_id="cancel-req",
    )
    iterator = stream.__aiter__()
    await anext(iterator)  # 拿到第一个事件后立刻放手，模拟客户端断开
    await asyncio.sleep(0.05)
    await stream.aclose()

    row = await _latest_request_row(harness)
    assert row["status"] == "cancelled"
    assert row["stream_duration_ms"] > 0, "被取消的流也要留下它跑了多久"


async def test_cancelled_stream_after_meta_is_attributed_to_the_client(harness) -> None:
    """已经收到 meta 后客户端断开，也不能留下 error_type=NULL。"""
    harness.adapter.queue(Behavior(chunks=5, delay=0.2))
    stream = harness.service.stream(
        ChatCompletionRequest(
            model="fake-model",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        ),
        request_id="cancel-after-meta",
    )
    async for event in stream:
        if event.type == "meta":
            break
    await stream.aclose()

    row = await _latest_request_row(harness)
    assert row["status"] == "cancelled"
    assert row["error_type"] == "client_disconnected"


async def test_cancelled_stream_after_upstream_error_is_client_disconnected(harness, monkeypatch) -> None:
    """取消状态优先归因客户端断开，不能保留上游 error_type。"""
    original = harness.adapter.stream

    async def failing_stream(request, ctx):
        async for chunk in original(request, ctx):
            yield chunk
            raise RuntimeError("midstream broken")

    monkeypatch.setattr(harness.adapter, "stream", failing_stream)
    stream = harness.service.stream(
        ChatCompletionRequest(
            model="fake-model",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        ),
        request_id="cancel-after-error",
    )
    async for event in stream:
        if event.type == "error":
            break
    await stream.aclose()

    row = await _latest_request_row(harness)
    assert row["status"] == "cancelled"
    assert row["error_type"] == "client_disconnected"


async def test_cancelled_non_stream_request_persists_attempt_row(harness) -> None:
    """非流式取消时，调度器已记录的取消 attempt 必须落库。"""
    harness.adapter.queue(Behavior(delay=1))
    request = ChatCompletionRequest(
        model="fake-model",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
    )
    task = asyncio.create_task(harness.service.chat(request, request_id="cancel-chat"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await _latest_request_row(harness)
    attempts = await harness.container.request_repository.attempts_for("cancel-chat")
    assert row["status"] == "cancelled"
    assert row["error_type"] == "client_disconnected"
    assert row["attempts"] == 1
    assert len(attempts) == 1
    assert attempts[0]["status"] == "cancelled"


async def test_cancelled_stream_is_attributed_to_the_client_not_missing_meta(harness) -> None:
    """2026-11-26：499 断开不得再记成 `no_stream_meta`。

    线上 55 条 499 全被记成 no_stream_meta，看起来像"网关没产出 meta"，
    实际是客户端 60 秒硬超时。归因错了排查方向就错了——这是可观测性 bug，
    不修的话下次还会照着错误的类型去翻路由代码。
    """
    harness.adapter.queue(Behavior(chunks=5, delay=0.4))
    stream = harness.service.stream(
        ChatCompletionRequest(
            model="fake-model",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        ),
        request_id="cancel-attribution",
    )
    iterator = stream.__aiter__()
    await anext(iterator)
    await stream.aclose()

    row = await _latest_request_row(harness)
    assert row["status"] == "cancelled"
    assert row["error_type"] == "client_disconnected"
    assert row["error_type"] != "no_stream_meta"
