"""文档里的规模数字必须和代码对得上。

这个项目的文档已经因此撒过三次谎：README 的测试数停在 `568`（实际 753）、
停在 `753`（实际 774），行数停在 `15,400`（实际 20,746）。数字本身不重要，
重要的是**下一个人会信它**——AGENTS.md 里有一条完整的教训就是"我写下了规则，
下一轮自己没执行"。

所以这里不靠自觉：把 README 声称的用例数拿去和 pytest 真收出来的数比。
改测试不改文档的人会在这里撞上，消息直接告诉他改哪儿。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT

_README = Path(PROJECT_ROOT) / "README.md"
#: README 里形如「774 个用例」的声称。
_CLAIM_RE = re.compile(r"(\d+)\s*个用例")


def _collected_count() -> int:
    """pytest 实际能收集到多少个用例（子进程，避免在自己身上递归）。

    不用 `-q`：pytest 9 在 `--collect-only -q` 下不再打印那行汇总，
    而非 `-q` 的最后一行就是「N tests collected」。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=300,
    )
    match = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout + proc.stderr)
    assert match, f"读不到 pytest 的收集数：\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    return int(match.group(1))


def test_readme_test_count_matches_reality() -> None:
    """README 说的用例数 = pytest 真收到的用例数。

    故意多花约 4 秒跑一个子进程：这是唯一能抓住"文档悄悄过期"的办法。
    """
    if not _README.is_file():  # pragma: no cover - README 是仓库自带的
        pytest.skip("no bundled README.md")

    actual = _collected_count()
    claims = sorted({int(n) for n in _CLAIM_RE.findall(_README.read_text(encoding="utf-8"))})

    assert claims, "README 里找不到「N 个用例」的声称——是文案被改掉了，还是刚删干净？"
    assert claims == [actual], (
        f"README 声称 {claims} 个用例，pytest 实际收集到 {actual} 个。"
        f"请把 README 里的「{claims[0]} 个用例」改成「{actual} 个用例」"
        "（测试树说明与 §18 两处都要改；行数同理，用 "
        "find app tests scripts -name '*.py' | xargs wc -l 数，不要估）"
    )


# --------------------------------------------------------------------------- #
# AGENTS.md 的载入预算（2026-11-14）
# --------------------------------------------------------------------------- #
#: 规则文件的载入预算。AGENTS.md 自己写着「超 65,536 字节预算要拆」，
#: 但这条规则一直靠人记 —— 2026-11-14 实测它已经 64,674 字节（余 862），
#: 而项目史上它超到过 74,947。**规则写进文档 ≠ 会执行，要工具化。**
AGENTS_BUDGET = 65_536


def test_agents_md_stays_within_its_load_budget() -> None:
    path = Path(PROJECT_ROOT) / "AGENTS.md"
    if not path.is_file():  # pragma: no cover - 仓库自带
        pytest.skip("no AGENTS.md")
    size = len(path.read_bytes())
    assert size <= AGENTS_BUDGET, (
        f"AGENTS.md {size} 字节，超过 {AGENTS_BUDGET} 的载入预算 {size - AGENTS_BUDGET} 字节。\n"
        "按文件自己的规矩：把「逐项排除过程」拆到 docs/ 下（结论与索引留在原处），"
        "见 docs/排查过程详述.md 的做法。别删规则本身。"
    )


def test_agents_warns_before_it_hits_the_wall() -> None:
    """余量 < 2KB 就报，别等到撞墙才发现（给下一次会话留出拆分的时间）。"""
    path = Path(PROJECT_ROOT) / "AGENTS.md"
    if not path.is_file():  # pragma: no cover
        pytest.skip("no AGENTS.md")
    size = len(path.read_bytes())
    slack = AGENTS_BUDGET - size
    assert slack > 2_048, (
        f"AGENTS.md 余量只剩 {slack} 字节（{size}/{AGENTS_BUDGET}）。"
        "下次往里加内容前先拆分：过程进 docs/，结论留原处。"
    )
