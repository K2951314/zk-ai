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


async def test_new_provider_has_adapter_immediately(write_api) -> None:
    """Regression: adding a provider used to leave the router without an adapter,
    so the very next ``GET /admin/providers/{id}/models`` 500'd with
    ``provider ... is unknown or disabled`` until the operator reloaded."""
    client, harness, _ = write_api
    created = await client.post(
        "/admin/providers",
        headers=ADMIN,
        json={
            "id": "stepfun",
            "type": "openai_compatible",
            "base_url": "http://127.0.0.1:9/v1",
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    assert "stepfun" in harness.container.router.adapters()


async def test_deleted_provider_loses_adapter_immediately(write_api) -> None:
    """Deleting a provider must drop its adapter too, not just the config entry."""
    client, harness, _ = write_api
    assert "fake" in harness.container.router.adapters()
    deleted = await client.delete("/admin/providers/fake", headers=ADMIN)
    # 'fake' still serves models in this fixture, so the delete is refused.
    # Strip its deployments first, then delete.
    assert deleted.status_code == 409
    # Aliases pin the models, models pin the provider — unwind in order.
    await client.delete("/admin/aliases/zk-test", headers=ADMIN)
    for model_id in ("keep-me", "delete-me"):
        await client.delete(f"/admin/models/{model_id}", headers=ADMIN)
    deleted = await client.delete("/admin/providers/fake", headers=ADMIN)
    assert deleted.status_code == 200, deleted.text
    assert "fake" not in harness.container.router.adapters()


async def test_disabled_provider_edit_drops_adapter(write_api) -> None:
    """Toggling ``enabled=false`` via the console must also retire the adapter;
    otherwise ``adapter()`` keeps handing it out and routing leaks to a provider
    the operator thinks is off."""
    client, harness, _ = write_api
    updated = await client.post(
        "/admin/providers",
        headers=ADMIN,
        json={
            "id": "fake",
            "type": "openai",
            "base_url": "http://fake.test/v1",
            "enabled": False,
        },
    )
    assert updated.status_code == 200, updated.text
    assert "fake" not in harness.container.router.adapters()


# --------------------------------------------------------------------------- #
# Adding a key: the secret goes to .env, never to the YAML
# --------------------------------------------------------------------------- #
async def test_add_key_writes_env_not_yaml(write_api, tmp_path, monkeypatch) -> None:
    """The reported gap: adding a key from the console left nothing in .env, so the
    credential resolved to ``missing`` and every request failed."""
    client, _harness, config_dir = write_api
    env_path = tmp_path / "env-under-test"
    env_path.write_text("ZKAI_PORT=8317\n", encoding="utf-8")
    monkeypatch.setattr("app.api.admin._env_file", lambda: env_path)

    res = await client.post(
        "/admin/providers/fake/credentials",
        headers=ADMIN,
        json={
            "id": "fake-09",
            "env_var": "FAKE_API_KEY_09",
            "value": "sk-" + "b" * 40,
            "write_env": True,
            "priority": 70,
        },
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["env_synced"].startswith(".env")
    # 1. the secret landed in .env
    assert "FAKE_API_KEY_09=sk-" + "b" * 40 in env_path.read_text("utf-8")
    # 2. the YAML holds only the name
    yaml_text = (config_dir / "providers.yaml").read_text("utf-8")
    assert "FAKE_API_KEY_09" in yaml_text
    assert "b" * 40 not in yaml_text, "the key must never reach providers.yaml"
    # 3. the live process can resolve it without a restart
    import os

    assert os.environ["FAKE_API_KEY_09"] == "sk-" + "b" * 40


async def test_add_key_refuses_key_pasted_as_id(write_api) -> None:
    """The exact mistake that put a real StepFun key into providers.yaml + the DB."""
    client, _, config_dir = write_api
    secret = "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
    res = await client.post(
        "/admin/providers/fake/credentials",
        headers=ADMIN,
        json={"id": secret, "env_var": "STEP_API_KEY"},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["error"]["type"] == "secret_in_id"
    assert secret not in (config_dir / "providers.yaml").read_text("utf-8")


async def test_write_env_requires_value_and_name(write_api, monkeypatch, tmp_path) -> None:
    client, _, _ = write_api
    monkeypatch.setattr("app.api.admin._env_file", lambda: tmp_path / "unused.env")
    missing_value = await client.post(
        "/admin/providers/fake/credentials",
        headers=ADMIN,
        json={"id": "k1", "env_var": "SOME_VAR", "write_env": True},
    )
    assert missing_value.status_code == 400
    missing_name = await client.post(
        "/admin/providers/fake/credentials",
        headers=ADMIN,
        json={"id": "k2", "value": "sk-x", "write_env": True},
    )
    assert missing_name.status_code == 400
    assert not (tmp_path / "unused.env").exists()


async def test_provider_models_notes_when_key_missing(write_api, tmp_path, monkeypatch) -> None:
    """Probing a provider with no usable key must explain itself, not 500.

    The marketplace panel drives this endpoint, so an unexplained 500 lands as a
    raw traceback in the console for what is really "you haven't added a key yet".
    """
    client, _, config_dir = write_api
    # 'fake' has a credential whose env var holds nothing resolvable.
    ((config_dir / "providers.yaml").write_text(
        "providers:\n  - id: fake\n    type: openai\n    base_url: http://fake.test/v1\n"
        "    credentials:\n      - id: key-1\n        env_var: NOT_A_REAL_VAR\n        priority: 100\n",
        encoding="utf-8",
    ))
    monkeypatch.delenv("NOT_A_REAL_VAR", raising=False)
    reloaded = await client.post("/admin/config/reload", headers=ADMIN)
    assert reloaded.status_code == 200, reloaded.text

    res = await client.get("/admin/providers/fake/models", headers=ADMIN)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["data"] == []
    assert "NOT_A_REAL_VAR" in body["note"]
    assert "加 Key" in body["note"]


async def test_provider_models_notes_when_provider_disabled(write_api, config_dir) -> None:
    client, _, _ = write_api
    disabled = await client.post(
        "/admin/providers",
        headers=ADMIN,
        json={"id": "fake", "type": "openai", "base_url": "http://fake.test/v1", "enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    res = await client.get("/admin/providers/fake/models", headers=ADMIN)
    assert res.status_code == 200
    assert "禁用" in res.json()["note"]


# --------------------------------------------------------------------------- #
# Model marketplace: measured facts outrank the hand-written preset
# --------------------------------------------------------------------------- #
async def test_market_marks_discovered_and_guessed_rows(write_api, monkeypatch) -> None:
    """The console has to tell "measured" from "guessed" - a wrong preset sold as
    fact is how a 1M-context vision model ends up configured for 128k, no vision."""
    client, harness, _ = write_api

    class RichAdapter(FakeAdapter):
        async def model_catalogue(self, credential=None):
            return {
                "fake-model": {"id": "fake-model", "context_length": 262144,
                               "supports_vision": True, "supported_parameters": ["temperature"]},
                "fake-blind": {"id": "fake-blind"},  # bare id, like NVIDIA/ModelScope answer
            }

    rich = RichAdapter(harness.container.config.providers["fake"])
    harness.container.router.register_adapter("fake", rich)

    res = await client.get("/admin/providers/fake/models", headers=ADMIN)
    assert res.status_code == 200, res.text
    rows = {r["upstream_model"]: r for r in res.json()["data"]}
    discovered = rows["fake-model"]
    assert discovered["discovered"]["context_window"] == 262144
    assert discovered["discovered"]["vision_input"] is True
    assert discovered["discovered_known"] is True
    assert discovered["context_window_source"] == "provider"
    # vision was measured, so the preset's capability score is overridden to a fact
    assert discovered["preset"]["capabilities"]["vision"] == 10.0
    # ...and the same row carries the plain-language explanation for the form
    assert any(row["label"] == "图像输入" for row in discovered["facts_note"])

    guessed = rows["fake-blind"]
    assert guessed["discovered_known"] is False
    assert guessed["context_window_source"] == "preset"
    assert "facts_note" not in guessed


async def test_market_falls_back_to_plain_id_list(write_api) -> None:
    """Adapters that only know ids (the default) still produce rows, all guessed."""
    client, _, _ = write_api
    res = await client.get("/admin/providers/fake/models", headers=ADMIN)
    assert res.status_code == 200
    rows = res.json()["data"]
    assert rows, "the default catalogue wraps list_models"
    assert all(row["discovered_known"] is False for row in rows)
    assert all(row["context_window_source"] == "preset" for row in rows)
