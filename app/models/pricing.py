"""参考价格获取：从公开聚合渠道拉取模型挂牌价。

注意口径：这里拿到的是聚合渠道的挂牌单价，不是各供应商给您的结算价。
两者可能一致也可能有差，且渠道存在按时段的浮动调价。因此本模块
只负责提供“参考价”，配置里必须同时记录来源，禁止冒充官方结算价。

单位统一换算为 USD / 1M tokens，与配置里的字段口径一致。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from app.core.logging import get_logger

logger = get_logger("models.pricing")

_SOURCE = "https://openrouter.ai/api/v1/models"
_SOURCE_NAME = "openrouter-reference"
_CACHE = Path("/var/lib/zkai/reference_price_cache.json")
_TTL = 24 * 3600
_TIMEOUT = 20.0


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
        logger.info("price cache write failed: %s", exc)


def normalize(model_id: str) -> str:
    """``deepseek-ai/DeepSeek-V4.1-Flash-1017`` -> ``deepseek-v4.1-flash-1017``."""
    name = model_id.split("/")[-1].strip().lower()
    return name.replace("_", "-")


def _per_mtok(raw) -> float:
    """渠道给的是每 token 美元价，换算成每百万 token。"""
    try:
        return round(float(raw) * 1_000_000, 6)
    except (TypeError, ValueError):
        return 0.0


def _parse(payload: dict) -> dict:
    table: dict[str, dict] = {}
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "")
        if not model_id:
            continue
        pricing = item.get("pricing") or {}
        input_price = _per_mtok(pricing.get("prompt"))
        output_price = _per_mtok(pricing.get("completion"))
        if not input_price and not output_price:
            continue
        entry = {
            "input_cost_per_mtok": input_price,
            "output_cost_per_mtok": output_price,
            "context_window": item.get("context_length"),
            "channel_model": model_id,
            "price_source": _SOURCE_NAME,
            "fetched_at": int(time.time()),
        }
        # 同时登记全名与去掉厂商前缀的名字，便于用本地模型名直接命中。
        table[model_id.lower()] = entry
        table.setdefault(normalize(model_id), entry)
    return table


async def reference_prices(force: bool = False) -> dict:
    """返回归一化模型名到参考价的映射；缓存一天，失败时沿用旧缓存。"""
    cache = _load()
    stamp = int(cache.get("_fetched_at") or 0)
    if not force and cache and time.time() - stamp < _TTL:
        return {k: v for k, v in cache.items() if not k.startswith("_")}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(_SOURCE, headers={"User-Agent": "zkai-gateway"})
        if response.status_code != 200:
            raise ValueError("HTTP %s" % response.status_code)
        table = _parse(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("reference price fetch failed: %s", exc)
        return {k: v for k, v in cache.items() if not k.startswith("_")}
    table["_fetched_at"] = int(time.time())
    _save(table)
    return {k: v for k, v in table.items() if not k.startswith("_")}


def lookup(table: dict, model_id: str):
    """按全名、再去厂商前缀、最后做包含匹配，取不到的返回 None。"""
    if not table:
        return None
    lowered = model_id.lower()
    if lowered in table:
        return table[lowered]
    name = normalize(model_id)
    if name in table:
        return table[name]
    # 型号带日期后缀时（-1017 / -0731）渠道名可能不带，反向也成立。
    for key, value in table.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        if key.endswith(name) or name.endswith(key):
            return value
    return None


__all__ = ["lookup", "normalize", "reference_prices"]
