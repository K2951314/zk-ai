"""quick_test 的映射表：必须能挑对测试，且宁可保守。

为什么值得测（2026-11-14）：这脚本的全部价值是「改 A 就跑 A 的测试」。
映射写错会让人**以为跑过了其实没跑**——比没有这个脚本更危险。
所以三条：映射指向的测试文件必须真实存在、横切文件必须回落全量、
没映射的改动不许静默通过。
"""

from __future__ import annotations

import sys

import pytest

from app.core.config import PROJECT_ROOT


@pytest.fixture
def qt():
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    try:
        import quick_test  # type: ignore[import-not-found]

        return quick_test
    finally:
        sys.path.pop(0)


def test_every_mapped_test_file_exists(qt) -> None:
    """映射里指向不存在的文件 = 跑了个空——最坏的静默失败。"""
    missing = [
        f"{prefix} -> {name}"
        for prefix, names in qt.MAP.items()
        for name in names
        if not (PROJECT_ROOT / "tests" / name).is_file()
    ]
    assert not missing, f"映射指向不存在的测试文件：{missing}"


@pytest.mark.parametrize(
    "path",
    ["app/main.py", "app/core/container.py", "app/core/errors.py",
     "pyproject.toml", "tests/conftest.py"],
)
def test_cross_cutting_files_always_fall_back_to_full(qt, path) -> None:
    """这些文件一改，任何模块都可能受影响——必须跑全量，不能挑。"""
    picked, why = qt.tests_for([path])
    assert picked == [], f"{path} 不该只挑测试"
    assert "全量" in why


def test_unknown_change_is_conservative(qt) -> None:
    """没登记的路径不许静默挑几个测试糊弄过去。"""
    picked, why = qt.tests_for(["app/some_new_module/nothing.py"])
    assert picked == []
    assert "全量" in why


def test_a_known_change_picks_its_tests(qt) -> None:
    picked, _why = qt.tests_for(["app/routing/scheduler.py"])
    assert picked, "app/routing 应映射到具体测试"
    assert "test_router.py" in picked
    assert all((PROJECT_ROOT / "tests" / n).is_file() for n in picked)


def test_no_changes_means_nothing_to_run(qt) -> None:
    picked, why = qt.tests_for([])
    assert picked == []
    assert "没有改动" in why


def test_every_mapping_prefix_is_a_real_path(qt) -> None:
    """映射的 key 必须对应真实存在的路径（防拼错/防已删目录）。"""
    bad = []
    for prefix in qt.MAP:
        p = PROJECT_ROOT / prefix
        if not (p.exists() or p.with_suffix(".py").exists()):
            bad.append(prefix)
    assert not bad, f"映射里的路径不存在：{bad}"
