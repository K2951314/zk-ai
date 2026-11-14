#!/usr/bin/env python3
"""按改动范围挑测试：改哪块就跑哪块的测试，而不是每次全量 166 秒。

为什么需要（2026-11-14）：全量测试实测 **166 秒**，而单个测试平均 0.1 秒——
时间全花在「跑 1,436 个」而不是「某个很慢」。改一行 `app/routing/` 的代码却
跑一遍 `tests/test_agent.py` 是纯浪费。这个脚本把「改了什么 → 该跑什么」
变成确定的映射，让迭代周期从 3 分钟降到 10-30 秒。

用法::

    python scripts/quick_test.py                 # 按 git diff 自动挑
    python scripts/quick_test.py app/routing/    # 指定范围
    python scripts/quick_test.py --all           # 老实跑全量（提交前用）

**它不是门禁的替代品**：提交前仍要跑 `ruff + mypy + pytest`。
这是迭代期的加速器。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 源码路径前缀 → 相关测试文件（相对 tests/）。
#: 只登记「有明确对应关系」的；找不到映射的改动回落到全量（保守优先）。
MAP: dict[str, tuple[str, ...]] = {
    "app/routing/": ("test_router.py", "test_retry.py", "test_limits.py",
                     "test_scheduler_last_leg.py", "test_throttle_scopes.py"),
    "app/credentials/": ("test_key_pool.py", "test_limits.py", "test_retry.py",
                         "test_throttle_scopes.py"),
    "app/providers/": ("test_adapters.py", "test_discovery.py"),
    "app/retry/": ("test_retry.py", "test_router.py"),
    "app/api/chat.py": ("test_api.py", "test_stream_duration.py"),
    "app/api/messages.py": ("test_messages_api.py",),
    "app/api/responses.py": ("test_responses_api.py",),
    "app/api/admin/": ("test_config_write_through.py", "test_api.py"),
    "app/api/agent.py": ("test_agent.py", "test_ui_pages.py"),
    "app/services/agent/": ("test_agent.py", "test_agent_cli.py", "test_agent_resume.py",
                            "test_agent_prompting.py", "test_agent_taskspace.py",
                            "test_agent_usage_ledger.py", "test_agent_auto.py"),
    "app/services/agent/cli/": ("test_agent_cli.py", "test_agent_resume.py",
                                "test_agent_prompting.py"),
    "app/services/burner_service/": ("test_burn_budget.py", "test_burner_key_single_source.py"),
    "app/services/events/": ("test_events.py",),
    "app/services/request_service.py": ("test_api.py", "test_messages_api.py",
                                        "test_responses_api.py"),
    "app/services/history.py": ("test_history.py",),
    "app/core/config/": ("test_config.py", "test_config_watch.py"),
    "app/core/config_watch.py": ("test_config_watch.py",),
    "app/core/config_writer.py": ("test_config.py", "test_config_write_through.py"),
    "app/database/": ("test_stream_duration.py", "test_api.py"),
    "app/models/": ("test_token_estimator.py", "test_router.py"),
    "scripts/": ("test_start_msg.py", "test_check_config_events.py",
                 "test_burner_key_single_source.py"),
    "app/web/": ("test_console_zh.py", "test_ui_pages.py"),
    "config/": ("test_config.py", "test_burner_key_single_source.py"),
}

#: 改这些文件必须跑全量（横切关注点，任何模块都可能受影响）。
ALWAYS_FULL = (
    "app/main.py",
    "app/core/container.py",
    "app/core/errors.py",
    "pyproject.toml",
    "tests/conftest.py",
)


def changed_files() -> list[str]:
    """相对 HEAD 的改动（含未跟踪的新文件）。"""
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,  # noqa: S607
        text=True, encoding="utf-8", errors="replace",
    ).stdout
    files: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:  # 重命名
            path = path.split(" -> ", 1)[1]
        files.append(path.strip('"'))
    return files


def tests_for(files: list[str]) -> tuple[list[str], str]:
    """把改动文件映射成测试文件；返回 (测试列表, 说明)。"""
    if not files:
        return [], "没有改动"

    for f in files:
        if f in ALWAYS_FULL:
            return [], f"{f} 是横切依赖，跑全量"

    picked: set[str] = set()
    unmatched: list[str] = []
    for f in files:
        hit = None
        for prefix, targets in MAP.items():
            if f.startswith(prefix) or f == prefix:
                hit = targets
                break
        if hit is None:
            unmatched.append(f)
        else:
            picked.update(hit)

    existing = [t for t in sorted(picked) if (ROOT / "tests" / t).is_file()]
    if unmatched or not existing:
        return [], f"有改动无明确映射（{', '.join(unmatched[:3]) or '空'}），保守跑全量"
    return existing, f"按映射选中 {len(existing)} 个测试文件"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按改动范围挑测试")
    parser.add_argument("paths", nargs="*", help="指定路径前缀（省略则按 git diff 自动挑）")
    parser.add_argument("--all", action="store_true", help="跑全量（提交前）")
    parser.add_argument("--dry-run", action="store_true", help="只打印会跑哪些")
    args = parser.parse_args(argv)

    if args.all:
        picked, why = [], "显式要求全量"
    elif args.paths:
        cross = [p for p in args.paths if p in ALWAYS_FULL]
        if cross:
            picked, why = [], f"{cross[0]} 是横切依赖，跑全量"
        else:
            picked_set: set[str] = set()
            for p in args.paths:
                for prefix, targets in MAP.items():
                    if p.startswith(prefix) or prefix.startswith(p):
                        picked_set.update(targets)
            picked = [t for t in sorted(picked_set) if (ROOT / "tests" / t).is_file()]
            why = f"按指定路径选中 {len(picked)} 个测试文件"
            if not picked:
                picked, why = [], "指定路径无映射，跑全量"
    else:
        picked, why = tests_for(changed_files())

    print(f"[quick_test] {why}")

    if args.dry_run:
        for t in picked:
            print(f"  tests/{t}")
        return 0

    cmd = [sys.executable, "-m", "pytest", "-q", "--no-header"]
    cmd += [f"tests/{t}" for t in picked] if picked else []
    print(f"[quick_test] {' '.join(cmd[:4])} …（{len(picked) or '全部'} 个文件）")
    return subprocess.call(cmd, cwd=ROOT)  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
