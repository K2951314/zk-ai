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

#: 仓库根目录。``check_config.py`` 在 ``scripts/`` 下，所以上跳一级。
REPO_ROOT = Path(__file__).resolve().parent.parent


def _provider_has_credentials(config: AppConfig, provider_id: str) -> bool:
    """该供应商还有没有**启用**的凭据（不需要 Key 的供应商恒为 True）。"""
    provider = config.providers.get(provider_id)
    if provider is None or not provider.enabled:
        return False
    if not provider.requires_credential:
        return True
    return any(cred.enabled for cred in provider.credentials)


def _live_deployments(config: AppConfig, model_id: str) -> list[DeploymentConfig]:
    """该模型真正可达的部署：部署启用、供应商启用、且**凭据没被全禁**。

    最后一条是 2026-11-13 补的：原来只看 ``dep.enabled`` 和 ``provider.enabled``，
    于是「供应商开着、唯一那个 Key 是 ``enabled: false``」的部署被算成可达，
    体检报「0 警告」而运行时静默跳过。服务器上的 ``HW`` 正是这个形状
    （``hw-01 enabled=False`` 却让 ``deepseek-v4.1-flash-1017`` 显示为可用）。
    **「配置里有」不等于「能调用」**——同 §已知坑 的三反例表。
    """
    model = config.models.get(model_id)
    if model is None or not model.enabled:
        return []
    live: list[DeploymentConfig] = []
    for dep in model.deployments:
        if not dep.enabled:
            continue
        if not _provider_has_credentials(config, dep.provider_id):
            continue
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
            "先 POST /zkadmin/config/reload，否则控制台保存会把它覆盖回去"
        )


def _check_credential_envs(config: AppConfig, warnings: list[str]) -> None:
    """凭据要读的 Key 是否在 .env 里——不在的话换机必丢。

    为什么值得单列一道：凭据的名字是从**进程环境**解析的（app/credentials/pool.py），
    所以「只在 OS 环境变量里有」的 Key 在这台机器上好好的，而迁移包只带 .env。
    2026-09-29 就在这台机器上抓到一把：SENSENOVA_API_KEY（sensenova-01，
    priority 100）只存在于用户级环境变量，.env 里只有 _02~_10。
    这不是错误——Key 可以来自别处——但必须让运营者看见，由他决定。
    """
    from app.core.config import credential_env_gaps

    gaps = credential_env_gaps(config)
    for name, ids in sorted(gaps.items()):
        where = "、".join(ids)
        from_os = (
            "（只存在于 OS 环境变量，本机可用，换机即丢）"
            if _os_has(name)
            else "（两边都没有，本机就是 DISABLED）"
        )
        warnings.append(
            f"{where} 读的 {name} 不在 .env 里{from_os}——"
            "迁移包只带 .env，补进去才能跟着换机"
        )


def _os_has(name: str) -> bool:
    import os

    return bool(os.environ.get(name))


# --------------------------------------------------------------------------- #
# 事件规则（config/events.yaml）
# --------------------------------------------------------------------------- #
#: 判断「skill 装了没有」时按顺序找的目录。跟 `cli_tools.yaml` 的 codex 一致：
#: codex 只读 ``~/.codex/skills``，claude 读 ``~/.claude/skills``。
_SKILL_ROOTS = ("~/.codex/skills", "~/.claude/skills", "~/.agents/skills", "~/.catpaw/skills")


def _skill_exists(name: str) -> bool:
    for root in _SKILL_ROOTS:
        base = Path(root).expanduser()
        if (base / name / "SKILL.md").is_file():
            return True
        # 也接受 ``my-skills/<name>/SKILL.md`` 这种分层布局
        if (base / "my-skills" / name / "SKILL.md").is_file():
            return True
    return False


def _check_events_rules(
    repo_root: Path, errors: list[str], warnings: list[str]
) -> None:
    """事件规则引用了不存在的东西 —— 派出去等于让 Agent 自由发挥。

    为什么单列一道（2026-11-03）：``events.yaml`` 三条规则全部写了
    ``skill: zkai-ops`` / ``zkai-sync``，而这两个 skill 在本机任何 skills 目录里
    都不存在。``build_task`` 只把名字拼成一句话，执行体找不到就按自己的理解干——
    输出看起来同样像模像样，运营者以为它是按 SOP 办的。这跟「规则写进文档 ≠ 会执行」
    是同一条教训：**引用必须被机器校验，不能靠人记得**。

    同时检查 ``to`` 对应的执行体角色是否可用、``cwd`` 是否存在——这两个出错
    的症状分别是「事件永远停在 received」和「worker 判工作目录越界」，都很隐蔽。
    """
    config_dir = repo_root / "config"
    rule_file = None
    for candidate in ("events.yaml", "events.yml"):
        if (config_dir / candidate).is_file():
            rule_file = config_dir / candidate
            break
    if rule_file is None:
        # 没启用中台就没有可检查的，不算错
        return

    from app.services.events.rules import RuleLoader

    ruleset = RuleLoader(config_dir).load()
    for warn in ruleset.warnings:
        warnings.append(f"[事件规则] {warn}")

    if not ruleset.rules:
        warnings.append(f"[事件规则] {rule_file.name} 里一条规则都没有：所有事件都会以 dropped 落库")
        return

    try:
        from app.services.agent.cli.registry import load_cli_registry

        app_config = load_app_config()
        available_roles = set(load_cli_registry(app_config.settings).known_roles())
    except Exception as exc:  # 执行体花名册读不出来不能挡住其他检查
        logger.warning("执行体花名册读取失败，跳过角色校验：%s", exc)
        available_roles = set()

    for rule in ruleset.rules:
        if rule.skill and not _skill_exists(rule.skill):
            warnings.append(
                f"[事件规则] 规则 {rule.name!r} 引用的 skill {rule.skill!r} 本机不存在"
                f"（找过 {'、'.join(_SKILL_ROOTS)}）——派出去后 Agent 会自由发挥，"
                "结论不可复现"
            )
        if rule.to and available_roles and rule.to not in available_roles:
            errors.append(
                f"[事件规则] 规则 {rule.name!r} 的 to={rule.to!r} 不是可用执行体角色"
                f"（可用：{'、'.join(sorted(available_roles)) or '无'}）"
                "——这条规则永远卡在 received"
            )
        if rule.cwd and rule.cwd not in (".", "./"):
            target = Path(rule.cwd)
            if not target.is_absolute():
                target = repo_root / target
            if not target.is_dir():
                errors.append(
                    f"[事件规则] 规则 {rule.name!r} 的 cwd={rule.cwd!r} 不存在"
                    "——worker 会判工作目录越界，任务直接失败"
                )


def _check_event_types_declared(
    repo_root: Path, warnings: list[str]
) -> None:
    """规则监听了采集器永远不会产出的类型 —— 那条规则会静静躺一辈子。

    2026-11-03 实测：``events.yaml`` 写了 ``rate_limit.burst`` 和 ``config.invalid``，
    而 ``event_watch.py`` 只会产出 health.quarantined / health.degraded /
    config.drift / git.behind。两条规则从写下那天起就没可能触发。
    """
    watcher = repo_root / "scripts" / "event_watch.py"
    config_dir = repo_root / "config"
    if not watcher.is_file():
        return
    rule_file = None
    for candidate in ("events.yaml", "events.yml"):
        if (config_dir / candidate).is_file():
            rule_file = config_dir / candidate
            break
    if rule_file is None:
        return

    import re

    produced = set(re.findall(r'"type":\s*"([a-z_.]+)"', watcher.read_text(encoding="utf-8")))
    if not produced:
        return

    from app.services.events.rules import RuleLoader

    ruleset = RuleLoader(config_dir).load()
    dead: dict[str, list[str]] = {}
    for rule in ruleset.rules:
        if not rule.types or not rule.sources:
            continue
        # 通配符不做静态判断（可能命中未来新增的类型）
        if any(any(ch in pattern for ch in "*?[") for pattern in rule.types):
            continue
        # 只判来源是 event_watch 的规则；外部来源（邮件、GitHub…）在别处产出
        if not any(src in ("zkai-gateway", "server") for src in rule.sources):
            continue
        for event_type in rule.types:
            if event_type in produced or _has_other_producer(event_type, repo_root):
                continue
            dead.setdefault(event_type, []).append(rule.name)

    for event_type, rule_names in sorted(dead.items()):
        rules = "、".join(f"{name!r}" for name in rule_names)
        warnings.append(
            f"[事件规则] 类型 {event_type!r}（规则 {rules}）没有任何产出方："
            f"event_watch.py 只会产出 {'、'.join(sorted(produced))}"
            "——这一条监听永远不会触发"
        )


def _has_other_producer(event_type: str, repo_root: Path) -> bool:
    """这个类型有没有 event_watch 之外的产出方。

    两类要认：
    1. 别的脚本里直接写了这个类型串（以后加的采集器、运维脚本）；
    2. ``<x>.decided`` —— 这类由 :meth:`EventService.decide` 在拍板后派生，
       源码里是 ``f"{parent_type}.decided"`` 拼出来的，静态搜字符串搜不到。
       不认这一类的话，新加的闭环规则会被误报成「永不触发」。
    """
    if event_type.endswith(".decided"):
        return True
    needle = f'"{event_type}"'
    for path in (repo_root / "scripts").glob("*.py"):
        try:
            if needle in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


def _check_shadowed_envs(warnings: list[str]) -> None:
    """OS 环境变量顶掉了 .env 的值——只打 stderr 的警告不算警告。

    ``load_dotenv(override=False)`` 意味着进程环境赢，而 loader 只用 logging
    报一声：肉眼能看见，``--strict`` 数不到，CI 也就拦不住。同族的
    :func:`_check_credential_envs` 走的是 warnings 列表，这里对齐过来。
    """
    from app.core.config import shadowed_env_names

    shadowed = shadowed_env_names()
    if shadowed:
        warnings.append(
            f".env 的值被 OS 环境变量顶掉（进程环境赢）：{'、'.join(shadowed)}"
            "——如果是陈旧的，在系统环境变量里清掉；判定见 AGENTS.md 的"
            "「shadowed by pre-existing process environment」条"
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
    parser.add_argument(
        "--emit-event",
        action="store_true",
        help="有错误时往事件中台投一条 config.invalid（配合定时任务使用）",
    )
    parser.add_argument("--gateway", default="http://127.0.0.1:8317", help="事件中台地址")
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
    _check_credential_envs(config, warnings)
    _check_shadowed_envs(warnings)
    _check_events_rules(REPO_ROOT, errors, warnings)
    _check_event_types_declared(REPO_ROOT, warnings)
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
    if args.emit_event and errors:
        _emit_invalid_event(errors, args.gateway)
    if errors:
        print(f"结论：{len(errors)} 个错误、{len(warnings)} 个警告")
        return 1
    if warnings and args.strict:
        print(f"结论：无错误，{len(warnings)} 个警告（--strict 下视为失败）")
        return 1
    print(f"结论：无错误，{len(warnings)} 个警告")
    return 0


def _emit_invalid_event(errors: list[str], gateway: str) -> None:
    """体检报错 -> 投一条 ``config.invalid`` 事件。

    为什么需要这一脚（2026-11-03）：``events.yaml`` 监听了 ``config.invalid``，
    但全仓没有任何地方产出它——那条规则从写下那天起就不会触发。
    体检本来就是「唯一会知道配置坏了」的程序，让它在报错时顺手投一条，
    规则才不是死的。

    投递失败**不影响退出码**：体检本身已经用非 0 表达了失败，
    中台没起来不应该把 CI 也一起弄坏。
    """
    import os

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx 是运行期依赖，不该缺
        return
    token = os.environ.get("ZKAI_ADMIN_TOKEN", "")
    payload = {
        "source": "zkai-gateway",
        "type": "config.invalid",
        "object": "check_config",
        "summary": f"配置体检报出 {len(errors)} 个错误",
        "link": "",
        "payload": {"errors": errors, "fingerprint": "|".join(sorted(errors))[:300]},
    }
    try:
        httpx.post(
            f"{gateway.rstrip('/')}/zkadmin/events",
            json=payload,
            headers={"X-Admin-Token": token} if token else {},
            timeout=10,
        )
    except Exception as exc:  # 中台没起来是常态，不该打断体检
        logger.warning("投递 config.invalid 事件失败：%s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
