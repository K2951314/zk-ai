"""执行体模型清单：**只有实测算数**，任何清单都不可信。

2026-11-11 一天内被运营者纠正两次，两次都是「把某种清单当成了权限」：

* 信 `codebuddy --help` 的 `Currently supported:` —— 静态帮助文字，
  26 个名字里 **17 个不存在**。
* 信桌面缓存 `acc-product-config-v3-*.json` —— 它记的是整条产品线的模型表，
  65 个 id 里**只有 35 个是对话模型**。

现在只有 `probe_models()` 真发请求算数：产出了内容（`content` 或
`reasoning_content`）才算可用；`finish_reason=error` 是硬否决
（`codewise-*` 会返回非空的空代码块，看「有没有字」判不出来）。

往上一层，这和 AGENTS.md 里「供应商的 `/v1/models` 是目录不是你能调的」
是同一条，只是这次连目录、帮助文档、桌面缓存都各栽了一遍。

桌面缓存现在只干一件事：**给实测通过的模型补元数据**
（`credits` 兑换率 / `supportsToolCall` / `maxInputTokens`）。
参考实现 `D:/zhangkun/Downloads/workbuddy-proxy` 的 catalog 合并逻辑。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.services.agent.cli import models as model_discovery


@pytest.fixture(autouse=True)
def _clear() -> None:
    model_discovery.clear_cache()
    yield
    model_discovery.clear_cache()


def _write_cache(tmp_path: Path, payload: dict) -> Path:
    root = tmp_path / ".workbuddy" / "cache"
    root.mkdir(parents=True)
    path = root / "acc-product-config-v3-1-abc.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_desktop_catalog_parses_credits_and_capabilities(tmp_path, monkeypatch) -> None:
    """桌面端缓存的 credits 要解析成数字倍率，工具/图片/上下文一并带出。"""
    _write_cache(tmp_path, {
        "models": [
            {"id": "hy4free", "name": "Hy4 Free", "credits": "x0.00",
             "supportsToolCall": True, "supportsImages": True,
             "maxInputTokens": 960000, "descriptionZh": "免费档"},
            {"id": "glm-5.3-flash", "name": "GLM-5.3-Flash", "credits": "x0.08",
             "supportsToolCall": True, "maxInputTokens": 1000000},
            {"id": "custom-local:my-thing", "credits": "x0.10"},  # 用户自建，不该出现
        ]
    })
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    got = model_discovery.read_desktop_catalog()[0]
    by_id = {m["id"]: m for m in got}
    assert "custom-local:my-thing" not in by_id, "自建本地模型不属于可调清单"
    assert by_id["hy4free"]["rate"] == 0.0
    assert by_id["hy4free"]["max_input_tokens"] == 960000
    assert by_id["glm-5.3-flash"]["rate"] == pytest.approx(0.08)


def test_desktop_catalog_keeps_unknown_rate_as_none(tmp_path, monkeypatch) -> None:
    """没有 credits 的模型 rate=None，**不能**当成免费。"""
    _write_cache(tmp_path, {"models": [{"id": "mystery", "name": "Mystery"}]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    got = model_discovery.read_desktop_catalog()[0]
    assert got[0]["rate"] is None


def test_night_discount_only_applies_in_the_window(tmp_path, monkeypatch) -> None:
    """夜间折扣只在 23:00–08:00 生效，其它时段返回 None。

    这是桌面端 ``modelPromotions`` 的语义。算错的后果是面板把白天的价格
    显示成半价，运营者据此派单会误判成本。
    """
    _write_cache(tmp_path, {
        "models": [{"id": "x", "credits": "x0.20"}],
        "modelPromotions": [
            {"kind": "discount", "enabled": True, "factor": 1,
             "modelIds": ["x"], "discount": {"factor": 0.5}},
        ],
    })
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    import time as _time

    # 2026-01-01 12:00 本地时间（白天）
    noon = _time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))
    assert model_discovery.desktop_discount_factor("x", now=noon) is None
    # 2026-01-01 23:30（夜间窗口）
    night = _time.mktime((2026, 1, 1, 23, 30, 0, 0, 0, -1))
    assert model_discovery.desktop_discount_factor("x", now=night) == pytest.approx(0.5)


def test_help_text_list_is_parsed_but_only_as_fallback() -> None:
    """``--help`` 的清单仍然能解析（保留兜底路径），但它不是权威来源。"""
    text = "  --model <model>  Currently supported: (hy3, glm-5.3-flash)"
    assert model_discovery.parse_codebuddy_models(text) == ["hy3", "glm-5.3-flash"]
    # 解析得出 ≠ 可信：docstring 里说明了实测 26 个名字有 17 个不存在
    assert "静态帮助文字" in (model_discovery.parse_codebuddy_models.__doc__ or "")


def test_desktop_catalog_is_metadata_not_a_permission_list(tmp_path, monkeypatch) -> None:
    """桌面缓存**只提供元数据，不决定清单**——实测只有它能算数。

    2026-11-11 被运营者当面纠正两次后定的规矩：
      · 第一版信 `codebuddy --help` → 26 个名字 17 个不存在
      · 第二版信桌面缓存 → 65 个 id 里只有 35 个是对话模型，26 个 400
      · 现行：`probe_models()` 真发请求，只有产出内容的才算可用

    这条钉住「缓存不能自己变成清单」：即使缓存里有一堆 id，
    没实测通过的就不该出现在 `discover_model_details` 的可用集里。
    """
    _write_cache(tmp_path, {"models": [
        {"id": "good-model", "credits": "x0.10", "supportsToolCall": True},
        {"id": "kling-video-v3", "credits": "x0.30"},   # 视频模型，chat 必 400
        {"id": "codewise-rewrite", "credits": "x0.08"},  # 补全模型，finish=error
    ]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(model_discovery, "_wb_proxy_key", lambda: "test-key")

    async def fake_probe(candidates, *, force=False):
        return {
            "good-model": (True, ""),
            "kling-video-v3": (False, "HTTP 400 Backend [maas] is not supported"),
            "codewise-rewrite": (False, "finish_reason=error（不是对话模型）"),
        }

    monkeypatch.setattr(model_discovery, "probe_models", fake_probe)
    got = asyncio.run(model_discovery.discover_model_details("codebuddy", "codebuddy"))
    usable = {m["id"] for m in got if m.get("probed")}
    assert usable == {"good-model"}, "缓存里有 ≠ 可用；只有实测 200+有产出的才算"
    # 不可用的要带原因，运营者要能回答「它为什么不在列表里」
    errors = {m["id"]: m["probe_error"] for m in got if not m.get("probed")}
    assert "Backend" in errors["kling-video-v3"]
    assert "finish_reason=error" in errors["codewise-rewrite"]


def test_probe_returns_empty_when_proxy_is_down(tmp_path, monkeypatch) -> None:
    """wb-proxy 没开时返回空，**不能**把未验证的缓存当成可用清单。

    这是上一版的错：代理没启动，面板照样显示 65 个模型，运营者点了才发现全 400。
    宁缺勿错——实测拿不到就如实为空，让调用方回落到配置快照。
    """
    _write_cache(tmp_path, {"models": [{"id": "whatever", "credits": "x0.10"}]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(model_discovery, "_wb_proxy_key", lambda: "")
    model_discovery.clear_cache()
    got = asyncio.run(model_discovery.discover_model_details("codebuddy", "codebuddy"))
    assert got == [], "没配 Key / 代理不可达时必须返回空，不许冒充清单"


def test_reasoning_only_models_count_as_working(tmp_path, monkeypatch) -> None:
    """只产出 `reasoning_content` 的推理模型也是可用的。

    踩过的坑：用 `max_tokens=1` 探测时，推理模型把 token 全花在思考上、
    `content` 为空、`finish_reason=length`，于是被误判成不可用——
    实测误杀 16/39（hy3 / hy4free / step-5-preview 等）。现在探测给 200 token，
    并且「有 content **或** 有 reasoning_content」都算工作。
    """
    _write_cache(tmp_path, {"models": [
        {"id": "hy4free", "credits": "x0.00", "maxInputTokens": 960000},
    ]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(model_discovery, "_wb_proxy_key", lambda: "k")
    monkeypatch.setattr(
        model_discovery, "probe_models",
        lambda c, force=False: _ready({"hy4free": (True, "")}),
    )
    got = asyncio.run(model_discovery.discover_model_details("codebuddy", "codebuddy"))
    usable = [m for m in got if m.get("probed")]
    assert [m["id"] for m in usable] == ["hy4free"]
    assert usable[0]["rate"] == 0.0
    assert usable[0]["max_input_tokens"] == 960000


def _ready(snapshot):
    import asyncio as _a
    fut = _a.Future()
    fut.set_result(snapshot)
    return fut


def test_probe_budget_must_not_be_one() -> None:
    """探测的输出预算必须够推理模型先思考——`max_tokens=1` 会误杀 16/39。

    这条钉住常量本身：不是「代码能跑」，是「别再改回 1」。
    """
    assert model_discovery._PROBE_MAX_TOKENS >= 100, (
        "max_tokens=1 会让推理模型的 token 全花在 reasoning_content 上、"
        "content 为空而被误判不可用（2026-11-11 实测误杀 16/39）"
    )


def test_config_snapshot_is_not_a_catalog_claim() -> None:
    """``cli_tools.yaml`` 的 models 只是离线兜底，不能当"账号真实清单"。

    这条守的是**注释与语义一致**：配置里必须写明它只是兜底，
    否则下一个人又会以为改 yaml 就能改真实清单（事实是要改桌面端缓存）。
    """
    root = Path(__file__).resolve().parent.parent
    template = root / "config" / "cli_tools.example.yaml"
    if not template.is_file():  # pragma: no cover - 模板是仓库自带的
        pytest.skip("没有 cli_tools.example.yaml")
    text = template.read_text(encoding="utf-8")
    # 找到 codebuddy 段
    start = text.index("codebuddy:")
    block = text[start:start + 4000]
    assert "兜底" in block or "fallback" in block, (
        "codebuddy 段的 models 必须注明只是离线兜底，真实清单来自桌面端缓存"
    )
