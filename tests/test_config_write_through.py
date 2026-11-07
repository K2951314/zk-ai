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
    deleted = await client.delete("/zkadmin/models/delete-me", headers=ADMIN)
    assert deleted.status_code == 200
    assert deleted.json()["synced"] == "models.yaml"
    text = models_file.read_text("utf-8")
    assert "delete-me" not in text and "待删模型" not in text  # entry + its header gone
    assert "keep-me" in text and "保留模型" in text  # neighbour untouched


async def test_deleted_model_stays_gone_after_reload(write_api) -> None:
    client, _, _ = write_api
    await client.delete("/zkadmin/models/delete-me", headers=ADMIN)
    reloaded = await client.post("/zkadmin/config/reload", headers=ADMIN)
    assert reloaded.status_code == 200
    ids = {m["id"] for m in (await client.get("/zkadmin/models", headers=ADMIN)).json()["data"]}
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
    await client.post("/zkadmin/config/reload", headers=ADMIN)
    ids = {m["id"] for m in (await client.get("/zkadmin/models", headers=ADMIN)).json()["data"]}
    assert "renamed-by-hand" in ids and "delete-me" not in ids


async def test_console_model_create_writes_yaml(write_api) -> None:
    client, _, config_dir = write_api
    created = await client.post(
        "/zkadmin/models",
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
        "/zkadmin/aliases", headers=ADMIN,
        json={"name": "ui-alias", "targets": ["keep-me"], "strategy": "priority"},
    )
    assert alias.status_code == 200 and alias.json()["synced"] == "models.yaml"
    assert "ui-alias" in (config_dir / "models.yaml").read_text("utf-8")
    gone = await client.delete("/zkadmin/aliases/ui-alias", headers=ADMIN)
    assert gone.status_code == 200
    assert "ui-alias" not in (config_dir / "models.yaml").read_text("utf-8")


async def test_limits_write_to_providers_yaml(write_api) -> None:
    client, _, config_dir = write_api
    res = await client.put(
        "/zkadmin/providers/fake/limits", headers=ADMIN,
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
        "/zkadmin/providers",
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
    deleted = await client.delete("/zkadmin/providers/fake", headers=ADMIN)
    # 'fake' still serves models in this fixture, so the delete is refused.
    # Strip its deployments first, then delete.
    assert deleted.status_code == 409
    # Aliases pin the models, models pin the provider — unwind in order.
    await client.delete("/zkadmin/aliases/zk-test", headers=ADMIN)
    for model_id in ("keep-me", "delete-me"):
        await client.delete(f"/zkadmin/models/{model_id}", headers=ADMIN)
    deleted = await client.delete("/zkadmin/providers/fake", headers=ADMIN)
    assert deleted.status_code == 200, deleted.text
    assert "fake" not in harness.container.router.adapters()


async def test_disabled_provider_edit_drops_adapter(write_api) -> None:
    """Toggling ``enabled=false`` via the console must also retire the adapter;
    otherwise ``adapter()`` keeps handing it out and routing leaks to a provider
    the operator thinks is off."""
    client, harness, _ = write_api
    updated = await client.post(
        "/zkadmin/providers",
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
    monkeypatch.setattr("app.api.admin._common._env_file", lambda: env_path)

    res = await client.post(
        "/zkadmin/providers/fake/credentials",
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
        "/zkadmin/providers/fake/credentials",
        headers=ADMIN,
        json={"id": secret, "env_var": "STEP_API_KEY"},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["error"]["type"] == "secret_in_id"
    assert secret not in (config_dir / "providers.yaml").read_text("utf-8")


async def test_write_env_requires_value_and_name(write_api, monkeypatch, tmp_path) -> None:
    client, _, _ = write_api
    monkeypatch.setattr("app.api.admin._common._env_file", lambda: tmp_path / "unused.env")
    missing_value = await client.post(
        "/zkadmin/providers/fake/credentials",
        headers=ADMIN,
        json={"id": "k1", "env_var": "SOME_VAR", "write_env": True},
    )
    assert missing_value.status_code == 400
    missing_name = await client.post(
        "/zkadmin/providers/fake/credentials",
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
    reloaded = await client.post("/zkadmin/config/reload", headers=ADMIN)
    assert reloaded.status_code == 200, reloaded.text

    res = await client.get("/zkadmin/providers/fake/models", headers=ADMIN)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["data"] == []
    assert "NOT_A_REAL_VAR" in body["note"]
    assert "加 Key" in body["note"]


async def test_provider_models_notes_when_provider_disabled(write_api, config_dir) -> None:
    client, _, _ = write_api
    disabled = await client.post(
        "/zkadmin/providers",
        headers=ADMIN,
        json={"id": "fake", "type": "openai", "base_url": "http://fake.test/v1", "enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    res = await client.get("/zkadmin/providers/fake/models", headers=ADMIN)
    assert res.status_code == 200
    assert "禁用" in res.json()["note"]


async def test_provider_models_falls_back_to_a_sibling_key(write_api, config_dir, monkeypatch) -> None:
    """第一把 Key 缺值不该挡死目录探测——池里还有好 Key 就该用（2026-10-05 实测：
    sensenova-01 是留空的网关独享位，_02~_09 都有值，整页却报「Key 没有实际值」）。"""
    client, _, _ = write_api
    (config_dir / "providers.yaml").write_text(
        "providers:\n  - id: fake\n    type: openai\n    base_url: http://fake.test/v1\n"
        "    credentials:\n"
        "      - id: key-empty\n        env_var: NOT_A_REAL_VAR\n        priority: 100\n"
        "      - id: key-good\n        env_var: A_REAL_VAR\n        priority: 90\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("NOT_A_REAL_VAR", raising=False)
    monkeypatch.setenv("A_REAL_VAR", "sk-real")
    reloaded = await client.post("/zkadmin/config/reload", headers=ADMIN)
    assert reloaded.status_code == 200, reloaded.text

    res = await client.get("/zkadmin/providers/fake/models", headers=ADMIN)
    assert res.status_code == 200, res.text
    body = res.json()
    # 不该再报「Key 没有实际值」——要么拿到目录，要么报探测失败（上游是假的）
    assert "没有实际值" not in body.get("note", "")


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

    res = await client.get("/zkadmin/providers/fake/models", headers=ADMIN)
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


async def test_a_stale_provider_catalogue_never_shrinks_an_official_window(write_api) -> None:
    """商汤目录报 262144，官方文档写 1M——**目录不等于官方文档**。

    2026-11-04 实测：sensenova-6.8-flash 的 ``GET /v1/models`` 报
    ``context_length: 262144``，官方文档写「支持1 M 上下文窗口」，真请求塞
    1,047,678 prompt tokens 照样 200。旧规则「探测压过预设」于是把这个 1M
    模型配成 256K——低估窗口不报错，只会让长请求在网关这层被判死、静默降级。
    新规则：两者都有时取大的，并把分歧挂到 ``context_window_conflict`` 上。
    """
    client, harness, _ = write_api

    class StaleAdapter(FakeAdapter):
        async def model_catalogue(self, credential=None):
            return {
                # 官方 preset 是 1M（deepseek-flash），目录却说 256K
                "deepseek-flash": {"id": "deepseek-flash", "context_length": 262144},
            }

    harness.container.router.register_adapter(
        "fake", StaleAdapter(harness.container.config.providers["fake"]))

    res = await client.get("/zkadmin/providers/fake/models", headers=ADMIN)
    assert res.status_code == 200, res.text
    row = {r["upstream_model"]: r for r in res.json()["data"]}["deepseek-flash"]
    assert row["preset"]["context_window"] == 1_048_576, "目录的旧值不能把小窗口写进表单"
    # source 仍报 preset（这个字段说的是「社区官方资料页」）；
    # 「这份 preset 是不是核实过的官方规格」由 spec_source 单独表达
    assert row["context_window_source"] == "preset"
    assert row["spec_source"] == "official"
    assert row["context_window_conflict"] == {"provider": 262144, "spec": 1_048_576}


async def test_a_larger_provider_window_still_wins(write_api) -> None:
    """反方向要让探测赢：供应商真的给了更大的窗口，就按它来。"""
    client, harness, _ = write_api

    class BiggerAdapter(FakeAdapter):
        async def model_catalogue(self, credential=None):
            return {"deepseek-flash": {"id": "deepseek-flash", "context_length": 2097152}}

    harness.container.router.register_adapter(
        "fake", BiggerAdapter(harness.container.config.providers["fake"]))
    res = await client.get("/zkadmin/providers/fake/models", headers=ADMIN)
    row = {r["upstream_model"]: r for r in res.json()["data"]}["deepseek-flash"]
    assert row["preset"]["context_window"] == 2_097_152
    assert row["context_window_source"] == "provider"
    assert row["context_window_conflict"] == {"provider": 2_097_152, "spec": 1_048_576}


async def test_market_falls_back_to_plain_id_list(write_api) -> None:
    """Adapters that only know ids (the default) still produce rows, all guessed."""
    client, _, _ = write_api
    res = await client.get("/zkadmin/providers/fake/models", headers=ADMIN)
    assert res.status_code == 200
    rows = res.json()["data"]
    assert rows, "the default catalogue wraps list_models"
    assert all(row["discovered_known"] is False for row in rows)
    assert all(row["context_window_source"] == "preset" for row in rows)


# --------------------------------------------------------------------------- #
# 手填上游模型 ID：接口目录不等于「这个供应商全部能调的模型」
# --------------------------------------------------------------------------- #
async def test_market_accepts_manual_ids_outside_the_catalogue(write_api) -> None:
    """2026-11-07 实测：NVIDIA 网页模型库有 kimi-k3.1-pro，接口目录 84 个里没有。

    市场只认目录，就等于把「目录里没有 ≠ 不能调」这条路堵死。手填的 ID 必须
    走同一条补全流水线（预设 / 牌价 / 已收录），并在行上标明来源。
    """
    client, harness, _ = write_api

    class OneModelAdapter(FakeAdapter):
        async def model_catalogue(self, credential=None):
            return {"fake-model": {"id": "fake-model"}}

    harness.container.router.register_adapter(
        "fake", OneModelAdapter(harness.container.config.providers["fake"]))

    res = await client.get(
        "/zkadmin/providers/fake/models",
        params={"manual_models": "fake-model, moonshotai/kimi-k3.1-pro"},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    rows = res.json()["data"]
    assert {row["upstream_model"] for row in rows} == {
        "fake-model", "moonshotai/kimi-k3.1-pro",
    }
    by_id = {row["upstream_model"]: row for row in rows}
    # 本来就在目录里的 ID 不重复、也不标「手填」
    assert by_id["fake-model"]["manual"] is False
    manual = by_id["moonshotai/kimi-k3.1-pro"]
    assert manual["manual"] is True
    assert manual["suggested_model_id"] == "kimi-k3.1-pro"
    assert manual["discovered_known"] is False, "手填的行没有上游实测事实"
    assert manual["already_added"] is False


async def test_manual_ids_are_deduplicated_and_capped(write_api) -> None:
    """手填上限必须 <= 后端一次能实测的数量，否则会有几个永远停在「未验证」。"""
    from app.api.admin._common import VerifyModelsRequest
    from app.api.admin.providers import _MANUAL_ID_CAP

    assert VerifyModelsRequest.model_fields["limit"].default >= _MANUAL_ID_CAP

    client, _, _ = write_api
    many = ",".join(f"vendor/model-{i}" for i in range(30))
    res = await client.get(
        "/zkadmin/providers/fake/models",
        params={"manual_models": many + "，vendor/model-0, vendor/model-1"},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    manual = [row for row in res.json()["data"] if row["manual"]]
    assert len(manual) == _MANUAL_ID_CAP, "手填 ID 要去重并限量"
    assert len({row["upstream_model"] for row in manual}) == _MANUAL_ID_CAP


async def test_market_without_manual_ids_still_marks_every_row(write_api) -> None:
    """不带手填参数时行为不能变：每行照样有 manual 字段，且全是 False。"""
    client, _, _ = write_api
    res = await client.get("/zkadmin/providers/fake/models", headers=ADMIN)
    assert res.status_code == 200
    rows = res.json()["data"]
    assert rows and all(row["manual"] is False for row in rows)


async def test_manual_ids_survive_a_failed_catalogue_probe(write_api) -> None:
    """目录探测失败时，手填的模型仍然要能列出来并实测。

    这恰恰是最需要手填的场景：NVIDIA 的 ``/v1/models`` 本身就经常超时。
    旧实现一见探测失败就早退，等于手填入口在最该用的时候失灵。
    """
    client, harness, _ = write_api

    class BrokenAdapter(FakeAdapter):
        async def model_catalogue(self, credential=None):
            raise RuntimeError("upstream timeout")

    harness.container.router.register_adapter(
        "fake", BrokenAdapter(harness.container.config.providers["fake"]))

    res = await client.get(
        "/zkadmin/providers/fake/models",
        params={"manual_models": "moonshotai/kimi-k3.1-pro"},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert [row["upstream_model"] for row in body["data"]] == [
        "moonshotai/kimi-k3.1-pro",
    ]
    assert body["data"][0]["manual"] is True
    assert "目录探测失败" in body["note"]


async def test_manual_ids_take_the_first_token_of_a_pasted_line(write_api) -> None:
    """从网页复制「模型名 + 描述」时，不能把描述里的每个词都当成模型 ID。

    按空格拆会让十几个垃圾词吃满 12 个验证额度，真正的 ID 反而排不上。
    """
    client, _, _ = write_api
    res = await client.get(
        "/zkadmin/providers/fake/models",
        params={
            "manual_models": (
                "moonshotai/kimi-k3.1-pro 3T-class open model\n"
                "vendor/second-model"
            ),
        },
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    manual = [row["upstream_model"] for row in res.json()["data"] if row["manual"]]
    assert manual == ["moonshotai/kimi-k3.1-pro", "vendor/second-model"]


async def test_an_overlong_manual_id_is_dropped_not_forwarded(write_api) -> None:
    """超长 ID 不能进 URL：12 个 500 字符的串会把 GET 顶成 414。"""
    client, _, _ = write_api
    res = await client.get(
        "/zkadmin/providers/fake/models",
        params={"manual_models": "x" * 500 + ",good/model"},
        headers=ADMIN,
    )
    assert res.status_code == 200, res.text
    manual = [row["upstream_model"] for row in res.json()["data"] if row["manual"]]
    assert manual == ["good/model"]


async def test_a_stale_config_save_returns_409_not_500(write_api) -> None:
    """外部改过 models.yaml 之后从控制台保存，必须是 409 + 「先 reload」。

    2026-09-29 第三轮审查抓到的真 bug：`ConfigStaleError` 原本继承
    ``RuntimeError``，从五个写入点的 `except (OSError, ValueError)` **穿过去**
    变成 500——`_file_write_failed` 里那段专门写的 409 提示是死代码。
    这条测试钉在**唯一的消费者**（HTTP 端点）上，因为守护契约的旧测试只走到
    `config_writer` 边界，从没走到这里。
    """
    from app.core.config_writer import ConfigStaleError

    # 类型契约先钉住：它必须是「值不合法」，才能被所有写入点现有的 except 收到
    assert issubclass(ConfigStaleError, ValueError), (
        "改成别的基类会让 except (OSError, ValueError) 再次漏掉它，"
        "运营者又会拿到 500 而不是 409"
    )

    client, _harness, config_dir = write_api
    models_file = config_dir / "models.yaml"

    # 模拟运营者在外部编辑器里改了文件（网关没有 reload）。
    # 加一行注释是最真实的形状：内容变了、但别名和模型都还在。
    original = models_file.read_text(encoding="utf-8")
    models_file.write_text(original + "\n# 运营者刚在这里加了一行\n", encoding="utf-8")

    # 删别名这条路径直接落到写文件（不像删模型那样有「还被引用」的前置 409）
    response = await client.delete("/zkadmin/aliases/zk-test", headers=ADMIN)

    assert response.status_code == 409, (
        f"期望 409（可操作的「先 reload」），实际 {response.status_code}: {response.text}"
    )
    body = response.json()
    # FastAPI 把 HTTPException 的 detail 再包一层 "detail"
    error = body["detail"]["error"]
    assert error["type"] == "config_stale"
    assert "reload" in error["hint"].lower()
    # 文件必须没被覆盖——闸门的意义就在这
    assert "运营者刚在这里加了一行" in models_file.read_text(encoding="utf-8")
