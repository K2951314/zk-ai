"""统一模型清单：把「每个工具能调哪些模型、分别什么价」汇到一处。

价格这件事必须分层，否则一定会骗人：

- ``tariff``   网关模型的**真实牌价**（``input_cost_per_mtok`` /
  ``output_cost_per_mtok``，USD/百万 token），来自 models.yaml 的部署配置。
- ``observed`` 外部 CLI 的**实测成本**：它们没有价目表，只有调用后自报的
  ``total_cost_usd``。所以只能报「过去 N 次平均每次多少钱」，不能换算成单价。
- ``subscription`` 订阅/免费额度内的调用：单价未知且不该按 0 元展示，
  只能说明「走订阅额度，超出后可能被限流」。
- ``unknown``  什么都没拿到。**如实说不知道**，不要用 0 或估算值填充。

``costed_calls < calls`` 时也要如实透出：说明有些调用没自报成本，
不能拿有数据的那几次去代表全部。
"""
from __future__ import annotations

from typing import Any

#: 价格来源标签（前端按它显示中文徽标）
PRICE_SOURCES = ("tariff", "observed", "subscription", "unknown")


def _gateway_row(model: dict[str, Any]) -> dict[str, Any]:
    """网关模型：价格取各部署的牌价，多个部署时逐条列出。"""
    prices: list[dict[str, Any]] = []
    for dep in model.get("deployments") or []:
        if not isinstance(dep, dict):
            continue
        prices.append(
            {
                "provider": dep.get("provider_id") or "",
                "deployment": dep.get("id") or "",
                "input_per_mtok": dep.get("input_cost_per_mtok"),
                "output_per_mtok": dep.get("output_cost_per_mtok"),
                "currency": "USD",
                "enabled": bool(dep.get("enabled", True)),
            }
        )
    # 只有真的带了数字才算有牌价；全空就是「没配价格」。
    has_tariff = any(
        isinstance(p["input_per_mtok"], (int, float))
        or isinstance(p["output_per_mtok"], (int, float))
        for p in prices
    )
    return {
        "id": model.get("id") or "",
        "display_name": model.get("display_name") or model.get("id") or "",
        "enabled": bool(model.get("enabled", True)),
        "context_window": model.get("context_window") or 0,
        "prices": prices,
        "price_source": "tariff" if has_tariff else "unknown",
        "price_note": "" if has_tariff else "配置里没有单价（未填或供应商未报价）",
    }


def _observed_for(
    observed: dict[str, dict[str, Any]], tool: str, model_id: str
) -> dict[str, Any] | None:
    """取某工具某模型的实测成本；没有记录就返回 None（不要编 0）。"""
    bucket = observed.get(f"{tool}/{model_id}")
    if not bucket:
        return None
    return {
        "calls": bucket.get("calls", 0),
        "costed_calls": bucket.get("costed_calls", 0),
        "avg_cost_usd": bucket.get("avg_cost_usd"),
        "total_cost_usd": bucket.get("total_cost_usd"),
        "tokens_in": bucket.get("tokens_in", 0),
        "tokens_out": bucket.get("tokens_out", 0),
        "last_ts": bucket.get("last_ts", 0.0),
    }


def _executor_row(
    item: dict[str, Any], observed: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """执行体：模型清单来自 live/配置，价格优先用桌面端兑换率。

    ``rate`` 是 WorkBuddy/CodeBuddy 桌面端缓存里的 ``credits`` 倍率
    （``x0.29`` = 按 0.29 倍计费），这是外部 CLI 唯一的**真实价格口径**。
    有它就不要再标「订阅内 / 未知」——那是在有价目表的情况下说瞎话。
    """
    tool = str(item.get("tool") or "")
    tier = str(item.get("cost_tier") or "")
    models: list[dict[str, Any]] = []
    for model in item.get("models") or []:
        model_id = str(model.get("id") or "")
        if not model_id:
            continue
        rate = model.get("rate")
        seen = _observed_for(observed, tool, model_id)
        if rate is not None:
            source = "tariff"
            note = (
                "不计费（兑换率 0）" if rate == 0
                else f"兑换率 ×{float(rate):.2f}（桌面端 credits，一次对话的积分倍率）"
            )
        elif seen is not None:
            source = "observed"
            note = (
                f"实测 {seen['costed_calls']}/{seen['calls']} 次拿到自报成本"
                if seen["costed_calls"] < seen["calls"]
                else f"实测 {seen['calls']} 次"
            )
        elif tier == "free":
            source = "subscription"
            note = "配置标为免费档，但拿不到兑换率；额度耗尽会被限流"
        else:
            source = "unknown"
            note = "没有价目表，也还没有实测记录"
        models.append(
            {
                "id": model_id,
                "free": bool(model.get("free")),
                # probed=True = 真发过请求验证可用；False = 只从清单里看到的
                "probed": bool(model.get("probed")),
                "price_source": source,
                "rate": rate,
                "tools": model.get("tools"),
                "images": model.get("images"),
                "max_input_tokens": model.get("max_input_tokens"),
                "input_per_mtok": None,
                "output_per_mtok": None,
                "observed": seen,
                "price_note": note,
            }
        )
    return {
        "tool": tool,
        "role": item.get("role") or "",
        "hint": item.get("hint") or "",
        "cost_tier": tier,
        "models_source": item.get("models_source") or "config",
        "models": models,
    }


def build(
    *,
    gateway_models: list[dict[str, Any]],
    executors: list[dict[str, Any]],
    observed: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """拼出统一清单。调用方负责把三份数据取好（便于离线测试）。"""
    gateway = [_gateway_row(m) for m in gateway_models if isinstance(m, dict)]
    rows = [_executor_row(e, observed) for e in executors if isinstance(e, dict)]
    # 汇总计数，给页面做标题用
    total = len(gateway) + sum(len(r["models"]) for r in rows)
    priced = sum(1 for g in gateway if g["price_source"] == "tariff") + sum(
        1 for r in rows for m in r["models"] if m["price_source"] == "observed"
    )
    return {
        "object": "model_inventory",
        "gateway": gateway,
        "executors": rows,
        "summary": {
            "total_models": total,
            "priced_models": priced,
            "gateway_models": len(gateway),
            "executor_models": total - len(gateway),
            "price_sources": list(PRICE_SOURCES),
        },
        "notes": [
            "网关模型的价格是配置里的真实牌价（USD/百万 token）。",
            "外部 CLI 的兑换率来自 WorkBuddy/CodeBuddy 桌面端缓存（credits 倍率），"
            "这是它们唯一的价格口径；拿不到就如实标「未知」，已不用的配置标签不再冒充。",
        ],
    }
