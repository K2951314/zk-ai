"""角色注册表：把一个「角色名」解析成网关里的 alias / model id。

派生子任务时父模型只说「我要个看图的人」，不指定具体供应商。具体派给谁由这里
决定，而不是由模型指定——否则等于让模型绕过能力路由自己挑渠道。

解析顺序（第一个命中即返回）：
1. ``settings.agent_role_aliases``（JSON 字符串，运行时覆盖）
2. :data:`DEFAULT_ROLE_ALIASES`（本文件内的默认表）
3. ``settings.agent_default_model``（兜底，保证任何角色都能跑起来）

未知角色返回 ``None``，由调用方把可用清单回给模型——能力边界由注册表硬拦，
不依赖父模型自觉。
"""

from __future__ import annotations

from app.core.config import Settings

#: 默认的角色 -> alias 映射。
#:
#: ⚠ 现役 config/models.yaml 只配了 zk-auto / zk-k3 / zk-vision 三个 alias。
#:   ``zk-coding`` / ``zk-cheap`` 在 config.py 的 ``_default_aliases()`` 里存在，
#:   但那只在 **yaml 一个 alias 都没配时**才兜底——现役 yaml 配了三个，所以这两个
#:   名字当前不在 ``config.aliases`` 里，写进去会在 validate_model() 处 400。
#:   所以这里只填真实存在的 alias；想要 coder/grinder 走专用链，先在 models.yaml
#:   补 alias，再改这张表。
DEFAULT_ROLE_ALIASES: dict[str, str] = {
    "coder": "zk-auto",
    "grinder": "zk-auto",
    "vision": "zk-vision",
}

#: 角色备注，用于把清单回给模型时说明每个角色适合干什么。
ROLE_HINTS: dict[str, str] = {
    "coder": "写代码 / 改代码 / 调试",
    "grinder": "机械改写、批量整理、按格式产出",
    "vision": "看图、OCR、分析图片内容",
}


def known_roles(settings: Settings | None = None) -> list[str]:
    """可用角色名（默认表 ∪ settings 覆盖），排序保证输出稳定。"""
    names = set(DEFAULT_ROLE_ALIASES)
    if settings is not None:
        names |= set(_overrides(settings))
    return sorted(names)


def resolve_alias(role: str, settings: Settings | None = None) -> str | None:
    """角色名 -> alias/model id；未知角色返回 None。"""
    name = (role or "").strip()
    if not name:
        return None
    if settings is not None:
        alias = _overrides(settings).get(name)
        if alias:
            return alias
    return DEFAULT_ROLE_ALIASES.get(name)


def describe_roles(settings: Settings | None = None) -> str:
    """给人/模型看的角色清单，未知角色时报这个。"""
    fallback = settings.agent_default_model if settings is not None else "zk-auto"
    parts = []
    for name in known_roles(settings):
        alias = resolve_alias(name, settings) or fallback
        hint = ROLE_HINTS.get(name, "")
        parts.append(f"{name}（{hint} -> {alias}）" if hint else f"{name} -> {alias}")
    return "、".join(parts)


def _overrides(settings: Settings) -> dict[str, str]:
    """settings.agent_role_aliases 的 JSON 解析结果（坏 JSON 时静默为空）。"""
    return settings.parsed_role_aliases()


__all__ = [
    "DEFAULT_ROLE_ALIASES",
    "ROLE_HINTS",
    "describe_roles",
    "known_roles",
    "resolve_alias",
]
