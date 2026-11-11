"""统一模型清单（每个工具的模型 + 价格）的守门测试。

价格最容易骗人的三种写法，这里逐条钉住：
1. 没牌价却显示 0（等于宣称免费）；
2. CLI 没价目表却硬换算单价；
3. 只报有数据的几次，掩盖「有些调用没自报成本」。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.agent import model_inventory as mi
from app.services.agent import usage_ledger


def _gateway(**over: Any) -> dict[str, Any]:
    base = {
        "id": "sensenova-6.8-flash",
        "display_name": "商汤 6.8 Flash",
        "enabled": True,
        "context_window": 1048576,
        "deployments": [
            {
                "id": "sensenova-6.8-flash-sensenova",
                "provider_id": "sensenova",
                "enabled": True,
                "input_cost_per_mtok": 0.0,
                "output_cost_per_mtok": 0.0,
            }
        ],
    }
    base.update(over)
    return base


def _executor(tier: str = "free", models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "tool": "codebuddy",
        "role": "cli-grinder",
        "hint": "通用改写",
        "cost_tier": tier,
        "models_source": "live",
        "models": models if models is not None else [{"id": "hy4-preview", "free": True}],
    }


# --------------------------------------------------------------------------- #
# 网关侧：有牌价才是 tariff，没填就说不知道
# --------------------------------------------------------------------------- #


def test_gateway_with_tariff_marks_price_source() -> None:
    data = mi.build(gateway_models=[_gateway()], executors=[], observed={})
    row = data["gateway"][0]
    assert row["price_source"] == "tariff"
    assert row["prices"][0]["input_per_mtok"] == 0.0
    assert row["prices"][0]["currency"] == "USD"


def test_gateway_without_numbers_is_unknown_not_zero() -> None:
    """缺单价时必须是 unknown——写 0 会被读成「免费」。"""
    model = _gateway(deployments=[{"id": "d", "provider_id": "p", "enabled": True}])
    data = mi.build(gateway_models=[model], executors=[], observed={})
    row = data["gateway"][0]
    assert row["price_source"] == "unknown"
    assert row["price_note"]
    assert row["prices"][0]["input_per_mtok"] is None


# --------------------------------------------------------------------------- #
# 执行体侧：订阅内 / 实测 / 未知 三态
# --------------------------------------------------------------------------- #


def test_executor_free_tier_without_history_is_subscription() -> None:
    """拿不到兑换率的免费档：标「订阅内」，不编单价，也不冒充「确定免费」。

    2026-11-11 改了措辞——原文「免费额度/订阅内，单价未知」容易让人以为
    「额度明确是免费的」；实际只是配置标了 ``cost_tier: free``。
    现在有兑换率就优先用它（见下一条），没有才退到这个口径。
    """
    data = mi.build(gateway_models=[], executors=[_executor("free")], observed={})
    model = data["executors"][0]["models"][0]
    assert model["price_source"] == "subscription"
    assert model["input_per_mtok"] is None      # 不换算单价
    assert "免费档" in model["price_note"]
    assert "拿不到兑换率" in model["price_note"]


def test_executor_with_desktop_credits_rate_is_tariff() -> None:
    """桌面端兑换率（credits）存在时，price_source 是 tariff 而不是订阅内。

    这是外部 CLI **唯一的真实价格口径**——有价目表还说「价格未知」就是撒谎。
    """
    executor = _executor("free", [
        {"id": "glm-5.3-flash", "free": False, "rate": 0.08,
         "tools": True, "images": True, "max_input_tokens": 1000000},
    ])
    data = mi.build(gateway_models=[], executors=[executor], observed={})
    model = data["executors"][0]["models"][0]
    assert model["price_source"] == "tariff"
    assert model["rate"] == pytest.approx(0.08)
    assert "0.08" in model["price_note"]
    # 能力字段要透传，前端靠它显示「工具 / 图片 / 上下文」
    assert model["tools"] is True
    assert model["max_input_tokens"] == 1000000


def test_executor_with_zero_rate_says_free_explicitly() -> None:
    """兑换率 0 是**真的不收费**，要说清楚，别落到「未知」。"""
    executor = _executor("free", [{"id": "hy4free", "free": True, "rate": 0.0}])
    data = mi.build(gateway_models=[], executors=[executor], observed={})
    model = data["executors"][0]["models"][0]
    assert model["price_source"] == "tariff"
    assert "不计费" in model["price_note"]


def test_executor_paid_without_history_is_unknown() -> None:
    data = mi.build(gateway_models=[], executors=[_executor("paid")], observed={})
    model = data["executors"][0]["models"][0]
    assert model["price_source"] == "unknown"


def test_executor_with_observed_cost_uses_measured_average() -> None:
    observed = {
        "codebuddy/hy4-preview": {
            "calls": 4,
            "costed_calls": 4,
            "avg_cost_usd": 0.0125,
            "total_cost_usd": 0.05,
            "tokens_in": 1200,
            "tokens_out": 300,
            "last_ts": 1.0,
        }
    }
    data = mi.build(gateway_models=[], executors=[_executor()], observed=observed)
    model = data["executors"][0]["models"][0]
    assert model["price_source"] == "observed"
    assert model["observed"]["avg_cost_usd"] == 0.0125


def test_partial_cost_coverage_is_disclosed() -> None:
    """4 次里只有 2 次自报成本，必须如实说明，不能拿 2 次代表全部。"""
    observed = {
        "codebuddy/hy4-preview": {
            "calls": 4,
            "costed_calls": 2,
            "avg_cost_usd": 0.01,
            "total_cost_usd": 0.02,
            "tokens_in": 0,
            "tokens_out": 0,
            "last_ts": 1.0,
        }
    }
    data = mi.build(gateway_models=[], executors=[_executor()], observed=observed)
    note = data["executors"][0]["models"][0]["price_note"]
    assert "2/4" in note


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


def test_summary_counts_only_models_with_real_prices() -> None:
    data = mi.build(
        gateway_models=[_gateway()],
        executors=[_executor(models=[{"id": "a", "free": True}, {"id": "b", "free": False}])],
        observed={},
    )
    # 网关 1 个有牌价；执行体 2 个都没有实测 -> priced 只有 1
    assert data["summary"]["total_models"] == 3
    assert data["summary"]["priced_models"] == 1
    assert data["summary"]["gateway_models"] == 1
    assert data["summary"]["executor_models"] == 2


def test_empty_inputs_do_not_crash() -> None:
    data = mi.build(gateway_models=[], executors=[], observed={})
    assert data["gateway"] == []
    assert data["executors"] == []
    assert data["summary"]["total_models"] == 0


# --------------------------------------------------------------------------- #
# 账本：缺成本不能被算成 0 元
# --------------------------------------------------------------------------- #


def test_cost_by_model_does_not_treat_missing_cost_as_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ZKAI_DATA_DIR", str(tmp_path))
    usage_ledger.record_delegation(
        tool="codebuddy", model="hy4-preview", role="cli-grinder",
        session_id="s1", task_space=str(tmp_path), ok=True, elapsed=1.0,
        tokens_in=10, tokens_out=5, cost_usd=None,
    )
    usage_ledger.record_delegation(
        tool="codebuddy", model="hy4-preview", role="cli-grinder",
        session_id="s1", task_space=str(tmp_path), ok=True, elapsed=1.0,
        tokens_in=10, tokens_out=5, cost_usd=0.02,
    )
    bucket = usage_ledger.cost_by_model()["codebuddy/hy4-preview"]
    assert bucket["calls"] == 2
    assert bucket["costed_calls"] == 1          # 只有 1 次真给了成本
    assert bucket["total_cost_usd"] == 0.02     # 不是 0.01（不能把 None 当 0 摊）
    assert bucket["avg_cost_usd"] == 0.02


def test_cost_by_model_ignores_mcp_rows(tmp_path, monkeypatch) -> None:
    """MCP 工具调用没有模型概念，不该混进模型价格统计。"""
    monkeypatch.setenv("ZKAI_DATA_DIR", str(tmp_path))
    usage_ledger.record(
        tool="create_sheet", server="editor-sdk", session_id="s1",
        task_space=str(tmp_path), ok=True, elapsed=0.2,
    )
    assert usage_ledger.cost_by_model() == {}
