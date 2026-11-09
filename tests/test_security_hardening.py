"""2026-11-08 对抗式审查（docs/对抗式审查-全面梳理-20261108.md）P0/P1 的安全回归。

每例对应报告里的一个编号发现：

- burner_log 两个端点曾完全无鉴权（SSE 当年为了让 EventSource 能连把鉴权去掉）；
- admin 面在「显式远程部署 + 未配令牌」时 fail-open 裸奔，其中 /zkadmin/chatgpt
  还把推理令牌明文下发；
- /v1/models 是唯一没挂客户端鉴权的 /v1 面；
- taskspace detail/release 把 URL 段直拼进文件路径（路径穿越）。

判据取舍都写在对应源码注释里；这里钉住行为不变。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from tests.conftest import build_harness, make_config

ADMIN = {"X-Admin-Token": "tok"}


@asynccontextmanager
async def _client(config) -> AsyncIterator[AsyncClient]:
    """与 test_api.py 的 api 夹具同构：假上游 + 内存库 + 真 ASGI。"""
    harness = await build_harness(config)
    app = create_app(config.settings)
    app.state.container = harness.container
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://zkai.test"
    ) as c:
        yield c
    await harness.container.shutdown()


# --------------------------------------------------------------------------- #
# burner_log：/zkadmin/burner/log/* 必须在 admin 令牌之后
# --------------------------------------------------------------------------- #


async def test_burner_log_tail_requires_admin() -> None:
    """无令牌 401；header 或 query 令牌都放行（SSE 发不了 header）。"""
    config = make_config(admin_token="tok")
    async with _client(config) as client:
        anonymous = await client.get("/zkadmin/burner/log/tail")
        assert anonymous.status_code == 401

        by_header = await client.get("/zkadmin/burner/log/tail", headers=ADMIN)
        assert by_header.status_code == 200

        by_query = await client.get("/zkadmin/burner/log/tail?token=tok")
        assert by_query.status_code == 200


async def test_burner_log_stream_requires_admin() -> None:
    """SSE 端点也必须先过鉴权：无令牌 401，且发生在流开始之前。

    这里不能测「带令牌的流」：ASGITransport 要等 app 发完 response body
    才返回，而 _follow 是无限流，测试会挂死。?token= 的放行逻辑由
    tail 那条覆盖（同一个 router 级依赖 require_admin_query）。
    """
    config = make_config(admin_token="tok")
    async with _client(config) as client:
        rejected = await client.get("/zkadmin/burner/log/stream?lines=1")
        assert rejected.status_code == 401


async def test_wrong_query_token_is_rejected() -> None:
    """?token= 也必须过常数时间比较，不是「带了就算」。"""
    config = make_config(admin_token="tok")
    async with _client(config) as client:
        r = await client.get("/zkadmin/burner/log/tail?token=wrong")
        assert r.status_code == 401


# --------------------------------------------------------------------------- #
# admin 面 fail-safe：显式远程部署 + 未配令牌 = 拒绝挂载
# --------------------------------------------------------------------------- #


async def test_admin_surface_refuses_to_mount_when_declared_remote_without_token() -> None:
    """/zkadmin/* 整面 404，而不是裸奔——控制台打不开是一眼能懂的事故。"""
    config = make_config()  # admin_token=None
    config.settings.exposure = "server"
    async with _client(config) as client:
        assert (await client.get("/zkadmin/chatgpt")).status_code == 404
        assert (await client.get("/zkadmin/burner/log/tail")).status_code == 404
        assert (await client.get("/zkadmin/agent/sessions")).status_code == 404
        # 不属于 admin 域的面照常活着
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/v1/models")).status_code == 200


async def test_admin_surface_still_mounts_locally_without_token() -> None:
    """本地开发语义不变：未显式声明远程 + 未配令牌 = 面照常（文档写明的行为）。"""
    config = make_config()  # host 默认，exposure 空，无 public_base_url
    async with _client(config) as client:
        assert (await client.get("/zkadmin/chatgpt")).status_code in (200, 401, 403)


# --------------------------------------------------------------------------- #
# /zkadmin/chatgpt 的推理令牌：fail-closed
# --------------------------------------------------------------------------- #


async def test_api_token_hidden_when_admin_surface_has_no_lock() -> None:
    """管理面没上锁时，推理令牌不给——否则匿名 GET 即可拿走它调 /v1/*。"""
    config = make_config()  # admin_token=None（本地 fail-open 语义保留）
    config.settings.api_token = "sk-secret-value"
    async with _client(config) as client:
        r = await client.get("/zkadmin/chatgpt")
        assert r.status_code == 200
        gw = r.json()["gateway"]
        assert gw["api_token"] == ""
        assert gw["api_token_hidden"] is True


async def test_api_token_still_served_once_admin_token_exists() -> None:
    """配了管理令牌（请求真过了鉴权）时，复制取值命令的语义不变。"""
    config = make_config(admin_token="tok")
    config.settings.api_token = "sk-secret-value"
    async with _client(config) as client:
        r = await client.get("/zkadmin/chatgpt", headers=ADMIN)
        assert r.status_code == 200
        gw = r.json()["gateway"]
        assert gw["api_token"] == "sk-secret-value"
        assert gw["api_token_hidden"] is False


# --------------------------------------------------------------------------- #
# /v1/models：补齐 /v1 面的客户端鉴权
# --------------------------------------------------------------------------- #


async def test_v1_models_requires_bearer_when_api_token_set() -> None:
    config = make_config(admin_token="tok")
    config.settings.api_token = "sk-secret-value"
    async with _client(config) as client:
        anonymous = await client.get("/v1/models")
        assert anonymous.status_code == 401

        authorized = await client.get(
            "/v1/models", headers={"Authorization": "Bearer sk-secret-value"}
        )
        assert authorized.status_code == 200


async def test_v1_models_stays_open_when_api_token_unset() -> None:
    """未配置 api_token 的既有部署（本地开发）行为不变。"""
    config = make_config(admin_token="tok")
    async with _client(config) as client:
        assert (await client.get("/v1/models")).status_code == 200


# --------------------------------------------------------------------------- #
# taskspace detail/release：URL 段直拼路径的穿越口
# --------------------------------------------------------------------------- #


async def test_taskspace_endpoints_reject_path_traversal() -> None:
    config = make_config(admin_token="tok")
    async with _client(config) as client:
        # 编码斜杠：试图跳出 .taskspaces/ 读别人的 state.json
        assert (
            await client.get("/zkadmin/agent/taskspaces/..%2F..%2F..%2Fsecret", headers=ADMIN)
        ).status_code == 404
        # 编码点：单靠 ".." 也要被白名单挡住（它解析到工作区上一层的 state.json）
        assert (
            await client.get("/zkadmin/agent/taskspaces/%2E%2E", headers=ADMIN)
        ).status_code == 404
        # release 同样不许删工作区外的 state.lock
        assert (
            await client.post(
                "/zkadmin/agent/taskspaces/..%2F..%2Fsecret/release", headers=ADMIN
            )
        ).status_code == 404


async def test_taskspace_id_whitelist_matches_generated_shape() -> None:
    """生成器产出的形状（字母开头 + 数字/点/横线/下划线）必须照常通过白名单。"""
    config = make_config(admin_token="tok")
    async with _client(config) as client:
        # 合法形状但不存在 → 走到 load 的 404 分支，而不是白名单的 404
        r = await client.get("/zkadmin/agent/taskspaces/20261108-abcd-deadbeef", headers=ADMIN)
        assert r.status_code == 404
        assert "任务空间不存在" in r.json()["detail"]
