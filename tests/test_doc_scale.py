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
