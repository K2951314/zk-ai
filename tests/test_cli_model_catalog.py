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
def _clear(monkeypatch, tmp_path) -> None:
    model_discovery.clear_cache()
    # 落盘快照一律指到临时目录：测试绝不读写真实的 data/model-probe.json，
    # 也不会因为本机跑过一次实测而行为漂移。
    monkeypatch.setenv("ZKAI_MODEL_PROBE_SNAPSHOT", str(tmp_path / "probe.json"))
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


# --------------------------------------------------------------------------- #
# 落盘实测快照：刷新一次之后，网关重启也不该退回配置快照里的 9 个模型
# --------------------------------------------------------------------------- #
def test_probe_snapshot_survives_process_cache_clear(tmp_path, monkeypatch) -> None:
    """清掉进程缓存（≈ 网关重启）后，仍要给出上次真测过的清单。

    这是「执行体里的模型不对」的直接修复：以前刷新实测只活 10 分钟内存，
    之后 /executors 就回落到 cli_tools.yaml 里那份手工快照。
    """
    _write_cache(tmp_path, {"models": [
        {"id": "hy4free", "credits": "x0.00", "supportsToolCall": True},
        {"id": "kling-video-v3", "credits": "x0.30"},
    ]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    model_discovery._write_snapshot({
        "hy4free": (True, ""),
        "kling-video-v3": (False, "HTTP 400 Backend [maas] is not supported"),
    })
    model_discovery.clear_cache()
    assert model_discovery.verified_snapshot()["ids"] == ["hy4free"]
    assert model_discovery.cached_models("codebuddy", "codebuddy") == ["hy4free"]
    details = model_discovery.cached_model_details("codebuddy", "codebuddy")
    assert [d["id"] for d in details] == ["hy4free"]
    assert details[0]["probed"] is True
    assert details[0]["rate"] == 0.0


def test_expired_or_corrupt_snapshot_is_not_trusted(monkeypatch, tmp_path) -> None:
    """坏文件 / 过期快照不能冒充「当前可用」，宁可回落到未实测标注。"""
    path = tmp_path / "probe.json"
    monkeypatch.setenv("ZKAI_MODEL_PROBE_SNAPSHOT", str(path))
    path.write_text("{not json", encoding="utf-8")
    assert model_discovery.verified_snapshot()["ids"] == []
    path.write_text(json.dumps({
        "version": 1, "captured_at": 1.0, "candidate_fingerprint": "old",
        "results": {"old-model": {"ok": True, "error": ""}},
    }), encoding="utf-8")
    assert model_discovery.verified_snapshot()["ids"] == [], "过期快照不能冒充当前可用"


def test_write_snapshot_merges_without_losing_history(tmp_path, monkeypatch) -> None:
    """增量刷新只覆盖本次真测过的 id，别把其它历史结论冲掉。"""
    _write_cache(tmp_path, {"models": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    model_discovery._write_snapshot({"a": (True, ""), "b": (False, "HTTP 400")})
    model_discovery._write_snapshot({"c": (True, "")})
    snap = model_discovery.verified_snapshot()
    assert set(snap["ids"]) == {"a", "c"}
    assert snap["results"]["b"] == (False, "HTTP 400")


def test_candidate_fingerprint_changes_when_catalog_changes(tmp_path, monkeypatch) -> None:
    """有新模型上线时指纹要变（界面据此提示刷新），但指纹本身不是权限。"""
    _write_cache(tmp_path, {"models": [{"id": "a", "credits": "x0.1"}]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    ids = model_discovery.candidate_ids()
    assert ids == ["a"]
    assert model_discovery.candidate_fingerprint(ids) == model_discovery.candidate_fingerprint()
    assert model_discovery.candidate_fingerprint(ids) != model_discovery.candidate_fingerprint(
        [*ids, "hy4free"]
    )


def test_probed_models_are_merged_into_stale_catalogue() -> None:
    """目录 ≠ 权限：目录没列但实测能调的模型要补进模型市场。

    2026-11-11 实测：wb-proxy /v1/models 只列 14 个旧 id，hy4free /
    glm-5.3-flash 真能调却一个都没列。补进来的行不能覆盖目录原有元数据，
    也不能就地改调用方传进来的对象（后面还要算 manual_set）。
    """
    from app.api.admin.providers import merge_probed_models

    catalogue = {"hy3": {"id": "hy3", "owned_by": "workbuddy"}}
    merged = merge_probed_models(catalogue, {"hy3", "hy4free", "glm-5.3-flash"})
    assert set(merged) == {"hy3", "hy4free", "glm-5.3-flash"}
    assert merged["hy3"]["owned_by"] == "workbuddy"
    assert catalogue == {"hy3": {"id": "hy3", "owned_by": "workbuddy"}}


# --------------------------------------------------------------------------- #
# 上下文窗口：别再拿预设表的 256000 冒充真实值
# --------------------------------------------------------------------------- #
def test_discovery_recognises_workbuddy_context_aliases() -> None:
    """wb-proxy 的 x_ 扩展字段与桌面缓存的驼峰字段都要认得。

    运营者原话：「hy4 明明就是 1M 左右的上下文，而模型市场里显示才 200k」。
    200k 来自预设表，真实值在桌面缓存的 ``maxInputTokens``（hy4free=960000）。
    """
    from app.models.discovery import model_facts

    assert model_facts({"x_max_input_tokens": 192000}).context_window == 192000
    assert model_facts({"maxInputTokens": 960000}).context_window == 960000
    assert model_facts({}).context_window is None


def test_probed_rows_carry_real_context_window() -> None:
    """目录外补进来的模型必须带桌面缓存元数据，不能只补一个裸 id。"""
    from app.api.admin.providers import merge_probed_models
    from app.models.discovery import model_facts

    meta = {"hy4free": {"id": "hy4free", "max_input_tokens": 960000,
                        "images": True, "reasoning": True}}
    merged = merge_probed_models({"hy3": {"id": "hy3"}}, {"hy4free"}, meta)
    facts = model_facts(merged["hy4free"])
    assert facts.context_window == 960000
    assert facts.vision_input is True
    assert facts.reasoning is True


def test_transient_probe_failure_keeps_verified_conclusion(tmp_path, monkeypatch) -> None:
    """一次 ReadTimeout 不能把已验证可用的模型踢出清单。

    2026-11-11 实测 ``kimi-k2.8-preview`` 超时一次，清单就从 36 掉到 35，
    运营者看到的「模型又有差异」有一半是这个抖动。
    """
    _write_cache(tmp_path, {"models": [{"id": "flaky", "credits": "x0.10"}]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    model_discovery._write_snapshot({"flaky": (True, "")})
    model_discovery._write_snapshot({"flaky": (False, "ReadTimeout")})
    assert model_discovery.verified_snapshot()["ids"] == ["flaky"]


def test_hard_probe_failure_still_overrides(tmp_path, monkeypatch) -> None:
    """明确否决（4xx / 11102）必须覆盖旧结论——别把宽容做成「永不更新」。"""
    _write_cache(tmp_path, {"models": [{"id": "gone", "credits": "x0.10"}]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    model_discovery._write_snapshot({"gone": (True, "")})
    model_discovery._write_snapshot({"gone": (False, 'HTTP 400 {"code":11102}')})
    assert model_discovery.verified_snapshot()["ids"] == []


def test_rate_limit_keeps_verified_conclusion_but_is_surfaced(tmp_path, monkeypatch) -> None:
    """429 免费额度限流不推翻已验证结论，但要留下「当前限流」状态。

    2026-11-11 实测：探测 65 个模型自己触发了 hy4free 的每日免费额度上限
    （错误写明「明日 10:20 重置」），旧逻辑把它当否决，模型直接从市场消失。
    限流是配额状态，不是「这个模型没权限」。
    """
    _write_cache(tmp_path, {"models": [{"id": "free-tier", "credits": "x0.00"}]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    model_discovery._write_snapshot({"free-tier": (True, "")})
    model_discovery._write_snapshot({
        "free-tier": (False, 'HTTP 429 {"code":6004,"msg":"使用量已超出频率限制"}'),
    })
    snap = model_discovery.verified_snapshot()
    assert snap["ids"] == ["free-tier"], "限流不应把已验证模型踢出清单"
    assert "429" in snap["transient"]["free-tier"], "要如实透出当前限流状态"


# --------------------------------------------------------------------------- #
# 「刷新实测」之后：桌面目录 / 一次瞬时失败都不能覆盖已验证结论
# --------------------------------------------------------------------------- #
def test_refresh_discovery_cannot_hide_verified_models(tmp_path, monkeypatch) -> None:
    """`/model-inventory?refresh=true` 先 `discover_models()` 再探测。

    旧代码里 `discover_models()` 会把桌面缓存**整份**塞进 `_CACHE`，随后
    `cached_models()` 直接读它——于是刷新一次，运营者看到的就从 36 个
    实测可用变回 65 个候选（含视频/补全模型），「越刷新越不对」。
    `discover_models()` 可以给全量候选，但 `cached_models()` 只认已验证清单。
    """
    _write_cache(tmp_path, {"models": [
        {"id": "hy4free", "credits": "x0.00", "maxInputTokens": 960000},
        {"id": "kling-video-v3", "credits": "x0.30"},
    ]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    model_discovery._write_snapshot({
        "hy4free": (True, ""),
        "kling-video-v3": (False, "HTTP 400 Backend [maas] is not supported"),
    })
    live = model_discovery.discover_models("codebuddy", "codebuddy")
    assert "hy4free" in live and "kling-video-v3" in live, "候选集可以全量给"
    assert model_discovery.cached_models("codebuddy", "codebuddy") == ["hy4free"], (
        "可用清单不能退回桌面目录，否则刷新后候选模型全回来了"
    )


def test_refresh_probe_uses_merged_snapshot_for_transient_failure(tmp_path, monkeypatch) -> None:
    """刷新时一次限流/超时，不能在同一响应里把模型标成不可用。

    2026-11-11 实测 hy4free 每日额度被打满回 429、kimi-k2.8-preview
    回 ReadTimeout。`_write_snapshot()` 会保留历史结论，但 `probe_models()`
    原来仍返回本次的失败结果，`discover_model_details()` 据此把模型标
    ``probed=False``——磁盘结论对了，接口第一次返回仍是错的。
    """
    _write_cache(tmp_path, {"models": [{"id": "flaky", "credits": "x0.10"}]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    model_discovery._write_snapshot({"flaky": (True, "")})
    monkeypatch.setattr(model_discovery, "_wb_proxy_key", lambda: "k")

    async def timeout_probe(client: object, model_id: str, key: str) -> tuple[str, bool, str]:
        del client, key
        return model_id, False, "ReadTimeout"

    monkeypatch.setattr(model_discovery, "_probe_one", timeout_probe)
    got = asyncio.run(model_discovery.discover_model_details("codebuddy", "codebuddy"))
    usable = [d for d in got if d.get("probed")]
    assert [d["id"] for d in usable] == ["flaky"], "本次没测成不能覆盖已验证结论"
    assert usable[0]["probe_error"] == "暂态：ReadTimeout", "当前没测成的状态要如实保留"


def test_unverified_desktop_catalog_is_not_an_available_list(tmp_path, monkeypatch) -> None:
    """探测拿不到、也没有落盘结论时，桌面目录不能冒充可用清单。"""
    _write_cache(tmp_path, {"models": [{"id": "kling-video-v3", "credits": "x0.30"}]})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    model_discovery.discover_models("codebuddy", "codebuddy")
    assert model_discovery.cached_models("codebuddy", "codebuddy") == []
