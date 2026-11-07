"""控制台「看得见新内容 / 模型显示正确」的守门测试。

背景（2026-11-07 实测）：
1. ``/ui/*`` 原本不带任何缓存头，浏览器启发式缓存旧 index.html / console.js，
   运营者重启网关、重新登录后仍看不到刚加的入口与引导。
2. ``/zkadmin/agent/executors`` 只读 cli_tools.yaml 的手抄模型快照，而 qoder
   的清单会随账号漂移（Qwen3.8-Flash -> Sonus），面板显示的名字因此是错的。

这里守三条：页面一律 no-store、模型缓存只读不启子进程、列表接口用实时清单。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api import agent as agent_api
from app.api.ui import router as ui_router
from app.services.agent.cli import models as model_discovery


def _client() -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(ui_router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# --------------------------------------------------------------------------- #
# 1. 页面禁用缓存
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/ui", "/ui/agent", "/ui/report", "/ui/events", "/ui/taskspaces", "/ui/models",
     "/ui/console.css", "/ui/console.js"],
)
async def test_console_pages_disable_caching(path: str) -> None:
    """每个页面/脚本都带 no-store，刷新就一定拿到磁盘上的最新版本。"""
    async with _client() as client:
        resp = await client.get(path)
    assert resp.status_code == 200
    assert "no-store" in resp.headers.get("cache-control", ""), path


@pytest.mark.asyncio
async def test_console_unknown_asset_still_404() -> None:
    """加缓存头不能顺手把不存在的资源也放出来。"""
    async with _client() as client:
        resp = await client.get("/ui/not-a-real-asset")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 2. 模型缓存：只读、会过期
# --------------------------------------------------------------------------- #


def test_cached_models_cold_returns_empty(monkeypatch: Any) -> None:
    """缓存冷时返回空，且绝不调用会起子进程的 discover_models。"""
    model_discovery.clear_cache()
    called: list[str] = []

    def _forbid(*args: Any, **kwargs: Any) -> list[str]:
        called.append("discover")
        return []

    monkeypatch.setattr(model_discovery, "discover_models", _forbid)
    assert model_discovery.cached_models("qoder", "qodercli") == []
    assert called == []


def test_cached_models_returns_live_list_without_subprocess() -> None:
    """预热后命中，返回副本（调用方改它不会污染缓存）。"""
    model_discovery.clear_cache()
    model_discovery._CACHE["qoder:qodercli"] = (time.time(), ["Sonus", "ds"])
    got = model_discovery.cached_models("qoder", "qodercli")
    assert got == ["Sonus", "ds"]
    got.append("mutated")
    assert model_discovery.cached_models("qoder", "qodercli") == ["Sonus", "ds"]
    model_discovery.clear_cache()


def test_cached_models_expired_returns_empty() -> None:
    """过了 TTL 就当冷缓存，不能拿旧清单冒充实时。"""
    model_discovery.clear_cache()
    model_discovery._CACHE["qoder:qodercli"] = (
        time.time() - model_discovery._TTL - 1,
        ["old-model"],
    )
    assert model_discovery.cached_models("qoder", "qodercli") == []
    model_discovery.clear_cache()


# --------------------------------------------------------------------------- #
# 3. /executors 用实时清单
# --------------------------------------------------------------------------- #


def _registry() -> SimpleNamespace:
    tool = SimpleNamespace(
        name="qoder",
        command="qodercli",
        model="Qwen3.8-Flash",       # 配置快照：账号里其实已经漂移成 Sonus
        cost_tier="free",
        effort="max",
        context_window=1000000,
        models=["Qwen3.8-Flash", "DeepSeek-Flash"],
        free_models=[],
    )
    rc = SimpleNamespace(tool="qoder", hint="机械改写")
    return SimpleNamespace(
        known_roles=lambda: ["cli-grinder-qoder"],
        roles={"cli-grinder-qoder": rc},
        tools={"qoder": tool},
    )


def _container() -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace())


@pytest.mark.asyncio
async def test_executors_uses_live_models_when_cache_warm(monkeypatch: Any) -> None:
    """缓存热时面板显示实时清单，而不是过期的手抄快照。"""
    monkeypatch.setattr(agent_api, "load_cli_registry", lambda _s: _registry())
    monkeypatch.setattr(agent_api, "cached_models", lambda _t, _c: ["Sonus", "ds"])
    data = await agent_api.list_executors(_container())  # type: ignore[arg-type]
    item = data["executors"][0]
    assert item["models_source"] == "live"
    assert [m["id"] for m in item["models"]] == ["Sonus", "ds"]


@pytest.mark.asyncio
async def test_executors_falls_back_to_config_when_cache_cold(monkeypatch: Any) -> None:
    """缓存冷时用配置快照，但明确标注来源，且不会因此报错。"""
    monkeypatch.setattr(agent_api, "load_cli_registry", lambda _s: _registry())
    monkeypatch.setattr(agent_api, "cached_models", lambda _t, _c: [])
    data = await agent_api.list_executors(_container())  # type: ignore[arg-type]
    item = data["executors"][0]
    assert item["models_source"] == "config"
    assert [m["id"] for m in item["models"]] == ["Qwen3.8-Flash", "DeepSeek-Flash"]
    assert all(m["free"] for m in item["models"])  # 免费档执行体整条都免费
