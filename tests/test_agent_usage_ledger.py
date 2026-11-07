"""工具调用账本的离线测试：只写临时目录。"""
from __future__ import annotations

import json
from pathlib import Path

from app.services.agent import usage_ledger


def _use_tmp(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ZKAI_DATA_DIR", str(tmp_path))


def test_record_and_recent(monkeypatch, tmp_path: Path) -> None:
    _use_tmp(monkeypatch, tmp_path)
    usage_ledger.record(
        tool="save_file",
        server="editor-sdk",
        session_id="s1",
        task_space="/ws",
        ok=True,
        elapsed=0.25,
    )
    usage_ledger.record(
        tool="open_file",
        server="editor-sdk",
        session_id="s1",
        task_space="/ws",
        ok=False,
        elapsed=1.5,
        error="文件被占用",
    )
    rows = usage_ledger.recent()
    assert [row["tool"] for row in rows] == ["open_file", "save_file"]
    assert rows[0]["ok"] is False
    assert rows[0]["error"] == "文件被占用"


def test_recent_missing_file_is_empty(monkeypatch, tmp_path: Path) -> None:
    _use_tmp(monkeypatch, tmp_path)
    assert usage_ledger.recent() == []


def test_recent_skips_broken_lines(monkeypatch, tmp_path: Path) -> None:
    _use_tmp(monkeypatch, tmp_path)
    path = tmp_path / usage_ledger.LEDGER_NAME
    path.write_text(
        '{"tool":"a","ok":true,"elapsed":1}\n不是 JSON\n{"tool":"b","ok":true,"elapsed":2}\n',
        encoding="utf-8",
    )
    rows = usage_ledger.recent()
    assert [row["tool"] for row in rows] == ["b", "a"]


def test_summary_aggregates_by_tool(monkeypatch, tmp_path: Path) -> None:
    _use_tmp(monkeypatch, tmp_path)
    for ok, elapsed in ((True, 1.0), (True, 3.0), (False, 2.0)):
        usage_ledger.record(
            tool="save_file",
            server="editor-sdk",
            session_id="s",
            task_space="/ws",
            ok=ok,
            elapsed=elapsed,
        )
    usage_ledger.record(
        tool="open_file",
        server="editor-sdk",
        session_id="s",
        task_space="/ws",
        ok=True,
        elapsed=0.5,
    )
    summary = usage_ledger.summary()
    top = summary[0]
    assert top["tool"] == "save_file"
    assert top["calls"] == 3
    assert top["ok"] == 2
    assert top["avg_elapsed"] == 2.0
    assert summary[1]["tool"] == "open_file"


def test_record_does_not_leak_arguments(monkeypatch, tmp_path: Path) -> None:
    _use_tmp(monkeypatch, tmp_path)
    usage_ledger.record(
        tool="save_file",
        server="editor-sdk",
        session_id="s",
        task_space="/ws",
        ok=True,
        elapsed=0.1,
    )
    raw = (tmp_path / usage_ledger.LEDGER_NAME).read_text(encoding="utf-8")
    entry = json.loads(raw.strip())
    assert "arguments" not in entry
    # kind 区分「MCP 工具调用」与「外部执行体委派」两类记录（后者带成本字段）
    assert set(entry) == {
        "kind",
        "ts",
        "tool",
        "server",
        "session_id",
        "task_space",
        "ok",
        "elapsed",
        "error",
    }
    assert entry["kind"] == "mcp"


def test_record_swallows_write_errors(monkeypatch, tmp_path: Path) -> None:
    # 把数据目录指到一个「文件」上，mkdir 会失败；record 必须静默。
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ZKAI_DATA_DIR", str(blocker))
    usage_ledger.record(
        tool="x",
        server="s",
        session_id="s",
        task_space="/ws",
        ok=True,
        elapsed=0.1,
    )
    assert usage_ledger.recent() == []


def test_delegation_record_keeps_cost_but_not_task_text(
    monkeypatch, tmp_path: Path
) -> None:
    """委派记录要带成本与模型，但**任务正文一个字都不能落盘**。"""
    _use_tmp(monkeypatch, tmp_path)
    usage_ledger.record_delegation(
        tool="codebuddy",
        model="hy4-preview",
        role="cli-grinder",
        session_id="s1",
        task_space="/ws",
        ok=True,
        elapsed=12.5,
        tokens_in=1200,
        tokens_out=340,
        cost_usd=0.0125,
    )
    entry = json.loads(
        (tmp_path / usage_ledger.LEDGER_NAME).read_text(encoding="utf-8").strip()
    )
    assert entry["kind"] == "delegation"
    assert entry["model"] == "hy4-preview"
    assert entry["cost_usd"] == 0.0125
    assert entry["tokens_in"] == 1200
    assert "task" not in entry
    assert "prompt" not in entry
