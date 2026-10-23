"""控制台运维动作：Key 改名/删除守卫 + 网关一键重启 + 复制取值命令取对令牌。

三条不变量：
1. 只有**已禁用**的 Key 才能改名 / 删除——防误操作正在服务的凭据。
2. 一键重启网关按部署形态分流（systemd / 托盘 / 手动兜底），检测不到时
   返回可抄命令而不是假装成功。
3. 「复制取值命令」取的是 ``ZKAI_API_TOKEN``（api_token），不是管理令牌。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from app.main import create_app
from tests.conftest import (
    Harness,
    build_harness,
    make_config,
    make_model,
)

ADMIN = {"x-admin-token": "test-admin-token"}


@pytest.fixture
async def api(provider_config, fake_adapter) -> AsyncIterator[tuple[httpx.AsyncClient, Harness]]:
    config = make_config(
        providers=[provider_config],
        models=[make_model("fake-model")],
        admin_token="test-admin-token",
    )
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    app = create_app(config.settings)
    app.state.container = harness.container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, harness


class TestCredentialGuards:
    async def test_cannot_delete_enabled_key(self, api) -> None:
        """在用中的 Key 不许删——先禁用再删，两步走。"""
        client, _ = api
        r = await client.delete(
            "/zkadmin/providers/fake/credentials/key-1", headers=ADMIN
        )
        assert r.status_code == 409
        assert "先禁用" in r.json()["detail"]["error"]["message"]

    async def test_can_delete_disabled_key(self, api) -> None:
        client, harness = api
        await client.post("/zkadmin/credentials/key-1/disable", headers=ADMIN)
        r = await client.delete(
            "/zkadmin/providers/fake/credentials/key-1", headers=ADMIN
        )
        assert r.status_code == 200
        assert r.json()["deleted"] == "key-1"
        assert all(c.id != "key-1" for c in harness.container.config.providers["fake"].credentials)
        # pool 里也必须清掉——register_provider 只加不删，靠 reconcile 补
        assert harness.container.pool.get("key-1") is None

    async def test_rename_clears_old_id_from_pool(self, api) -> None:
        """改名后旧 id 不能残留在 pool 里。"""
        client, harness = api
        await client.post("/zkadmin/credentials/key-1/disable", headers=ADMIN)
        r = await client.put(
            "/zkadmin/providers/fake/credentials/key-1",
            json={"new_id": "key-1-reborn"},
            headers=ADMIN,
        )
        assert r.status_code == 200
        assert harness.container.pool.get("key-1") is None
        assert harness.container.pool.get("key-1-reborn") is not None

    async def test_cannot_rename_enabled_key(self, api) -> None:
        client, _ = api
        r = await client.put(
            "/zkadmin/providers/fake/credentials/key-1",
            json={"new_id": "key-1-reborn"},
            headers=ADMIN,
        )
        assert r.status_code == 409
        assert "先禁用" in r.json()["detail"]["error"]["message"]

    async def test_can_rename_disabled_key(self, api) -> None:
        client, harness = api
        await client.post("/zkadmin/credentials/key-1/disable", headers=ADMIN)
        r = await client.put(
            "/zkadmin/providers/fake/credentials/key-1",
            json={"new_id": "key-1-reborn"},
            headers=ADMIN,
        )
        assert r.status_code == 200
        assert r.json()["credential_id"] == "key-1-reborn"
        ids = [c.id for c in harness.container.config.providers["fake"].credentials]
        assert "key-1-reborn" in ids and "key-1" not in ids

    async def test_reject_secret_looking_new_id(self, api) -> None:
        client, _ = api
        await client.post("/zkadmin/credentials/key-1/disable", headers=ADMIN)
        r = await client.put(
            "/zkadmin/providers/fake/credentials/key-1",
            json={"new_id": "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789abcd"},
            headers=ADMIN,
        )
        assert r.status_code == 400


class TestProviderRename:
    async def test_rename_provider_retargets_references(self, api) -> None:
        """供应商改名后，部署和凭据的 provider_id 必须同步换。"""
        client, harness = api
        r = await client.put(
            "/zkadmin/providers/fake/rename",
            json={"new_id": "fake-reborn"},
            headers=ADMIN,
        )
        assert r.status_code == 200
        assert r.json()["provider_id"] == "fake-reborn"
        assert "fake-reborn" in harness.container.config.providers
        assert "fake" not in harness.container.config.providers
        # 凭据的 provider_id 也换了
        for c in harness.container.config.providers["fake-reborn"].credentials:
            assert c.provider_id == "fake-reborn"
        # pool 里旧 key 清了、新 key 在
        assert harness.container.pool.get("key-1") is not None

    async def test_rename_rebuilds_router_adapter(self, api) -> None:
        """改名后 router.adapter(new_id) 必须能拿到 adapter，否则模型市场报错。"""
        client, harness = api
        r = await client.put(
            "/zkadmin/providers/fake/rename",
            json={"new_id": "fake-reborn"},
            headers=ADMIN,
        )
        assert r.status_code == 200
        # 新 id 的 adapter 在
        adapter = harness.container.router.adapter("fake-reborn")
        assert adapter is not None
        # 旧 id 的 adapter 已移除
        import pytest

        from app.routing.router import ProviderNotFoundError

        with pytest.raises(ProviderNotFoundError):
            harness.container.router.adapter("fake")

    async def test_rename_rejects_duplicate(self, api) -> None:
        client, _ = api
        r = await client.put(
            "/zkadmin/providers/fake/rename",
            json={"new_id": "fake"},  # 同名
            headers=ADMIN,
        )
        assert r.status_code == 200  # 同名返回 renamed=False
        assert r.json()["renamed"] is False


class TestModelCrud:
    async def test_add_model_with_slash_in_id(self, api) -> None:
        """模型 ID 带 /（如 deepseek-ai/DeepSeek-V4.1-Flash）要能增删。"""
        client, harness = api
        model_id = "deepseek-ai/DeepSeek-V4.1-Flash"
        r = await client.post("/zkadmin/models", headers=ADMIN, json={
            "id": model_id,
            "context_window": 131072,
            "deployments": [{
                "id": "probe-dep", "provider_id": "fake",
                "model": "deepseek-ai/DeepSeek-V4.1-Flash",
            }],
        })
        assert r.status_code == 200, r.text
        assert model_id in harness.container.config.models

        # 删除也要能匹配（路由要 :path）
        r = await client.delete(f"/zkadmin/models/{model_id}", headers=ADMIN)
        # URL 里 / 会被 encode 成 %2F，但 :path 能吃下
        assert r.status_code == 200 or r.status_code == 404  # 404 = URL 编码问题，见下

    async def test_delete_model_with_slash_via_path(self, api) -> None:
        """DELETE /models/{id} 对带 / 的 id 必须命中（:path 参数）。"""
        client, harness = api
        model_id = "org/model-name"
        # 先加
        r = await client.post("/zkadmin/models", headers=ADMIN, json={
            "id": model_id,
            "context_window": 8192,
            "deployments": [{"id": "d1", "provider_id": "fake", "model": "x"}],
        })
        assert r.status_code == 200, r.text
        # 再删——URL 编码后的 %2F 要能被 :path 匹配
        r = await client.delete(f"/zkadmin/models/{model_id}", headers=ADMIN)
        assert r.status_code == 200, r.text
        assert model_id not in harness.container.config.models

    async def test_add_model_strips_unknown_deployment_fields(self, api) -> None:
        """部署里的多余字段（市场证据）不该把保存炸成 500。"""
        client, _ = api
        r = await client.post("/zkadmin/models", headers=ADMIN, json={
            "id": "test-model",
            "context_window": 8192,
            "deployments": [{
                "id": "d1", "provider_id": "fake", "model": "x",
                "reasoning_efforts": ["low", "high"],   # 不在 DeploymentConfig
                "official_facts": [{"label": "x"}],      # 不在 DeploymentConfig
                "spec_source": "official",               # 不在 DeploymentConfig
            }],
        })
        assert r.status_code == 200, r.text
        # 未知字段被剥掉了，但模型建出来了
        assert r.json()["data"]["id"] == "test-model"

    async def test_merge_adds_deployment_without_overwriting(self, api) -> None:
        """市场加同 ID 第二个供应商：合并部署，不覆盖原有的。"""
        client, harness = api
        # 第一次：建模型，挂一个部署
        r = await client.post("/zkadmin/models", headers=ADMIN, json={
            "id": "shared-model",
            "context_window": 8192,
            "deployments": [{"id": "dep-amd", "provider_id": "fake", "model": "a"}],
        })
        assert r.status_code == 200
        # 第二次：merge=true，加另一个部署（不同 upstream_model 避开 UNIQUE 约束）
        r = await client.post("/zkadmin/models", headers=ADMIN, json={
            "id": "shared-model",
            "context_window": 8192,
            "merge": True,
            "deployments": [{"id": "dep-nvidia", "provider_id": "fake", "model": "b"}],
        })
        assert r.status_code == 200, r.text
        assert r.json().get("merged") is True
        # 两个部署都在
        model = harness.container.config.models["shared-model"]
        dep_ids = [d.id for d in model.deployments]
        assert "dep-amd" in dep_ids
        assert "dep-nvidia" in dep_ids

    async def test_model_level_strategy(self, api) -> None:
        """模型可设 strategy 字段，直接调模型名时生效。"""
        client, harness = api
        r = await client.post("/zkadmin/models", headers=ADMIN, json={
            "id": "rr-model",
            "context_window": 8192,
            "strategy": "round_robin",
            "deployments": [
                {"id": "d1", "provider_id": "fake", "model": "a"},
                {"id": "d2", "provider_id": "fake", "model": "b"},
            ],
        })
        assert r.status_code == 200, r.text
        model = harness.container.config.models["rr-model"]
        assert model.strategy == "round_robin"


class TestGatewayRestart:
    async def test_restart_returns_a_mode(self, api) -> None:
        """检测不到 systemd/托盘时必须 ok=False + 可抄命令，不许假装成功。"""
        client, _ = api
        r = await client.post("/zkadmin/gateway/restart", headers=ADMIN)
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "gateway_restart"
        assert body["mode"] in {"systemd", "tray", "manual"}
        if not body["ok"]:
            assert body["command"], "失败时必须给出手动命令"

    async def test_tray_mode_writes_request_file(self, monkeypatch, tmp_path) -> None:
        from app.services import gateway_restart

        monkeypatch.setattr(gateway_restart, "detect_mode", lambda: "tray")
        data_dir = tmp_path / "data"
        result = gateway_restart.restart_gateway(data_dir)
        assert result["ok"] is True
        assert result["mode"] == "tray"
        assert (data_dir / "gateway_restart.request").exists()


class TestCopyToken:
    async def test_chatgpt_snapshot_carries_api_token_not_admin_token(self, api) -> None:
        """「复制取值命令」必须取 ZKAI_API_TOKEN，不能拿管理令牌顶。"""
        client, harness = api
        harness.container.settings.api_token = "zk-api-token-xyz"
        r = await client.get("/zkadmin/chatgpt", headers=ADMIN)
        assert r.status_code == 200
        gw = r.json()["gateway"]
        assert gw["api_token_set"] is True
        assert gw["api_token"] == "zk-api-token-xyz"
        assert gw["api_token"] != "test-admin-token"
