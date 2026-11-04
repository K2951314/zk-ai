"""check_config 的事件规则体检：引用不存在的 skill / 角色 / 目录要被抓出来。

为什么要有这组测试（2026-11-03）：``events.yaml`` 三条规则全都引用了
本机不存在的 ``zkai-ops`` / ``zkai-sync``，而体检脚本 ``grep events`` 零命中，
一个都没查。项目史上已经栽过一次同类错（「规则写进文档 ≠ 会执行」），
所以这里把「引用必须被机器校验」钉成测试。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def checker():
    from scripts.check_config import (
        _check_event_types_declared,
        _check_events_rules,
    )

    return _check_events_rules, _check_event_types_declared


def _write_repo(root: Path, rules_yaml: str) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "config" / "events.yaml").write_text(rules_yaml, encoding="utf-8")


def test_a_rule_pointing_at_a_missing_skill_is_reported(
    tmp_path: Path, checker
) -> None:
    check_rules, _ = checker
    _write_repo(
        tmp_path,
        """
rules:
  - name: 演示规则
    when:
      source: zkai-gateway
    to: cli-skill
    skill: definitely-not-installed-skill
""",
    )
    errors: list[str] = []
    warnings: list[str] = []
    check_rules(tmp_path, errors, warnings)
    assert any("definitely-not-installed-skill" in w for w in warnings), (
        f"不存在的 skill 必须被报出来，实际 warnings={warnings}"
    )


def test_a_rule_with_unknown_role_is_an_error(tmp_path: Path, checker) -> None:
    check_rules, _ = checker
    _write_repo(
        tmp_path,
        """
rules:
  - name: 演示规则
    when:
      source: zkai-gateway
    to: cli-does-not-exist
""",
    )
    errors: list[str] = []
    warnings: list[str] = []
    check_rules(tmp_path, errors, warnings)
    # 本机能读到真实的执行体花名册时，未知角色必须是**错误**（不是警告）：
    # 这条规则会永远卡在 received，比缺 skill 更硬。
    assert any("cli-does-not-exist" in e for e in errors), (
        f"未知执行体角色必须报错，实际 errors={errors}"
    )


def test_a_rule_with_missing_cwd_is_an_error(tmp_path: Path, checker) -> None:
    check_rules, _ = checker
    _write_repo(
        tmp_path,
        """
rules:
  - name: 演示规则
    when:
      source: zkai-gateway
    to: cli-skill
    cwd: "不存在的目录/sub"
""",
    )
    errors: list[str] = []
    warnings: list[str] = []
    check_rules(tmp_path, errors, warnings)
    assert any("不存在的目录/sub" in e for e in errors)


def test_a_type_nobody_produces_is_reported(tmp_path: Path, checker) -> None:
    """``rate_limit.burst`` 曾被写进规则但没有任何产出方 —— 永不触发。"""
    _, check_types = checker
    _write_repo(
        tmp_path,
        """
rules:
  - name: 演示规则
    when:
      source: zkai-gateway
      types: [health.quarantined, totally.made.up]
    to: cli-skill
""",
    )
    (tmp_path / "scripts" / "event_watch.py").write_text(
        '{"type": "health.quarantined"}\n', encoding="utf-8"
    )
    warnings: list[str] = []
    check_types(tmp_path, warnings)
    assert any("totally.made.up" in w for w in warnings)
    # 只报那个真没人产出的；消息里会罗列「已产出的有…」，
    # 所以按「只报一条」判断，而不是按字符串包含
    assert len(warnings) == 1, f"只有 totally.made.up 该被报，实际={warnings}"


def test_decided_types_are_not_reported_as_dead(tmp_path: Path, checker) -> None:
    """``<x>.decided`` 由 decide() 派生，静态搜字符串搜不到，不能误报死类型。"""
    _, check_types = checker
    _write_repo(
        tmp_path,
        """
rules:
  - name: 执行决策
    when:
      source: zkai-gateway
      types: [health.quarantined.decided]
    to: cli-skill
""",
    )
    (tmp_path / "scripts" / "event_watch.py").write_text(
        '{"type": "health.quarantined"}\n', encoding="utf-8"
    )
    warnings: list[str] = []
    check_types(tmp_path, warnings)
    assert warnings == [], f".decided 不该被当成死类型，实际={warnings}"


def test_a_type_written_by_another_script_counts_as_produced(
    tmp_path: Path, checker
) -> None:
    _, check_types = checker
    _write_repo(
        tmp_path,
        """
rules:
  - name: 演示规则
    when:
      source: zkai-gateway
      types: [config.invalid]
    to: cli-skill
""",
    )
    (tmp_path / "scripts" / "event_watch.py").write_text(
        '{"type": "health.quarantined"}\n', encoding="utf-8"
    )
    (tmp_path / "scripts" / "check_config.py").write_text(
        '"type": "config.invalid",\n', encoding="utf-8"
    )
    warnings: list[str] = []
    check_types(tmp_path, warnings)
    assert warnings == [], f"别的脚本产出的类型不该被报死，实际={warnings}"
