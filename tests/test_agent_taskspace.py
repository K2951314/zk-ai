"""任务空间与统一权限的离线测试。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.services.agent import taskspace
from app.services.agent.cli.base import build_adapter
from app.services.agent.cli.registry import CliToolConfig


def _cfg(name: str, **overrides: Any) -> CliToolConfig:
    base: dict[str, Any] = {
        "name": name,
        "command": name,
        "backend": "official",
        "model": "占位模型",
        "cost_tier": "paid",
        "timeout": 30.0,
        "approval_mode": "bypass",
    }
    base.update(overrides)
    return CliToolConfig(**base)


def test_create_makes_layout_and_state(tmp_path: Path) -> None:
    space = taskspace.create(tmp_path, session_id="s-1", goal="整理月报")
    for sub in taskspace.SUBDIRS:
        assert (space.path / sub).is_dir()
    state = taskspace.load(tmp_path, space.task_id)
    assert state is not None
    assert state["goal"] == "整理月报"
    assert state["steps"] == []
    assert state["artifacts"] == []


def test_create_is_idempotent(tmp_path: Path) -> None:
    first = taskspace.create(tmp_path, session_id="s", goal="原始目标", task_id="fixed")
    second = taskspace.create(tmp_path, session_id="s", goal="新目标", task_id="fixed")
    assert first.path == second.path
    state = taskspace.load(tmp_path, "fixed")
    assert state is not None
    assert state["goal"] == "原始目标"


def test_update_merges_and_touches_time(tmp_path: Path) -> None:
    space = taskspace.create(tmp_path, session_id="s", goal="g")
    before = taskspace.load(tmp_path, space.task_id)
    assert before is not None
    updated = taskspace.update(
        tmp_path,
        space.task_id,
        steps=[{"id": 1, "status": "done"}],
        handoff_brief="下一步生成图表",
    )
    assert updated is not None
    assert updated["handoff_brief"] == "下一步生成图表"
    assert len(updated["steps"]) == 1
    assert updated["updated_at"] >= before["updated_at"]


def test_update_missing_returns_none(tmp_path: Path) -> None:
    assert taskspace.update(tmp_path, "不存在", steps=[]) is None


def test_list_all_orders_by_created_at(tmp_path: Path) -> None:
    first = taskspace.create(tmp_path, session_id="a", goal="早", task_id="a")
    second = taskspace.create(tmp_path, session_id="b", goal="晚", task_id="b")
    taskspace.update(tmp_path, first.task_id, created_at=1.0)
    taskspace.update(tmp_path, second.task_id, created_at=2.0)
    items = taskspace.list_all(tmp_path)
    assert [item["task_id"] for item in items] == ["b", "a"]
    assert items[0]["goal"] == "晚"


def test_list_all_skips_broken_state(tmp_path: Path) -> None:
    taskspace.create(tmp_path, session_id="a", goal="正常", task_id="ok")
    broken = taskspace.root_for(tmp_path) / "broken"
    broken.mkdir(parents=True)
    (broken / taskspace.STATE_FILE).write_text("不是 JSON", encoding="utf-8")
    items = taskspace.list_all(tmp_path)
    assert [item["task_id"] for item in items] == ["ok"]


def test_is_inside_boundary(tmp_path: Path) -> None:
    space = taskspace.create(tmp_path, session_id="a", goal="g", task_id="t1")
    assert taskspace.is_inside(tmp_path, space.path / "work") is True
    assert taskspace.is_inside(tmp_path, tmp_path / "src") is False
    assert taskspace.is_inside(tmp_path, tmp_path) is False


def test_codex_bypass_uses_dangerous_flag() -> None:
    argv = build_adapter(_cfg("codex")).build_command("任务", ".")
    assert "--dangerously-bypass-approvals-and-sandbox" in argv


def test_gemini_bypass_maps_to_yolo() -> None:
    argv = build_adapter(_cfg("gemini")).build_command("任务", ".")
    assert "yolo" in argv
    assert "bypass" not in argv


def test_qoder_bypass_maps_to_bypass_permissions() -> None:
    argv = build_adapter(_cfg("qoder")).build_command("任务", ".")
    assert "bypass_permissions" in argv


def test_codebuddy_bypass_maps_to_camel_case() -> None:
    argv = build_adapter(_cfg("codebuddy")).build_command("任务", ".")
    assert "bypassPermissions" in argv


def test_qwen_bypass_degrades_to_default() -> None:
    argv = build_adapter(_cfg("qwen")).build_command("任务", ".")
    assert "default" in argv
    assert "bypass" not in argv


def test_acquire_and_release_blocks_other_writer(tmp_path: Path) -> None:
    taskspace.create(tmp_path, session_id="s", goal="g", task_id="t1")
    assert taskspace.acquire(tmp_path, "t1", owner="甲")[0] is True
    ok, holder = taskspace.acquire(tmp_path, "t1", owner="乙")
    assert ok is False
    assert holder == "甲"
    taskspace.release(tmp_path, "t1", owner="甲")
    assert taskspace.acquire(tmp_path, "t1", owner="乙")[0] is True


def test_acquire_is_idempotent_for_same_owner(tmp_path: Path) -> None:
    taskspace.create(tmp_path, session_id="s", goal="g", task_id="t2")
    assert taskspace.acquire(tmp_path, "t2", owner="甲")[0] is True
    assert taskspace.acquire(tmp_path, "t2", owner="甲")[0] is True


def test_release_only_by_owner(tmp_path: Path) -> None:
    taskspace.create(tmp_path, session_id="s", goal="g", task_id="t3")
    taskspace.acquire(tmp_path, "t3", owner="甲")
    taskspace.release(tmp_path, "t3", owner="乙")  # 不是持有者，无效
    ok, holder = taskspace.acquire(tmp_path, "t3", owner="丙")
    assert ok is False
    assert holder == "甲"


def test_expired_lease_can_be_taken(tmp_path: Path) -> None:
    taskspace.create(tmp_path, session_id="s", goal="g", task_id="t4")
    taskspace.acquire(tmp_path, "t4", owner="甲", ttl=0.01)
    time.sleep(0.05)
    ok, holder = taskspace.acquire(tmp_path, "t4", owner="乙", ttl=60)
    assert ok is True
    assert holder == "乙"


def test_lease_is_recorded_in_state(tmp_path: Path) -> None:
    taskspace.create(tmp_path, session_id="s", goal="g", task_id="t5")
    taskspace.acquire(tmp_path, "t5", owner="甲")
    state = taskspace.load(tmp_path, "t5")
    assert state is not None
    lease = state.get("lease")
    assert isinstance(lease, dict)
    assert lease["owner"] == "甲"
    taskspace.release(tmp_path, "t5", owner="甲")
    state = taskspace.load(tmp_path, "t5")
    assert state is not None
    assert state.get("lease") is None


def test_list_all_includes_lease(tmp_path: Path) -> None:
    taskspace.create(tmp_path, session_id="s", goal="g", task_id="t6")
    taskspace.acquire(tmp_path, "t6", owner="甲")
    items = taskspace.list_all(tmp_path)
    assert items[0]["lease"]["owner"] == "甲"


def test_force_release_ignores_owner(tmp_path: Path) -> None:
    taskspace.create(tmp_path, session_id="s", goal="g", task_id="t7")
    taskspace.acquire(tmp_path, "t7", owner="甲")
    holder = taskspace.force_release(tmp_path, "t7")
    assert holder == "甲"
    assert taskspace.acquire(tmp_path, "t7", owner="乙")[0] is True


def test_force_release_without_lease_returns_empty(tmp_path: Path) -> None:
    taskspace.create(tmp_path, session_id="s", goal="g", task_id="t8")
    assert taskspace.force_release(tmp_path, "t8") == ""


def test_task_space_state_is_utf8_json(tmp_path: Path) -> None:
    space = taskspace.create(tmp_path, session_id="s", goal="中文目标")
    raw = space.state_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["goal"] == "中文目标"
