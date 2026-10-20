"""从模型社区官方资料补全上游目录里缺失的事实。

上游模型列表通常只回答 id、归属和创建时间，没有上下文长度、能力倾向
和介绍文案。社区的资料接口能给出这些事实：Name、Description、
ReadMeContent、Tasks、Downloads。

与 discovery 模块同一条原则：官方没说的不猜，只补能确证的事实；
网络失败一律静默降级，不影响模型市场本身可用。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx

from app.core.logging import get_logger

logger = get_logger("models.catalogue_info")

_URL = "https://modelscope.cn/api/v1/models/{mid}"
_CACHE = Path("/var/lib/zkai/model_info_cache.json")
_TTL = 7 * 24 * 3600
_CONCURRENCY = 4
_TIMEOUT = 10.0

#: 说明文档里写上下文窗口的几种真实写法，按命中优先级排列。
#: 表格写法（`| `context_window` | 1M tokens |`）必须排在最前：
#: DeepSeek-V4.1-Flash-1017 只在表格里写了 1M，旧规则认不出，
#: 结果会回退到预置表的旧值 256K，把真实上限说小了。
_CTX_PATTERNS = (
    re.compile(r"context[_ ]?(?:window|length)`?\s*[|:]\s*`?(\d+(?:\.\d+)?)\s*([kKmM])", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*([kKmM])[-\s]?tokens?\s+context", re.I),
    re.compile(r"context\s+(?:window|length)[^.\n]{0,40}?(\d+(?:\.\d+)?)\s*([kKmM])\b", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*([kKmM])\s*context\s+window", re.I),
)
_MULT = {"k": 1024, "m": 1024 * 1024}

_VISION = ("视觉", "多模态", "vision", "image", "multimodal")
_REASON = ("reasoning", "推理", "thinking")
_TOOL = ("function call", "tool use", "工具调用")


def _load() -> dict:
    try:
        data = json.loads(_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(cache: dict) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE)
    except OSError as exc:
        logger.info("info cache write failed: %s", exc)


def _context_window(text: str):
    """说明文档里明确写出的上下文窗口；没写就是 None，不按名字猜。"""
    for pattern in _CTX_PATTERNS:
        found = pattern.search(text)
        if found:
            return int(float(found.group(1)) * _MULT[found.group(2).lower()])
    return None


def _describe(data: dict, mid: str) -> dict:
    tasks = [
        str(item.get("ChineseName") or item.get("Name") or "").strip()
        for item in (data.get("Tasks") or [])
        if isinstance(item, dict)
    ]
    tasks = [task for task in tasks if task]
    text = data.get("ReadMeContent") or ""
    desc = str(data.get("Description") or "").strip()
    if not desc:
        bits = []
        if tasks:
            bits.append("、".join(tasks[:3]))
        downloads = data.get("Downloads")
        if isinstance(downloads, int) and downloads > 0:
            bits.append("社区下载 %d 次" % downloads)
        desc = "｜".join(bits)
    lowered = (text + " " + " ".join(tasks)).lower()
    return {
        "display_name": str(data.get("Name") or mid.split("/")[-1]).strip(),
        "description": desc,
        "context_window": _context_window(text),
        "vision": True if any(word in lowered for word in _VISION) else None,
        "reasoning": True if any(word in lowered for word in _REASON) else None,
        "tool_use": True if any(word in lowered for word in _TOOL) else None,
        "license": data.get("License"),
        "downloads": data.get("Downloads"),
        "source": "modelscope",
        "fetched_at": int(time.time()),
    }


async def _one(client: httpx.AsyncClient, mid: str):
    try:
        response = await client.get(
            _URL.format(mid=mid), headers={"User-Agent": "zkai-gateway"}
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("detail failed %s: %s", mid, exc)
        return None
    data = payload.get("Data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    return _describe(data, mid)


async def enrich_modelscope(model_ids):
    """按上游 model id 返回官方资料，缓存命中直接复用，缺失的并发补齐。"""
    cache = _load()
    now = time.time()
    out = {}
    todo = []
    for mid in model_ids:
        hit = cache.get(mid)
        if isinstance(hit, dict) and now - float(hit.get("fetched_at", 0)) < _TTL:
            out[mid] = hit
        else:
            todo.append(mid)
    if todo:
        sem = asyncio.Semaphore(_CONCURRENCY)

        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            async def work(item):
                async with sem:
                    return item, await _one(client, item)

            got = await asyncio.gather(*(work(item) for item in todo))
        for item, info in got:
            if info:
                cache[item] = info
                out[item] = info
        _save(cache)
    return out


__all__ = ["enrich_modelscope"]
