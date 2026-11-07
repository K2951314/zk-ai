"""工具调用账本：每次 ``mcp_call`` 落一行 JSON，供控制台看"花了多少、成不成"。

为什么用追加文件而不是新表：
- 不动网关核心库的表结构，零迁移风险；
- 一行一条、坏了只坏一行，读的时候跳过即可；
- 单人单机量级（一天几十到几百条）完全够用。

约定：
- 路径 ``<data_dir>/mcp_usage.jsonl``，可用 ``ZKAI_DATA_DIR`` 覆盖（与托盘一致）；
- 记账失败**绝不影响主流程**（记不上就算了，不能因为写日志把任务搞挂）；
- 只记工具名、服务、耗时、成败、任务空间和会话号，**不记参数内容**
  （参数里可能有文件路径或业务数据，落盘要谨慎）。
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

#: 账本文件名
LEDGER_NAME = "mcp_usage.jsonl"
#: 读取时最多回看多少行（避免大文件全量加载）
_MAX_SCAN = 5_000


def ledger_path() -> Path:
    """账本路径；``ZKAI_DATA_DIR`` 优先（测试用 tmp_path 隔离）。"""
    return Path(os.environ.get("ZKAI_DATA_DIR", "data")) / LEDGER_NAME


def record(
    *,
    tool: str,
    server: str,
    session_id: str,
    task_space: str,
    ok: bool,
    elapsed: float,
    error: str = "",
) -> None:
    """追加一条调用记录；任何异常都吞掉，不影响调用方。"""
    _append(
        {
            "kind": "mcp",
            "ts": round(time.time(), 3),
            "tool": tool,
            "server": server,
            "session_id": session_id,
            "task_space": task_space,
            "ok": bool(ok),
            "elapsed": round(float(elapsed), 3),
            "error": error[:200],
        }
    )


def record_delegation(
    *,
    tool: str,
    model: str,
    role: str,
    session_id: str,
    task_space: str,
    ok: bool,
    elapsed: float,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    error: str = "",
) -> None:
    """追加一条**外部执行体委派**记录（含上游自报用量）。

    CLI 工具没有价目表，唯一客观的价格来源就是这里累计出来的实测成本。
    ``cost_usd`` 为 None 表示这次没拿到自报成本——**不要用 0 代替**，
    否则会把「不知道」算成「免费」。
    """
    _append(
        {
            "kind": "delegation",
            "ts": round(time.time(), 3),
            "tool": tool,
            "model": model,
            "role": role,
            "session_id": session_id,
            "task_space": task_space,
            "ok": bool(ok),
            "elapsed": round(float(elapsed), 3),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "error": error[:200],
        }
    )


def _append(entry: dict[str, Any]) -> None:
    """写一行；任何 OSError 都吞掉（记不上账不能把任务搞挂）。"""
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """最近 N 条记录（新的在前）；坏行跳过。"""
    path = ledger_path()
    if not path.is_file():
        return []
    rows: deque[dict[str, Any]] = deque(maxlen=max(1, min(limit, _MAX_SCAN)))
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return list(reversed(rows))


def summary(limit: int = _MAX_SCAN) -> list[dict[str, Any]]:
    """按工具聚合：调用次数、成功数、平均耗时、最近一次时间。"""
    stats: dict[str, dict[str, Any]] = {}
    for item in recent(limit):
        tool = str(item.get("tool") or "未知")
        bucket = stats.setdefault(
            tool,
            {"tool": tool, "calls": 0, "ok": 0, "total_elapsed": 0.0, "last_ts": 0.0},
        )
        bucket["calls"] += 1
        if item.get("ok"):
            bucket["ok"] += 1
        try:
            bucket["total_elapsed"] += float(item.get("elapsed") or 0)
            bucket["last_ts"] = max(bucket["last_ts"], float(item.get("ts") or 0))
        except (TypeError, ValueError):
            continue
    result: list[dict[str, Any]] = []
    for bucket in stats.values():
        calls = bucket["calls"]
        result.append(
            {
                "tool": bucket["tool"],
                "calls": calls,
                "ok": bucket["ok"],
                "avg_elapsed": round(bucket["total_elapsed"] / calls, 3) if calls else 0.0,
                "last_ts": bucket["last_ts"],
            }
        )
    result.sort(key=lambda row: row["calls"], reverse=True)
    return result


def cost_by_model(limit: int = _MAX_SCAN) -> dict[str, dict[str, Any]]:
    """按 ``工具/模型`` 聚合**实测**成本，给「价格从哪来」提供证据。

    只统计 ``kind == "delegation"`` 且拿到过自报成本的行；
    返回 ``{"codebuddy/hy4-preview": {calls, costed_calls, total_cost_usd,
    avg_cost_usd, tokens_in, tokens_out, last_ts}}``。

    ``costed_calls < calls`` 说明有些调用没自报成本——这是真实情况，
    要如实透出去，不能把缺数据的调用按 0 元摊平。
    """
    stats: dict[str, dict[str, Any]] = {}
    for item in recent(limit):
        if item.get("kind") != "delegation":
            continue
        tool = str(item.get("tool") or "未知")
        model = str(item.get("model") or "未知")
        key = f"{tool}/{model}"
        bucket = stats.setdefault(
            key,
            {
                "tool": tool,
                "model": model,
                "calls": 0,
                "costed_calls": 0,
                "total_cost_usd": 0.0,
                "tokens_in": 0,
                "tokens_out": 0,
                "last_ts": 0.0,
            },
        )
        bucket["calls"] += 1
        cost = item.get("cost_usd")
        if isinstance(cost, (int, float)):
            bucket["costed_calls"] += 1
            bucket["total_cost_usd"] += float(cost)
        for field in ("tokens_in", "tokens_out"):
            value = item.get(field)
            if isinstance(value, int):
                bucket[field] += value
        try:
            bucket["last_ts"] = max(bucket["last_ts"], float(item.get("ts") or 0))
        except (TypeError, ValueError):
            continue
    for bucket in stats.values():
        costed = bucket["costed_calls"]
        bucket["total_cost_usd"] = round(bucket["total_cost_usd"], 6)
        bucket["avg_cost_usd"] = (
            round(bucket["total_cost_usd"] / costed, 6) if costed else None
        )
    return stats
