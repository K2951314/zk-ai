"""``POST /v1/chat/completions`` - the OpenAI-compatible entry point.

Compatibility notes
-------------------
* The request model accepts **every** parameter an OpenAI SDK may send; unknown
  ones are recorded and (only when a provider can map them) forwarded. Nothing is
  ever dropped silently.
* Streaming uses pure ``data:`` chunks terminated by ``data: [DONE]`` so the
  official SDKs parse it unchanged. Routing metadata travels in an SSE
  **comment** (``: zkai-meta {...}``) plus response headers, which every SSE
  parser ignores - it can never corrupt a client's chunk stream.
* ``usage`` is only included in the final chunk when ``stream_options.include_usage``
  is set, exactly like OpenAI.
* Client disconnects cancel the upstream request and are recorded as ``cancelled``.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import ContainerDep, client_metadata, require_client_auth
from app.core.errors import ZKAIError
from app.core.logging import get_logger
from app.models.request import ChatCompletionRequest, new_request_id
from app.models.response import ChatCompletionChunk, StreamEvent

logger = get_logger("api.chat")

router = APIRouter(tags=["chat"], dependencies=[Depends(require_client_auth)])

SSE_DONE = "data: [DONE]\n\n"
#: 流式空闲心跳（SSE 注释行）。客户端按规范忽略 `:` 开头的行，
#: 所以它只解决「长时间零字节被判死」，不改变任何数据语义。
SSE_PING = ": ping\n\n"


_REASONING_KEYS = ("reasoning_content", "reasoning", "content_recovered_from_reasoning")


def _strip_reasoning_fields(payload: dict[str, Any]) -> None:
    """Drop thinking fields from a serialized response/chunk.

    The switch means "I never want to see the thinking", so reasoning keys are
    stripped unconditionally; when the answer was cut off and thinking was
    promoted into ``content`` (the never-blank guard, marked
    ``content_recovered_from_reasoning``), the promoted text is blanked too —
    a blank reply under an explicit hide-thinking switch is the honest result.
    （截断信号：非流式由回填路径补 ``finish_reason=length``；流式回填路径目前
    不补 finish_reason，这是已知的轻微不一致，见 README §18 缺陷 11。）
    """
    for choice in payload.get("choices") or []:
        for section in ("delta", "message"):
            body = choice.get(section)
            if not isinstance(body, dict):
                continue
            if body.get("content_recovered_from_reasoning"):
                body["content"] = ""
            for key in _REASONING_KEYS:
                body.pop(key, None)


def _chunk_payload(
    chunk: ChatCompletionChunk, *, include_usage: bool, strip_reasoning: bool = False
) -> dict[str, Any]:
    payload = chunk.model_dump(exclude_none=True)
    if not include_usage:
        payload.pop("usage", None)
    if strip_reasoning:
        _strip_reasoning_fields(payload)
    return payload


#: 需要心跳 / 能正确忽略 `:` 注释行的客户端 User-Agent 关键词（白名单）。
#: 只有这些客户端在长时间静默后会判连接失败并断连——它们也是遵守 SSE 规范、
#: 能正确忽略 `:` 注释行的（心跳与 zkai-meta 注释行共用这份白名单）。
#: OpenAI 官方 SDK 的 UA 有两种写法，必须都覆盖（2026-11-19 实测）：
#:   新版（Stainless）：`OpenAI/Python 1.55.0` / `OpenAI/JS 4.104.0`
#:   旧式：`openai-python/1.0.0` / `openai-node/4.0.0`
#: 匹配在 lower() 后做子串包含，所以两种写法各列一个关键词。
#: WorkBuddy 桌面版、Anthropic SDK、浏览器 EventSource 不在名单里：它们要么
#: 不遵守 SSE 规范把 `: ping` 当正文显示（WorkBuddy），要么超时容忍度够长不需要心跳。
#: 未知 UA **默认不发**——宁可让需要心跳的客户端偶尔断连（它会重试），
#: 也不要让不兼容的客户端显示污染信息。
_PING_USER_AGENTS = (
    "codex",
    #: 2026-11-23 修：真实 UA 是 ``deepseek-harness/0.2.2-rc.5
    #: (+https://github.com/deepseek-ai/deepseek-harness) dsh-profile/desktop``，
    #: 而名单里写的是 ``ds-harness`` —— 子串匹配不上，于是这条「为 harness 加
    #: 的心跳」从上线起就没对 harness 生效过（服务器 DB 实测 618 条 harness
    #: 流式请求，零心跳）。两个写法都保留：官方 ID 可能变，别名别丢。
    "deepseek-harness",
    "ds-harness",
    "openai-python",  # 旧式 OpenAI Python SDK
    "openai-node",  # 旧式 OpenAI Node SDK
    "openai/python",  # 新版 openai-python（Stainless）
    "openai/js",  # 新版 openai-node（Stainless）
)


def keepalive_interval(
    idle_timeout: float | None,
    *,
    user_agent: str | None = None,
    extra_keywords: Sequence[str] = (),
) -> float | None:
    """按客户端空闲判定的一半计算心跳间隔；None = 不发心跳。

    ``stream_idle_timeout`` 是「客户端能忍多久没有字节」。心跳必须在它**之前**
    到达，取一半留一倍余量（120s → 每 60s 一次 ping）。

    0 或负数 = 关闭心跳（回到旧行为），便于以后排查「心跳是否影响某个客户端」。

    **按 User-Agent 白名单发心跳**（2026-11-18）：只有已知会在长时间静默后断连
    的客户端才发。WorkBuddy 桌面版的 SSE 解析器不遵守规范、把 `: ping` 注释行
    当正文显示——对它发心跳 = 污染输出。未知 UA 默认不发，安全侧。
    ``extra_keywords`` 来自配置 ``request.sse_comment_user_agents``（追加名单）。
    """
    if idle_timeout is None or idle_timeout <= 0:
        return None
    if not wants_sse_comments(user_agent, extra_keywords):
        return None
    return max(5.0, idle_timeout / 2.0)


def extra_sse_ua_keywords(raw: str | None) -> tuple[str, ...]:
    """把配置里的逗号分隔 UA 关键词解析成小写元组（空/空白项丢弃）。"""
    return tuple(part.strip().lower() for part in (raw or "").split(",") if part.strip())


def wants_sse_comments(
    user_agent: str | None, extra_keywords: Sequence[str] = ()
) -> bool:
    """客户端是否能正确忽略 SSE `:` 注释行（心跳 ping / zkai-meta 都用这个判断）。

    两类注释行都是「合规 SSE 解析器忽略、不合规客户端当正文显示」：
    WorkBuddy 桌面版把 `: ping` / `: zkai-meta` 当正文显示（不遵守 SSE 规范），
    Anthropic SDK 超时容忍度够长，浏览器 EventSource 也不会在静默期断连。
    只有 Codex CLI / ds harness / OpenAI SDK 会在零字节静默后判连接失败、
    同时也是能正确忽略注释行的合规解析器。

    内置名单永远生效；``extra_keywords``（配置追加）让新客户端不用改代码
    就能进名单——硬编码名单必然随客户端生态漂移（2026-11-19 新版 OpenAI
    SDK 的 UA 写法变化刚暴露过一次）。
    """
    if not user_agent:
        return False
    ua_lower = user_agent.lower()
    if any(keyword in ua_lower for keyword in _PING_USER_AGENTS):
        return True
    return any(keyword in ua_lower for keyword in extra_keywords)


def retry_after_headers(exc: ZKAIError) -> dict[str, str]:
    """``Retry-After`` for throttled / pool-blackout errors, if known.

    A client that hammers a pool in cooldown every 2 seconds only makes the
    blackout worse; the header (paired with ``error.retry_after`` in the body)
    gives well-behaved SDKs something to back off by.
    """
    retry_after = getattr(exc, "retry_after", None)
    if not retry_after or retry_after <= 0:
        return {}
    return {"retry-after": str(math.ceil(retry_after))}


def error_response(exc: ZKAIError) -> JSONResponse:
    """Serialise a gateway error into an OpenAI-shaped error envelope."""
    return JSONResponse(
        status_code=exc.http_status, content=exc.to_dict(), headers=retry_after_headers(exc)
    )


def routing_headers(response_meta: dict[str, Any]) -> dict[str, str]:
    """Headers that expose routing decisions without touching the body."""
    headers = {"x-zkai-request-id": str(response_meta.get("request_id", ""))}
    for key, header in (
        ("provider", "x-zkai-provider"),
        ("credential_id", "x-zkai-credential"),
        ("alias", "x-zkai-alias"),
        ("resolved_model", "x-zkai-model"),
        ("attempt", "x-zkai-attempt"),
    ):
        value = response_meta.get(key)
        if value is not None:
            headers[header] = str(value)
    if response_meta.get("fallback_used"):
        headers["x-zkai-fallback"] = "true"
    return headers


@router.post("/v1/chat/completions", summary="创建一次对话补全")
async def chat_completions(
    payload: ChatCompletionRequest,
    container: ContainerDep,
    request: Request,
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
) -> Any:
    """Route, execute and account for a chat completion."""
    request_id = x_request_id or new_request_id()
    metadata = client_metadata(request)

    if payload.stream:
        return await _stream_response(payload, container, request_id, metadata, request)
    return await _json_response(payload, container, request_id, metadata)


async def _json_response(
    payload: ChatCompletionRequest,
    container: ContainerDep,
    request_id: str,
    metadata: dict[str, str | None],
) -> Any:
    try:
        response = await container.request_service.chat(
            payload,
            request_id=request_id,
            client_ip=metadata.get("client_ip"),
            user_agent=metadata.get("user_agent"),
        )
    except ZKAIError as exc:
        logger.warning("request %s failed: %s (%s)", request_id, exc.error_type, exc.message)
        return error_response(exc)

    headers = routing_headers(response.zk_ai.model_dump()) if response.zk_ai else {}
    body = response.model_dump(exclude_none=True)
    if container.settings.strip_reasoning:
        _strip_reasoning_fields(body)
    return JSONResponse(status_code=200, content=body, headers=headers)


async def _stream_response(
    payload: ChatCompletionRequest,
    container: ContainerDep,
    request_id: str,
    metadata: dict[str, str | None],
    request: Request,
) -> Any:
    """Start the stream immediately; prefetch only within a bounded window."""
    generator = container.request_service.stream(
        payload,
        request_id=request_id,
        client_ip=metadata.get("client_ip"),
        user_agent=metadata.get("user_agent"),
    )
    iterator = generator.__aiter__()
    #: 预取窗口：第一个事件到达前最多等这么久。None = 不设超时（旧行为）。
    #: 见 Settings.stream_prefetch_timeout —— 预取期间一个字节都发不出去，
    #: 而调度器的故障转移全在这个窗口里，窗口开多久客户端就「静默」多久。
    prefetch_timeout = container.settings.stream_prefetch_timeout or None
    #: 预取：先发一个 ``anext``，但**最多等 stream_prefetch_timeout 秒**。
    #: 超时**不取消**它——把在飞的那个 task 原样交给流（「跨心跳同一 task」
    #: 不变式），先返回 200 + 心跳，让客户端立刻知道连接是活的。
    #: 没有这一段，预取期间一个字节都发不出去，而调度器的换 Key / 换部署 /
    #: 退避重试全在这个窗口里（2026-11-23 服务器实测 harness 首块 p90=30s、
    #: p99=141s，尾部请求在客户端看来就是「永远没有响应」）。
    first_event: StreamEvent | None = None
    pending: asyncio.Future[Any] | None = None
    prefetch = asyncio.ensure_future(anext(iterator))
    try:
        done, _ = await asyncio.wait(
            {prefetch},
            timeout=None if prefetch_timeout is None else prefetch_timeout,
        )
        if prefetch in done:
            first_event = prefetch.result()
        else:
            pending = prefetch
    except StopAsyncIteration:
        first_event = None
    except ZKAIError as exc:
        await _aclose(generator)
        logger.warning("stream request %s rejected: %s", request_id, exc.error_type)
        return error_response(exc)
    except asyncio.CancelledError:
        prefetch.cancel()
        await _aclose(generator)
        raise

    include_usage = bool((payload.stream_options or {}).get("include_usage"))
    strip_reasoning = container.settings.strip_reasoning
    headers = {
        "x-zkai-request-id": request_id,
        "x-zkai-stream": "true",
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
    }

    ping_every = keepalive_interval(
        container.settings.stream_idle_timeout,
        user_agent=metadata.get("user_agent"),
        extra_keywords=extra_sse_ua_keywords(container.settings.sse_comment_user_agents),
    )
    meta_allowed = wants_sse_comments(
        metadata.get("user_agent"),
        extra_sse_ua_keywords(container.settings.sse_comment_user_agents),
    )

    async def event_stream() -> AsyncIterator[str]:
        watcher = asyncio.create_task(_watch_disconnect(request))
        # 在飞的那个 anext 必须**跨心跳保持同一个 task**：
        # 每轮重新 ensure_future 会让上一轮还在跑的 generator 被第二次驱动
        # （``anext(): asynchronous generator is already running``）。
        # ``pending`` 可能是预取超时时留下的那一个（见上），不能丢掉。
        inflight: asyncio.Future[Any] | None = pending
        try:
            # 预取窗口超时时先发 HTTP 200；立即补首个心跳，避免再等一个 ping_every。
            # ping_every is not None 仅对 SSE 注释行兼容的 UA 成立，不会污染非白名单客户端。
            if pending is not None and ping_every is not None:
                yield SSE_PING
            event = first_event
            while True:
                # event 为 None = 心跳把上一格发完了，该去等下一个。
                if event is not None:
                    if event.type == "chunk" and event.chunk is not None:
                        payload_json = json.dumps(
                            _chunk_payload(
                                event.chunk,
                                include_usage=include_usage,
                                strip_reasoning=strip_reasoning,
                            ),
                            ensure_ascii=False,
                        )
                        yield f"data: {payload_json}\n\n"
                    elif event.type == "meta" and event.meta is not None:
                        # SSE comment: invisible to compliant SSE parsers by design.
                        # 但 WorkBuddy 桌面版把 `:` 注释行当正文显示（与 `: ping`
                        # 同一个根因），对它发 = 污染输出。
                        # 注意：流式响应头只带 request-id/stream 等基础字段，
                        # meta 的 provider/model/credential 等**只在**这条注释行里——
                        # 它先于流式 body 发出，没法塞进 HTTP 头。所以这不是
                        # 「锦上添花」，而是对合规客户端才有意义的补充信息；
                        # 对不合规客户端不发，优先保证输出不被污染。
                        if meta_allowed:
                            yield f": zkai-meta {json.dumps(event.meta.model_dump(), ensure_ascii=False)}\n\n"
                    elif event.type == "error" and event.error is not None:
                        yield f"data: {json.dumps(event.error, ensure_ascii=False)}\n\n"
                    elif event.type == "end":
                        break
                    # 这一格已发完：置空，绝不重复发（心跳不能推动数据）。
                    event = None
                # ``anext`` returns an Awaitable, not a Coroutine, so wrap it with
                # ensure_future (create_task would reject it).
                if inflight is None:
                    inflight = asyncio.ensure_future(anext(iterator))
                # timeout 只表示「该发心跳了」：不取消 inflight，上游照常推进。
                # 没有它的时候，上游静默多久，客户端就多久收不到一个字节。
                done, _ = await asyncio.wait(
                    {inflight, watcher},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=ping_every,
                )
                if inflight not in done:
                    if watcher.done():
                        # Client vanished: cancel upstream and stop producing.
                        inflight.cancel()
                        inflight = None
                        logger.info("client disconnected during stream %s", request_id)
                        break
                    # 只是静默：SSE 注释行保活，循环继续等**同一个** inflight。
                    if ping_every is not None:
                        yield SSE_PING
                    continue
                finished, inflight = inflight, None
                try:
                    event = finished.result()
                except StopAsyncIteration:
                    break
                except ZKAIError as exc:
                    # 预取超时后响应头已发出（200），此时调度器才把
                    # AllAttemptsFailed 之类抛出来——不能再抛给框架
                    # （Starlette 会报 "response already started" 并吞掉客户端唯一的
                    # 错误信号），必须转成流内 error 事件（2026-11-23 真机抓到）。
                    logger.warning(
                        "stream %s failed after the response started: %s",
                        request_id,
                        exc.error_type,
                    )
                    yield f"data: {json.dumps(exc.to_dict(), ensure_ascii=False)}\n\n"
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("stream %s crashed mid-flight", request_id)
                    yield (
                        "data: "
                        + json.dumps(
                            {"error": {"message": "网关内部错误", "type": "internal_error", "code": 500}},
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    break
            yield SSE_DONE
        finally:
            if inflight is not None:
                inflight.cancel()
            watcher.cancel()
            await _aclose(generator)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


async def _watch_disconnect(request: Request, interval: float = 0.25) -> None:
    """Complete as soon as the client goes away."""
    while True:
        if await request.is_disconnected():
            return
        await asyncio.sleep(interval)


async def _aclose(generator: Any) -> None:
    """Close an async generator without masking the original error."""
    aclose = getattr(generator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        logger.debug("error closing stream generator", exc_info=True)


__all__ = [
    "SSE_DONE",
    "SSE_PING",
    "error_response",
    "extra_sse_ua_keywords",
    "keepalive_interval",
    "retry_after_headers",
    "router",
    "routing_headers",
    "wants_sse_comments",
]
