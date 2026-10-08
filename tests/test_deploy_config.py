"""部署形态：ZKAI_PUBLIC_BASE_URL / 回退 / /health 的 deploy 块。

核心不变量只有一条：**客户端该用的地址必须由一个地方算出来**。host:port 推不出
反代前缀（Caddy 的 /zkai），所以显式配置优先，缺失时回落到 host:port——这三条
组合起来才让「本地零配置可用」与「服务器抄到的地址真能打开」同时成立。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services import chatgpt_service as cg


class TestPublicBaseUrl:
    def test_explicit_config_wins(self) -> None:
        s = Settings(public_base_url="https://120.53.28.29/zkai", port=8318)
        assert s.public_base_url_effective == "https://120.53.28.29/zkai"

    def test_falls_back_to_host_port(self) -> None:
        s = Settings(host="127.0.0.1", port=8317)
        assert s.public_base_url_effective == "http://127.0.0.1:8317"

    def test_trailing_slash_is_normalised(self) -> None:
        s = Settings(public_base_url="https://example.com/zkai/")
        assert s.public_base_url_effective == "https://example.com/zkai"

    def test_client_base_url_appends_v1_once(self) -> None:
        assert Settings(public_base_url="https://x/zkai").client_base_url == (
            "https://x/zkai/v1"
        )
        assert Settings(public_base_url="https://x/zkai/v1").client_base_url == (
            "https://x/zkai/v1"
        )

    def test_is_remote_when_public_url_configured(self) -> None:
        # 反代后面 bind 回环也常见——配了公网入口就算远程，控制台才会走
        # 「公网调用」那条路，而不是让运营者抄一个只在服务器本机能用的地址。
        s = Settings(host="127.0.0.1", public_base_url="https://x/zkai")
        assert s.is_remote_deploy is True

    def test_is_remote_when_host_is_not_loopback(self) -> None:
        assert Settings(host="0.0.0.0").is_remote_deploy is True
        assert Settings(host="127.0.0.1").is_remote_deploy is False
        assert Settings(host="localhost").is_remote_deploy is False

    def test_exposure_overrides_inference(self) -> None:
        assert Settings(exposure="local", host="0.0.0.0").is_remote_deploy is False
        assert Settings(exposure="server", host="127.0.0.1").is_remote_deploy is True


class TestDeployContext:
    def test_remote_hides_local_writes(self) -> None:
        ctx = cg.deploy_context(Settings(public_base_url="https://x/zkai"))
        assert ctx["is_remote"] is True
        assert ctx["exposure"] == "server"
        assert ctx["can_write_local"] is False
        assert ctx["client_base_url"] == "https://x/zkai/v1"

    def test_local_keeps_local_writes(self) -> None:
        ctx = cg.deploy_context(Settings(host="127.0.0.1", port=8317))
        assert ctx["is_remote"] is False
        assert ctx["exposure"] == "local"
        assert ctx["can_write_local"] is True

    def test_drift_is_reported_but_not_raised(self) -> None:
        """base_url 漂移是提醒不是错误：客户端可能刻意走另一条链路。"""
        s = Settings(public_base_url="https://120.53.28.29/zkai")
        cfg = cg.ChatGptConfig(base_url="http://127.0.0.1:8317/v1")
        assert cg.validate(cfg) == []  # 保存本身合法
        notes = cg.base_url_drift(cfg, s)
        assert notes and "120.53.28.29" in notes[0]

    def test_no_drift_when_urls_match(self) -> None:
        s = Settings(public_base_url="https://120.53.28.29/zkai")
        cfg = cg.ChatGptConfig(base_url="https://120.53.28.29/zkai/v1")
        assert cg.base_url_drift(cfg, s) == []


class TestSettingsYamlBridge:
    def test_deploy_section_is_bridged(self) -> None:
        from app.core.config import _SETTINGS_SECTIONS, apply_settings_from_yaml

        assert "deploy.public_base_url" in _SETTINGS_SECTIONS
        assert "deploy.exposure" in _SETTINGS_SECTIONS
        merged = apply_settings_from_yaml(
            Settings(),
            {"deploy": {"public_base_url": "https://from-yaml/zkai"}},
        )
        assert merged.public_base_url == "https://from-yaml/zkai"

    def test_env_wins_over_yaml(self) -> None:
        import os

        from app.core.config import apply_settings_from_yaml

        os.environ["ZKAI_PUBLIC_BASE_URL"] = "https://from-env/zkai"
        try:
            merged = apply_settings_from_yaml(
                Settings(),
                {"deploy": {"public_base_url": "https://from-yaml/zkai"}},
            )
            assert merged.public_base_url == "https://from-env/zkai"
        finally:
            del os.environ["ZKAI_PUBLIC_BASE_URL"]


class TestHealthDeployBlock:
    @pytest.mark.asyncio
    async def test_health_reports_deploy(self, tmp_path: Path) -> None:
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app
        from tests.conftest import build_harness, make_alias, make_config, make_model

        config = make_config(
            models=[make_model("fake-model")],
            aliases=[make_alias("zk-auto", ["fake-model"])],
            admin_token="t",
        )
        config.settings.public_base_url = "https://120.53.28.29/zkai"
        config.settings.data_dir = tmp_path
        harness = await build_harness(config)
        app = create_app(config.settings)
        app.state.container = harness.container
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://zkai.test"
            ) as client:
                resp = await client.get("/health")
            body = resp.json()
            deploy = body["deploy"]
            assert deploy["is_remote"] is True
            assert deploy["public_base_url"] == "https://120.53.28.29/zkai"
            assert deploy["client_base_url"] == "https://120.53.28.29/zkai/v1"
            assert deploy["burner_log_path"].endswith("burn_sensenova.log")
            assert deploy["burner_state_path"].endswith("burn_state.json")
        finally:
            await harness.container.shutdown()

    @pytest.mark.asyncio
    async def test_health_local_defaults(self, tmp_path: Path) -> None:
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app
        from tests.conftest import build_harness, make_alias, make_config, make_model

        config = make_config(
            models=[make_model("fake-model")],
            aliases=[make_alias("zk-auto", ["fake-model"])],
            admin_token="t",
        )
        config.settings.host = "127.0.0.1"
        config.settings.port = 8317
        config.settings.public_base_url = ""
        config.settings.data_dir = tmp_path
        harness = await build_harness(config)
        app = create_app(config.settings)
        app.state.container = harness.container
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://zkai.test"
            ) as client:
                resp = await client.get("/health")
            deploy = resp.json()["deploy"]
            assert deploy["is_remote"] is False
            assert deploy["public_base_url"] == "http://127.0.0.1:8317"
            assert json.dumps(deploy)  # 必须可序列化
        finally:
            await harness.container.shutdown()


class TestBurnerDir:
    """burner 的账本/日志目录可以与网关 data_dir 分开。

    服务器上 zkai-burner.service 用 --state-file/--log-file 把它们指到
    /var/lib/zkai/burner/，比网关的 ZKAI_DATA_DIR 深一层。拼 data_dir 会让
    控制台去读写一个不存在的账本——运营者以为校准生效了，真正的 burner 什么都
    没看到。这个类钉住「可以分开」与「默认不分开」两条。
    """

    def test_defaults_to_data_dir(self, tmp_path: Path) -> None:
        from app.core.config import Settings

        s = Settings(data_dir=tmp_path)
        assert s.resolved_burner_dir == s.resolved_data_dir
        assert s.burner_state_path == s.resolved_data_dir / "burn_state.json"
        assert s.burner_log_path == s.resolved_data_dir / "burn_sensenova.log"

    def test_absolute_path_wins(self, tmp_path: Path) -> None:
        from app.core.config import Settings

        burner = tmp_path / "elsewhere"
        s = Settings(data_dir=tmp_path, burner_dir=burner)
        assert s.resolved_burner_dir == burner
        assert s.burner_state_path == burner / "burn_state.json"
        assert s.burner_log_path == burner / "burn_sensenova.log"

    @pytest.mark.asyncio
    async def test_health_and_log_endpoints_agree_on_the_path(
        self, tmp_path: Path
    ) -> None:
        """/health 报的路径必须就是 SSE 端点实际读的那个。"""
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app
        from tests.conftest import build_harness, make_alias, make_config, make_model

        data_dir = tmp_path / "data"
        burner_dir = tmp_path / "burner"
        data_dir.mkdir()
        burner_dir.mkdir()
        config = make_config(
            models=[make_model("fake-model")],
            aliases=[make_alias("zk-auto", ["fake-model"])],
            admin_token="t",
        )
        config.settings.data_dir = data_dir
        config.settings.burner_dir = burner_dir
        harness = await build_harness(config)
        app = create_app(config.settings)
        app.state.container = harness.container
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://zkai.test"
            ) as client:
                health = (await client.get("/health")).json()["deploy"]
                tail = await client.get(
                    "/zkadmin/burner/log/tail", headers={"X-Admin-Token": "t"}
                )
            assert health["burner_log_path"] == str(burner_dir / "burn_sensenova.log")
            assert tail.status_code == 200
            # 日志不在，端点要说明原因而不是假装成功
            assert "还没有日志" in tail.text
        finally:
            await harness.container.shutdown()
