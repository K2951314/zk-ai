"""保底腿：链上最后一条可用部署可以扫完整个凭据池（2026-11-13）。

背景（真机数据）：``kimi-k3`` 只有一个部署（sensenova）。调度器一律把
凭据截到 ``max_credentials_per_deployment=3`` 把，而池里有 9 把 Key ——
连续 3 次 429 整条请求就失败，剩下 6 把最近健康、还能用的 Key 从未被试，
运营者看到的就是「总是在中途断开」。

这里钉两半，缺一都不能证明修复正确：
1. **只剩一条腿**时，它会扫完整个池（否则那个请求必败）；
2. **还有别的腿**时，仍然截断（让渡设计不能被这次修改破坏）。
"""

from __future__ import annotations

import pytest

from app.models.request import ChatCompletionRequest
from tests.conftest import (
    Behavior,
    build_harness,
    make_alias,
    make_config,
    make_model,
)


def _request(model: str = "zk-solo") -> ChatCompletionRequest:
    return ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "hi"}])


async def test_the_only_leg_scans_the_whole_credential_pool(provider_config, fake_adapter):
    """只剩一个部署时，必须把池里每一把 Key 都试过。

    修复前：``max_credentials_per_deployment=1`` 会只试 1 把就放弃，
    剩下的 Key 明明健康却从未被试。修复后：保底腿扫完全部 3 把。
    """
    config = make_config(
        providers=[provider_config],          # 3 把 Key
        models=[make_model("solo", priority=100)],
        aliases=[make_alias("zk-solo", ["solo"])],
    )
    config.retry.max_credentials_per_deployment = 1   # 让让渡生效得很明显
    config.retry.max_retries_per_credential = 0
    config.retry.max_total_attempts = 20
    config.retry.max_deployments = 5
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    try:
        # 三把 Key 全 429 —— 期望的是「三把都被试过」而不是「只试一把」
        fake_adapter.queue_status(429, 429, 429)

        from app.core.errors import ZKAIError

        with pytest.raises(ZKAIError):
            await harness.service.chat(_request(), request_id="req-last-leg")

        tried = [call["credential_id"] for call in harness.all_calls()]
        assert len(tried) == 3, f"保底腿只试了 {len(tried)} 把 Key：{tried}"
        assert len(set(tried)) == 3, f"同一把 Key 被重复试：{tried}"
    finally:
        await harness.container.shutdown()


async def test_a_backup_leg_still_respects_the_credential_cap(provider_config, fake_adapter):
    """还有另一条健康腿时，让渡照旧：一个请求不多扫 Key。"""
    config = make_config(
        providers=[provider_config],
        models=[
            make_model("first", priority=100),
            make_model("backup", priority=90),
        ],
        # PRIORITY 保证 first 先试、backup 在后
        aliases=[make_alias("zk-two", ["first", "backup"])],
    )
    config.retry.max_credentials_per_deployment = 1
    config.retry.max_retries_per_credential = 0
    config.retry.max_total_attempts = 20
    harness = await build_harness(config, adapters={"fake": fake_adapter})
    try:
        fake_adapter.queue(Behavior(status=429), Behavior(text="backup 顶上"))
        response = await harness.service.chat(_request("zk-two"), request_id="req-cap")
        assert response.choices[0].message.text() == "backup 顶上"
        # 第一条腿只试了 1 把（让渡），然后切到备用腿 —— 不是在同一腿扫 3 把
        creds = [call["credential_id"] for call in harness.all_calls()]
        assert creds[0] != creds[1], f"同一部署上扫了多把 Key：{creds}"
    finally:
        await harness.container.shutdown()
