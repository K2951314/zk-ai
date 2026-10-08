"""消耗器明细日志的控制台通道（SSE + 降级 tail）。

消耗器是独立进程，日志在它自己的文件里（data/burn_sensenova.log，服务器上是
/var/lib/zkai/burner/burn_sensenova.log）——控制台从来读不到它，运营者想知道
「现在烧了多少」只能登陆服务器跑 journalctl。本模块把这个文件接进控制台，
让它在「🔥 积分消耗器」页里实时滚动。

两个端点：

- ``GET /admin/burner/log/tail``   一次性取尾 N 行。SSE 被代理挡住、浏览器
  不支持 EventSource 时的降级路径，前端还能拿它展示历史。
- ``GET /admin/burner/log/stream`` SSE 按字节偏移推增量行。

读文件的容错策略与 ``burner_service.read_status`` 一致：消耗器每分钟落盘，
读到半行的概率不为 0。半行不会报错，只会等下一次轮询剩余字节到齐再输出——
否则一条日志会被拆成两条推给前端。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse, StreamingResponse

from app.api.deps import ContainerDep
from app.core.logging import get_logger

logger = get_logger("api.burner_log")

router = APIRouter(prefix="/zkadmin/burner/log", tags=["burner"])

#: 轮询间隔（秒）。日志是 append 写、每分钟一条汇总，1 秒的粒度既不会有
#: 明显延迟，也不会把 CPU 烧在 stat 上。测试用 monkeypatch 调小它。
_POLL_SECONDS = 1.0

#: 日志文件名。与账本同目录（两者都属于同一个消耗器进程）；
#: zkai-burner.service 的 --log-file 指的就是这里，所以服务器上也一致。
_LOG_NAME = "burn_sensenova.log"


def _log_path(container: ContainerDep) -> Path:
    """消耗器日志的绝对路径。

    走 `Settings.burner_log_path` 而不是自己拼 data_dir：服务器上 burner 的
    日志在 `/var/lib/zkai/burner/`，比网关的 data_dir 深一层（见
    `Settings.resolved_burner_dir` 的注释）。
    """
    return container.settings.burner_log_path


def _leading_epoch(line: str) -> float | None:
    """'2026-10-03 18:48:29 INFO ...' -> epoch；不像日志行则 None。"""
    if len(line) < 19:
        return None
    try:
        return time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


#: 只读文件尾部这么多字节。日志一晚上能长到几百 MB（实测本地 105MB /
#: 63.7 万行），从头读会让这个端点卡十几秒——而它挂在一个会被自动刷新
#: 反复调用的页面上。256KB 约 2000 行日志，够预览又不拖慢页面。
_TAIL_BYTES = 256 * 1024


def _read_lines(path: Path, *, within_seconds: int) -> list[str]:
    """读文件尾部，只返回 within_seconds 内的完整行（0 = 不限时段）。

    时间解析不出来的行（异常日志、打印中断）保留——它们往往正是要找的那条，
    不能因为格式而丢。首行可能被截断，丢掉它（它不属于「尾部」的完整行）。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    start = max(0, size - _TAIL_BYTES)
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return []
    text = blob.decode("utf-8", errors="replace")
    if start > 0:
        # 丢掉被截断的第一行（不完整，连时间戳都不全）
        _head, sep, text = text.partition("\n")
        if not sep:
            return []
    horizon = time.time() - within_seconds if within_seconds > 0 else 0.0
    out: list[str] = []
    for raw in text.splitlines():
        ts = _leading_epoch(raw)
        if ts is not None and ts < horizon:
            continue
        out.append(raw)
    return out


def _warm_up(path: Path, *, lines: int, within_seconds: int) -> tuple[str, int]:
    """预热底料 + 随后的游标（= 读到历史那一刻的文件大小）。

    两个值必须同一次算出来：分开算的话，「读完历史」和「定游标」之间新追加的
    内容会被游标覆盖掉——那正是半行丢失的路径。
    """
    history = _read_lines(path, within_seconds=within_seconds)
    if len(history) > lines:
        history = history[-lines:]
    text = "".join("data: " + line + "\n\n" for line in history)
    text += "data: 已连接，开始实时跟随 " + str(path) + "\n\n"
    try:
        offset = path.stat().st_size
    except OSError:
        offset = 0
    return text, offset


def _read_tail(path: Path, offset: int) -> tuple[bytes, int]:
    """从 offset 读到文件尾，返回 (新字节, 新 offset)。

    同步且阻塞，但文件只有几 KB~几 MB 的纯文本、每秒一次，阻塞一次的代价
    远低于把整个 tail 循环改写成线程池 + 事件的复杂度。
    """
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(size - offset)
    return data, size


async def _follow(path: Path, *, lines: int, within_seconds: int):
    """按字节偏移 tail，只输出完整行；文件被截断或旋转时重置。"""
    warm, offset = await asyncio.to_thread(_warm_up, path, lines=lines, within_seconds=within_seconds)
    yield warm
    carry = ""
    while True:
        await asyncio.sleep(_POLL_SECONDS)
        try:
            data, size = await asyncio.to_thread(_read_tail, path, offset)
        except FileNotFoundError:
            continue  # 文件被旋转 / 删除临时：等它回来，不退出
        except OSError:
            continue
        if size < offset:
            offset, carry = 0, ""
            continue
        if size == offset:
            continue
        offset = size
        text = carry + data.decode("utf-8", errors="replace")
        if text.endswith("\n"):
            carry = ""
            complete = text.splitlines()
        else:
            head, sep, carry = text.rpartition("\n")
            complete = head.splitlines() if sep else []
        for line in complete:
            yield "data: " + line + "\n\n"


@router.get(
    "/tail",
    summary="消耗器日志尾部（一次性）",
    response_class=PlainTextResponse,
)
async def burner_log_tail(
    container: ContainerDep,
    lines: int = Query(200, ge=1, le=2000, description="返回多少行"),
    within_seconds: int = Query(
        86400, ge=0, le=7 * 86400, description="只看这么多过去多少秒"
    ),
) -> PlainTextResponse:
    path = _log_path(container)
    body = "\n".join(_read_lines(path, within_seconds=within_seconds)[-lines:])
    if not body and not path.exists():
        body = (
            "\n还没有日志：" + str(path)
            + "\n消耗器可能还没跑过，或它的日志文件指向了别处。"
        )
    return PlainTextResponse(body + "\n", headers={"Cache-Control": "no-store"})


@router.get("/stream", summary="消耗器日志实时推送（SSE）")
async def burner_log_stream(
    container: ContainerDep,
    lines: int = Query(200, ge=1, le=2000, description="先发多少行历史"),
    within_seconds: int = Query(
        86400, ge=0, le=7 * 86400, description="历史只看这么多过去多少秒"
    ),
) -> StreamingResponse:
    path = _log_path(container)
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        # nginx / Caddy 都不能把这个响应缓冲成一次性返回，否则「实时」毫无意义。
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _follow(path, lines=lines, within_seconds=within_seconds),
        media_type="text/event-stream",
        headers=headers,
    )
