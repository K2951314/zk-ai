"""运营可观测性：客户端差异、可选子系统使用量与运行时参数。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx
import pytest

from app.main import create_app
from app.models.response import AttemptOutcome
from tests.conftest import Harness, build_harness, make_config, make_model, make_provider


@pytest.fixture
async def observability_api(fake_adapter) -> AsyncIterator[tuple[httpx.AsyncClient, Harness]]:
    config = make_config(
        providers=[make_provider("fake", key_ids=("key-1",))],
        models=[make_model("fake-model", provider_id="fake")],
        admin_token="test-admin-token",
    )
    config.settings.stream_idle_timeout = 12.0
    config.settings.stream_prefetch_timeout = 3.0
    config.settings.max_input_tokens = 1234
    config.settings.max_request_seconds = 456.0
    config.settings.agent_enabled = False
    config.settings.events_enabled = True
    config.retry.max_credentials_per_deployment = 7
    config.retry.max_total_attempts = 9
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
        yield client, harness
    await harness.container.shutdown()


ADMIN = {"X-Admin-Token": "test-admin-token"}


async def test_stats_breaks_down_results_by_client_agent(observability_api) -> None:
    client, harness = observability_api
    repo = harness.container.request_repository
    await repo.start(request_id="qoder-ok", requested_model="ds", stream=True, user_agent="qoder")
    await repo.finish(
        "qoder-ok", status="success", http_status=200, attempt_count=2, input_tokens=100
    )
    await repo.start(request_id="qoder-429", requested_model="ds", stream=True, user_agent="qoder")
    await repo.finish(
        "qoder-429",
        status="error",
        http_status=429,
        error_type="rate_limit_error",
        attempt_count=5,
    )
    await repo.start(
        request_id="harness-ok",
        requested_model="ds",
        stream=True,
        user_agent="deepseek-harness",
    )
    await repo.finish(
        "harness-ok", status="success", http_status=200, attempt_count=1, input_tokens=200_000
    )
    await repo.start(request_id="unknown-cancel", requested_model="ds", stream=False)
    await repo.finish("unknown-cancel", status="cancelled", http_status=499, attempt_count=0)

    response = await client.get("/zkadmin/stats?days=7&recent=0", headers=ADMIN)
    assert response.status_code == 200
    clients = {row["user_agent"]: row for row in response.json()["clients"]}
    assert clients["qoder"] == {
        "user_agent": "qoder",
        "requests": 2,
        "success": 1,
        "errors": 1,
        "cancelled": 0,
        "success_rate": 0.5,
        "rate_limited": 1,
        "avg_attempts": 3.5,
        "avg_input_tokens": 100.0,
        "max_input_tokens": 100,
    }
    assert clients["deepseek-harness"]["avg_input_tokens"] == 200_000.0
    assert clients["unknown"]["cancelled"] == 1


async def test_stats_reports_optional_subsystem_usage(observability_api) -> None:
    client, harness = observability_api
    await harness.container.agent_repository.create_session(
        session_id="agent-1", title="测试", model="zk-auto", workspace="."
    )
    await harness.container.event_repository.create(
        {
            "source": "manual",
            "type": "test.event",
            "object": "test",
            "summary": "测试",
            "dedupe_key": "manual:test.event:test",
            "state": "dropped",
        }
    )

    response = await client.get("/zkadmin/stats?days=7&recent=0", headers=ADMIN)
    assert response.status_code == 200
    subsystems = response.json()["subsystems"]
    assert subsystems["agent"]["enabled"] is False
    assert subsystems["agent"]["sessions"] == 1
    assert subsystems["agent"]["last_used_at"].endswith("Z")
    assert subsystems["events"]["enabled"] is True
    assert subsystems["events"]["events"] == 1
    assert subsystems["events"]["last_used_at"].endswith("Z")


async def test_health_exposes_effective_runtime_limits(observability_api) -> None:
    client, _harness = observability_api
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["runtime"] == {
        "stream_idle_timeout": 12.0,
        "stream_prefetch_timeout": 3.0,
        "max_input_tokens": 1234,
        "max_request_seconds": 456.0,
        "max_credentials_per_deployment": 7,
        "max_total_attempts": 9,
        "agent_enabled": False,
        "events_enabled": True,
    }


async def test_stats_aggregates_each_deployment_from_attempt_rows(observability_api) -> None:
    """2026-11-26：每家部署的接单统计必须来自 request_attempts。

    线上 `requests` 表在 cancelled/error 时 deployment_id 100% 为 NULL，
    用它聚合会漏掉全部失败、成功率虚高。attempt 行则每行都有 deployment_id。
    """
    client, harness = observability_api
    repo = harness.container.request_repository
    await repo.start(request_id="dep-ok", requested_model="ds", stream=True)
    await repo.finish("dep-ok", status="success", http_status=200, attempt_count=1)
    now = time.time()
    await repo.add_attempts(
        "dep-ok",
        [
            AttemptOutcome(
                attempt_number=1,
                provider="fake",
                model="upstream",
                deployment_id="dep-a",
                credential_id="key-1",
                status="success",
                started_at=now,
                finished_at=now,
                latency_ms=120.0,
            ),
            AttemptOutcome(
                attempt_number=2,
                provider="fake",
                model="upstream",
                deployment_id="dep-b",
                credential_id="key-1",
                status="error",
                http_status=429,
                error_type="rate_limit_error",
                started_at=now,
                finished_at=now,
                latency_ms=80.0,
            ),
        ],
    )

    response = await client.get("/zkadmin/stats?days=7&recent=0", headers=ADMIN)
    assert response.status_code == 200
    rows = {r["deployment_id"]: r for r in response.json()["deployments"]}
    assert rows["dep-a"]["attempts"] == 1
    assert rows["dep-a"]["requests"] == 1
    assert rows["dep-a"]["success"] == 1
    assert rows["dep-a"]["success_rate"] == 1.0
    assert rows["dep-b"]["attempts"] == 1
    assert rows["dep-b"]["requests"] == 1
    assert rows["dep-b"]["success"] == 0
    assert rows["dep-b"]["rate_limited"] == 1
    assert rows["dep-b"]["success_rate"] == 0.0

def test_attempt_outcome_started_at_defaults_to_the_current_time() -> None:
    """2026-11-26 审查：漏传 started_at 的 attempt 不能再从部署统计里消失。

    默认 0.0 表示 1970 年，会被 `deployment_summary` 的 7 天 cutoff 静默过滤——
    统计少一条且没有任何报错。调度器虽然每处都显式赋值，但默认值不该是个陷阱。
    """
    outcome = AttemptOutcome(attempt_number=1, provider="fake", model="upstream")
    assert outcome.started_at >= time.time() - 5.0
