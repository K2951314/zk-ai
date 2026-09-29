#!/usr/bin/env python
"""配置自洽性体检：改完 config/*.yaml 立刻跑一次。

为什么需要这个脚本（2026-09-29 的教训）：我在一次脚本化编辑里**误删了整个
`deepseek-v4.1-flash` 模型块**，当时只 `grep` 了一眼就以为没事——grep 只能证明
"某些行还在"，证明不了"没有东西不见了"。直到后来配置校验器报
`alias target is unknown` 才暴露。同类错误同期出现两次（另一次是禁用 nvidia
被控制台保存覆盖回 true）。

所以改完配置应该跑这个脚本，而不是靠肉眼看 diff。退出码非 0 表示有错误。

用法::

    python scripts/check_config.py            # 人读报告
    python scripts/check_config.py --strict   # 有警告也返回非 0（CI 用）
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import AppConfig, load_app_config
from app.models.provider import DeploymentConfig

logger = logging.getLogger("check_config")


def _live_deployments(config: AppConfig, model_id: str) -> list[DeploymentConfig]:
    """该模型真正可达的部署：部署自身启用，且它所属的供应商也启用。"""
    model = config.models.get(model_id)
    if model is None or not model.enabled:
        return []
    live: list[DeploymentConfig] = []
    for dep in model.deployments:
        provider = config.providers.get(dep.provider_id)
        if dep.enabled and provider is not None and provider.enabled:
            live.append(dep)
    return live


def _check_deployments(config: AppConfig, errors: list[str]) -> None:
    for model in config.models.values():
        for dep in model.deployments:
            if dep.provider_id not in config.providers:
                errors.append(f"部署 {dep.id} 指向不存在的供应商 '{dep.provider_id}'")


def _check_aliases(config: AppConfig, errors: list[str]) -> None:
    for alias in config.aliases.values():
        for target in alias.targets:
            if target not in config.models and target not in config.aliases:
                errors.append(
                    f"别名 {alias.name} 的 target '{target}' 既不是模型也不是别名"
                )
        if alias.front_model and alias.front_model not in alias.targets:
            errors.append(
                f"别名 {alias.name} 的 front_model '{alias.front_model}' 不在 targets 内"
                "（接口模型没有可用部署）"
            )


def _check_unreachable_models(config: AppConfig, warnings: list[str]) -> None:
    """「模型 enabled 但所有部署都不可达」最阴：配置看着在，运行时静默跳过。"""
    for model in sorted(config.models.values(), key=lambda m: m.id):
        if not model.enabled:
            continue
        if _live_deployments(config, model.id):
            continue
        declared = [dep.id for dep in model.deployments]
        warnings.append(
            f"模型 {model.id} 已启用但没有任何可用部署"
            f"（声明了 {declared or '无'}；检查对应供应商是否 enabled: false）"
        )


def _check_alias_provider_breadth(config: AppConfig, warnings: list[str]) -> None:
    """别名只剩一个供应商 = 没有 vendor 级容错。"""
    for alias in sorted(config.aliases.values(), key=lambda a: a.name):
        if not alias.enabled:
            continue
        providers: set[str] = set()
        for target in alias.targets:
            for dep in _live_deployments(config, target):
                providers.add(dep.provider_id)
        if len(providers) == 1:
            warnings.append(
                f"别名 {alias.name} 只剩 1 个供应商（{next(iter(providers))}）"
                "——没有 vendor 级容错（若这是有意为之，比如 zk-k3 的定位就是"
                "「只用 K3、不兜底」，可忽略）"
            )


def _check_stale_files(warnings: list[str]) -> None:
    """有人在外面改过配置、网关还没 reload —— 控制台一保存就会覆盖回去。"""
    from app.core import config_writer

    stale = config_writer.stale_config_files()
    if stale:
        warnings.append(
            f"配置文件在加载后被外部改过：{'、'.join(stale)}——"
            "先 POST /admin/config/reload，否则控制台保存会把它覆盖回去"
        )


def _report(config: AppConfig) -> None:
    print("\n可用模型（模型 -> 真正可达的部署）")
    for model in sorted(config.models.values(), key=lambda m: m.id):
        live = [dep.id for dep in _live_deployments(config, model.id)]
        mark = "  " if live else "⚠ "
        shown = "、".join(live) if live else "（不可达）"
        print(f"  {mark}{model.id:<32} {shown}")

    print("\n别名链")
    for alias in sorted(config.aliases.values(), key=lambda a: a.name):
        front = f"  接口模型={alias.front_model}" if alias.front_model else ""
        print(f"  {alias.name:<10} {' → '.join(alias.targets)}{front}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配置自洽性体检")
    parser.add_argument("--strict", action="store_true", help="有警告也返回非 0")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    config = load_app_config()

    errors: list[str] = []
    warnings: list[str] = []

    print("=" * 72)
    print(
        f"模型 {len(config.models)} 个 · 供应商 {len(config.providers)} 个 · "
        f"别名 {len(config.aliases)} 个"
    )
    print("=" * 72)

    _check_deployments(config, errors)
    _check_aliases(config, errors)
    _check_unreachable_models(config, warnings)
    _check_alias_provider_breadth(config, warnings)
    for item in getattr(config, "warnings", None) or []:
        warnings.append(f"[loader] {item}")
    try:
        _check_stale_files(warnings)
    except Exception as exc:
        logger.warning("陈旧检测跳过：%s", exc)

    _report(config)

    if warnings:
        print(f"\n警告 {len(warnings)} 条")
        for item in warnings:
            print(f"  ⚠ {item}")
    if errors:
        print(f"\n错误 {len(errors)} 条")
        for item in errors:
            print(f"  ✗ {item}")

    print("\n" + "=" * 72)
    if errors:
        print(f"结论：{len(errors)} 个错误、{len(warnings)} 个警告")
        return 1
    if warnings and args.strict:
        print(f"结论：无错误，{len(warnings)} 个警告（--strict 下视为失败）")
        return 1
    print(f"结论：无错误，{len(warnings)} 个警告")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
