"""委派 prompt 的构造：任务信封、段间交接、产物清单（2026-11-13）。

这几条都是纯函数，测试直接对着行为断言，不牵涉子进程。
"""
from __future__ import annotations

from pathlib import Path

from app.services.agent.cli.prompting import (
    HANDOFF_CHARS,
    build_task_prompt,
    collect_artifacts,
    format_artifacts,
    with_context,
)


def test_task_prompt_carries_the_envelope_and_keeps_the_task_verbatim():
    """信封回答三个问题：在哪（绝对路径）、怎么交活、能不能问。

    任务原文必须完整保留在最后——平台只加约束，不改写语义。
    """
    out = build_task_prompt("把 config.yaml 的 timeout 改成 300", cwd=r"D:\ws\.taskspaces\t1\work")
    assert "【工作目录】D:\\ws\\.taskspaces\\t1\\work" in out
    assert "没有人能回答你的追问" in out
    assert "outputs/" in out
    assert out.endswith("【任务】\n把 config.yaml 的 timeout 改成 300")


def test_handoff_carries_the_previous_result_with_a_cap():
    """上一段结论注入下一段，但必须有上限：整段历史会按倍数烧额度。"""
    out = with_context("cli-a", "配置里 timeout 是 300", "审一下")
    assert out.startswith("# 上一段（cli-a）的结论")
    assert "配置里 timeout 是 300" in out
    assert out.endswith("# 你的任务\n审一下")

    huge = "x" * (HANDOFF_CHARS + 500)
    clipped = with_context("cli-a", huge, "审一下")
    assert "已截断" in clipped
    assert len(clipped) < HANDOFF_CHARS + 400


def test_handoff_is_a_noop_without_a_previous_result():
    """第一段不能塞一个空段落——那会让执行体以为自己漏读了什么。"""
    assert with_context("cli-a", "", "先查") == "先查"
    assert with_context("cli-a", "   \n ", "先查") == "先查"


def test_artifacts_are_listed_from_the_outputs_dir(tmp_path: Path):
    """调用方要的是「你产出了什么」，不是「你说你产出了什么」。"""
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "report.md").write_text("x" * 3200, encoding="utf-8")
    (tmp_path / "outputs" / "nested").mkdir()
    (tmp_path / "outputs" / "nested" / "data.csv").write_text("a,b\n", encoding="utf-8")
    (tmp_path / "outputs" / "ignore-me-dir").mkdir()

    items = collect_artifacts(tmp_path)
    paths = [item["path"] for item in items]
    assert "outputs/report.md" in paths
    assert "outputs/nested/data.csv" in paths
    assert all(item["bytes"] >= 0 for item in items)

    note = format_artifacts(items)
    assert "产物：" in note and "outputs/report.md" in note and "3.1KB" in note


def test_no_outputs_dir_means_no_note(tmp_path: Path):
    """没产物就不该在回喂正文里多一行噪音。"""
    assert collect_artifacts(tmp_path) == []
    assert format_artifacts([]) == ""
