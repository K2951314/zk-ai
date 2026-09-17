"""Console edits are written back to the YAML files, so a deleted model can never
resurrect on reload/restart (the reported bug: deletion only hit memory + DB)."""

from __future__ import annotations

import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings, load_app_config
from app.main import create_app
from tests.conftest import FakeAdapter, Harness, build_harness

ADMIN = {"x-admin-token": "test-admin-token"}

PROVIDERS_YAML = textwrap.dedent("""\
    # 供应商注释
    providers:
      - id: fake
        type: openai
        base_url: http://fake.test/v1
        credentials:
          - id: key-1
            value: sk-inline-1
            priority: 100
    """)

MODELS_YAML = textwrap.dedent("""\
    # 模型注释
    models:
      # -----
      # 保留模型
      # -----
      - id: keep-me
        enabled: true
        context_window: 128000
        capabilities:
          coding: 5.0
        deployments:
          - id: keep-fake
            provider_id: fake
            model: keep-me
            priority: 100
      # -----
      # 待删模型
      # -----
      - id: delete-me
        enabled: true
        context_window: 128000
        capabilities:
          coding: 5.0
        deployments:
          - id: del-fake
            provider_id: fake
            model: delete-me
            priority: 90

    aliases:
      - name: zk-test
        targets: [keep-me]
        strategy: priority
    """)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "providers.yaml").write_text(PROVIDERS_YAML, encoding="utf-8")
    (tmp_path / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture
async def write_api(config_dir: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Harness, Path]]:
    settings = Settings(
        environment="test",
        config_dir=str(config_dir),
        admin_token="test-admin-token",
        health_check_mode="off",
        health_check_on_startup=False,
        allow_inline_secrets=True,
    )
    config = load_app_config(settings)
    provider = config.providers["fake"]
    adapter = FakeAdapter(provider)
    harness = await build_harness(config, adapters={"fake": adapter})
    # Populate the config-mirror tables (build_harness skips startup), so model
    # deployment FKs resolve.
    await harness.container.config_repository.sync_config(config)
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://zkai.test") as client:
        yield client, harness, config_dir
    await harness.container.shutdown()


async def test_console_model_delete_rewrites_yaml(write_api) -> None:
    client, _, _config_dir = write_api
    models_file = _config_dir / "models.yaml"
    deleted = await client.delete("/admin/models/delete-me", headers=ADMIN)
    assert deleted.status_code == 200
    assert deleted.json()["synced"] == "models.yaml"
    text = models_file.read_text("utf-8")
    assert "delete-me" not in text and "待删模型" not in text  # entry + its header gone
    assert "keep-me" in text and "保留模型" in text  # neighbour untouched


async def test_deleted_model_stays_gone_after_reload(write_api) -> None:
    client, _, _ = write_api
    await client.delete("/admin/models/delete-me", headers=ADMIN)
    reloaded = await client.post("/admin/config/reload", headers=ADMIN)
    assert reloaded.status_code == 200
    ids = {m["id"] for m in (await client.get("/admin/models", headers=ADMIN)).json()["data"]}
    assert "delete-me" not in ids and "keep-me" in ids


async def test_hand_deleting_model_in_file_not_resurrected(write_api) -> None:
    # Operator edits the file by hand (the classic workflow). reload must honour it.
    client, _, config_dir = write_api
    path = config_dir / "models.yaml"
    path.write_text(
        path.read_text("utf-8").replace(
            "  - id: delete-me", "  - id: renamed-by-hand"
        ),
        "utf-8",
    )
    await client.post("/admin/config/reload", headers=ADMIN)
    ids = {m["id"] for m in (await client.get("/admin/models", headers=ADMIN)).json()["data"]}
    assert "renamed-by-hand" in ids and "delete-me" not in ids


async def test_console_model_create_writes_yaml(write_api) -> None:
    client, _, config_dir = write_api
    created = await client.post(
        "/admin/models",
        headers=ADMIN,
        json={
            "id": "new-from-ui", "enabled": True, "context_window": 64000,
            "capabilities": {"coding": 8.0},
            "deployments": [{"id": "nf-fake", "provider_id": "fake", "model": "new-from-ui",
                             "priority": 80}],
        },
    )
    assert created.status_code == 200
    assert created.json()["synced"] == "models.yaml"
    assert "new-from-ui" in (config_dir / "models.yaml").read_text("utf-8")


async def test_console_alias_upsert_and_delete_write_yaml(write_api) -> None:
    client, _, config_dir = write_api
    alias = await client.post(
        "/admin/aliases", headers=ADMIN,
        json={"name": "ui-alias", "targets": ["keep-me"], "strategy": "priority"},
    )
    assert alias.status_code == 200 and alias.json()["synced"] == "models.yaml"
    assert "ui-alias" in (config_dir / "models.yaml").read_text("utf-8")
    gone = await client.delete("/admin/aliases/ui-alias", headers=ADMIN)
    assert gone.status_code == 200
    assert "ui-alias" not in (config_dir / "models.yaml").read_text("utf-8")


async def test_limits_write_to_providers_yaml(write_api) -> None:
    client, _, config_dir = write_api
    res = await client.put(
        "/admin/providers/fake/limits", headers=ADMIN,
        json={"rules": [{"scope": "credential", "window_seconds": 60, "max_requests": 40}]},
    )
    assert res.status_code == 200 and res.json()["synced"] == "providers.yaml"
    assert "max_requests: 40" in (config_dir / "providers.yaml").read_text("utf-8")
