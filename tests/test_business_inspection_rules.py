"""业务侧巡检规则必须「有产出方」——防的是 2026-11-03 那类死规则。

为什么单列一条（2026-11-19）：``rate_limit.burst`` / ``config.invalid`` 两条规则
从写下那天起就没有任何产出方，静静躺了半个月。这次新加四条巡检规则，用的
事件类型定义在 ``scripts/signal_probes.py`` 而不是 ``event_watch.py``——
体检脚本的「有没有产出方」检查靠静态搜类型串，跨文件时最容易漏。这里钉住
「四条规则的类型都能被静态找到」，否则规则上线即死。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

#: 2026-11-19 新增的业务侧巡检规则（规则名 → 事件类型）
BUSINESS_RULES = {
    "三菱库存接口自检失败": "inventory.canary_failed",
    "三菱库存接口已恢复": "inventory.canary_recovered",
    "数据新鲜度异常": "data.freshness_failed",
    "数据新鲜度已恢复": "data.freshness_recovered",
}


@pytest.fixture(scope="module")
def ruleset():
    from app.services.events.rules import RuleLoader

    return RuleLoader(REPO_ROOT / "config").load()


def test_business_rules_are_loaded(ruleset) -> None:
    names = {rule.name for rule in ruleset.rules}
    missing = set(BUSINESS_RULES) - names
    assert not missing, f"巡检规则没被加载（events.yaml 写错了？）：{missing}"


@pytest.mark.parametrize("rule_name,event_type", sorted(BUSINESS_RULES.items()))
def test_every_business_rule_has_a_static_producer(rule_name, event_type) -> None:
    """类型串必须能在某个脚本里被静态找到，否则 check_config 会报死类型。"""
    from scripts.check_config import _has_other_producer

    assert _has_other_producer(event_type, REPO_ROOT), (
        f"规则 {rule_name!r} 监听 {event_type!r}，但没有任何脚本产出它"
        " —— 这条规则永远不会触发"
    )


def test_producer_lives_in_signal_probes() -> None:
    """产出方确实在 signal_probes.py（而不是巧合被别处匹配到）。"""
    text = (REPO_ROOT / "scripts" / "signal_probes.py").read_text(encoding="utf-8")
    for event_type in BUSINESS_RULES.values():
        assert f'"{event_type}"' in text, f"{event_type} 不在 signal_probes.py 里"


def test_no_dead_business_type_warnings(ruleset) -> None:
    """整仓体检不该把新规则报成「没有任何产出方」。"""
    from scripts.check_config import _check_event_types_declared

    warnings: list[str] = []
    _check_event_types_declared(REPO_ROOT, warnings)
    dead = [
        w for w in warnings
        if any(event_type in w for event_type in BUSINESS_RULES.values())
    ]
    assert not dead, f"新巡检规则被报成死类型：{dead}"


def test_business_rules_are_read_only(ruleset) -> None:
    """四条巡检规则的指令里必须写明「只读」，防止执行体顺手改数据。

    这条来自项目既有纪律：「只读诊断类任务不要改任何配置或数据」。
    规则文字改了却忘了保留这句 = 把一条通报规则变成写操作。
    """
    by_name = {rule.name: rule for rule in ruleset.rules}
    # 每条规则必须逐字禁掉「真去做就会出事」的那一类动作。
    # 原来的断言只查 "不要" 两个字，属于宽松断言：指令里出现无数个「不要」
    # 也可能恰好没禁到关键动作（2026-11-19 复核收紧）。
    forbidden = {
        "三菱库存接口自检失败": ("登录三菱", "改凭证", "改任何配置"),
        "数据新鲜度异常": ("重跑生成器", "改数据", "commit"),
    }
    for name, actions in forbidden.items():
        ask = by_name[name].ask
        for action in actions:
            assert action in ask, (
                f"{name} 的指令没禁止 {action!r}，执行体可能真去做：{ask!r}"
            )
    # 采集器本身也不该写业务项目的数据文件（只跑金丝雀与 _health）。
    probe_text = (REPO_ROOT / "scripts" / "signal_probes.py").read_text(encoding="utf-8")
    for forbidden in ("shutil.copy", "write_text(", "os.remove(", "subprocess.run([\"git\""):
        assert forbidden not in probe_text, (
            f"signal_probes.py 不该包含 {forbidden!r} —— 巡检必须只读"
        )


def test_tell_semantics() -> None:
    """失败告警要打扰人，恢复告知只落库。"""
    from app.services.events.rules import RuleLoader

    ruleset = RuleLoader(REPO_ROOT / "config").load()
    by_name = {rule.name: rule for rule in ruleset.rules}
    assert by_name["三菱库存接口自检失败"].tell == "always"
    assert by_name["数据新鲜度异常"].tell == "always"
    assert by_name["三菱库存接口已恢复"].tell == "never"
    assert by_name["数据新鲜度已恢复"].tell == "never"
