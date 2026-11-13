"""check_config 的可达性体检：**凭据被全禁**的部署不能算「可用」。

为什么要有这条（2026-11-13）：``_live_deployments`` 原来只看
``dep.enabled`` 和 ``provider.enabled``，于是服务器上这个形状骗过了体检：

    HW 供应商 enabled: true
    hw-01   enabled: false      <- 唯一的 Key 被禁用
    deepseek-v4.1-flash-1017 -> 只部署在 HW

体检报「0 警告」，而 ``ds`` 别名链的**第一条腿**就是它 —— 真机上必须等
第一次请求撞 403 才暴露。这是 §已知坑「供应商的 /v1/models 是目录不是
你能调的」同一个错的配置侧变体：**「配置里有」不等于「能调用」**。
"""

from __future__ import annotations

from app.core.config import AppConfig
from app.models.provider import DeploymentConfig, ModelConfig, ProviderConfig, ProviderType
from tests.conftest import make_config, make_provider


def _provider(
    pid: str = "fake",
    *,
    enabled: bool = True,
    key_enabled: bool = True,
    keyless: bool = False,
    key_ids: int | None = None,
) -> ProviderConfig:
    """最小供应商。

    ``keyless`` 用来验证 Ollama 那类不需要 Key 的不会被误杀；
    ``key_ids`` 指定凭据把数（测「轮换上限 vs 池规模」用）。
    """
    if keyless:
        ids: tuple[str, ...] = ()
    elif key_ids is None:
        ids = (f"{pid}-01",)
    else:
        ids = tuple(f"{pid}-{i:02d}" for i in range(1, key_ids + 1))
    provider = make_provider(pid, key_ids=ids, keyless=keyless)
    provider.enabled = enabled
    if keyless:
        # make_provider 的 keyless 只清空 credentials，不改 type ——
        # ``requires_credential`` 判的是 type，必须显式设成 OLLAMA 才测得到。
        provider.type = ProviderType.OLLAMA
    for cred in provider.credentials:
        cred.enabled = key_enabled
    return provider


def _config(provider: ProviderConfig, *, dep_enabled: bool = True) -> AppConfig:
    model = ModelConfig(
        id="m1",
        enabled=True,
        deployments=[
            DeploymentConfig(
                id="m1-dep", provider_id=provider.id, model="m1", enabled=dep_enabled
            )
        ],
    )
    # make_config 会补齐 Settings 等必填字段，别直接构造 AppConfig
    return make_config(providers=[provider], models=[model])


def test_live_deployments_requires_an_enabled_credential():
    """供应商开着、唯一 Key 被禁 → 这个部署**不可达**（2026-11-13 修的盲区）。"""
    from scripts.check_config import _live_deployments

    config = _config(_provider(key_enabled=False))
    assert _live_deployments(config, "m1") == []


def test_live_deployments_accepts_a_provider_with_an_enabled_credential():
    from scripts.check_config import _live_deployments

    config = _config(_provider(key_enabled=True))
    assert [d.id for d in _live_deployments(config, "m1")] == ["m1-dep"]


def test_live_deployments_treats_keyless_providers_as_reachable():
    """Ollama 之类不需要 Key 的供应商不能被这条规则误杀。"""
    from scripts.check_config import _live_deployments

    config = _config(_provider(keyless=True))
    assert config.providers["fake"].type is ProviderType.OLLAMA
    assert [d.id for d in _live_deployments(config, "m1")] == ["m1-dep"]


def test_unreachable_model_warns_when_every_credential_is_disabled():
    """端到端：模型 enabled + 部署 enabled + Key 全禁 → 必须报「没有任何可用部署」。"""
    from scripts.check_config import _check_unreachable_models

    config = _config(_provider(key_enabled=False))
    warnings: list[str] = []
    _check_unreachable_models(config, warnings)
    assert warnings, "Key 全被禁的模型必须被体检抓出来"
    assert "没有任何可用部署" in warnings[0]


def test_disabled_provider_is_already_covered():
    """供应商本身 enabled: false 是旧有的检查，别因为新逻辑退化成漏报。"""
    from scripts.check_config import _live_deployments

    config = _config(_provider(enabled=False, key_enabled=True))
    assert _live_deployments(config, "m1") == []


def test_disabled_deployment_is_still_unreachable():
    """部署自己 enabled: false 的旧检查不能被新逻辑绕过。"""
    from scripts.check_config import _live_deployments

    config = _config(_provider(key_enabled=True), dep_enabled=False)
    assert _live_deployments(config, "m1") == []


# --------------------------------------------------------------------------- #
# 轮换上限 vs 池规模（2026-11-13）：两地取值不同不报错，但可疑关系要提示
# --------------------------------------------------------------------------- #
def test_rotation_ceiling_larger_than_pool_is_flagged():
    """设了超过池大小的值 = 多出的部分永远用不上。"""
    from scripts.check_config import _check_credential_rotation_ceiling

    config = _config(_provider(key_ids=2))  # 2 把
    config.retry.max_credentials_per_deployment = 9
    warnings: list[str] = []
    _check_credential_rotation_ceiling(config, warnings)
    assert warnings and "大于最大凭据池" in warnings[0]


def test_rotation_ceiling_of_one_with_a_pool_is_flagged():
    """设 1 且池 ≥3：撞一次 429 就换部署，兄弟 Key 基本没机会被试。"""
    from scripts.check_config import _check_credential_rotation_ceiling

    config = _config(_provider(key_ids=5))
    config.retry.max_credentials_per_deployment = 1
    warnings: list[str] = []
    _check_credential_rotation_ceiling(config, warnings)
    assert warnings and "兄弟 Key" in warnings[0]


def test_a_deliberate_partial_ceiling_is_not_flagged():
    """3 vs 9 是**有意的让渡**（本地负载轻、服务器账号多）——不该报。"""
    from scripts.check_config import _check_credential_rotation_ceiling

    config = _config(_provider(key_ids=9))
    config.retry.max_credentials_per_deployment = 3
    warnings: list[str] = []
    _check_credential_rotation_ceiling(config, warnings)
    assert warnings == []
