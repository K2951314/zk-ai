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
