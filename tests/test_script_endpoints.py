"""独立脚本打的管理端前缀必须是 ``/zkadmin``，不能是 ``/admin``。

2026-11-10 发现的真 bug：``scripts/smoke_test.py``（9 处）与
``scripts/health_check.py``（1 处）打的是 ``/admin/*``，而网关只挂
``/zkadmin``——``/admin`` 在 2026-10-08 就还给智能询价了（见 ``console.js``
的 ``api()`` 注释：``/admin/* 彻底归还智能询价``）。

为什么这个 bug 活了这么久：**控制台前端有 ``api()`` 一层把 ``/admin/``
改写为 ``/zkadmin/``，所以走浏览器永远是对的**；只有独立脚本没有那层改写，
裸着打旧前缀就必然 404。而两个脚本当时都没有测试覆盖，失败了也没人看见。

实测证据（2026-11-10，本机运行中的网关）：
    /admin/credentials  -> 404    /zkadmin/credentials  -> 200
    /admin/health       -> 404    /zkadmin/health       -> 200
    /admin/stats        -> 404    /zkadmin/stats        -> 200

这条守卫扫 ``scripts/`` 下所有 ``.py`` 里的字符串字面量路径，按目录自动发现
（不手写清单——AGENTS.md: 「新增页面/脚本/提示键时，这些守卫按目录或元组
自动覆盖，不要手写清单」）。
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_PY_FILES = tuple(sorted(p for p in _SCRIPTS.glob("*.py") if p.is_file()))
assert _PY_FILES, "scripts/ 下没有任何 .py？路径不对"

#: 匹配**真的会被发出去的**路径字面量：引号（仅 " 和 '，不含反引号——
#: 反引号在 docstring 里是 Markdown 行内代码，属于叙述不是调用）紧跟着 /admin/。
#: 用 `(?<![a-z])` 排除 ``/zkadmin/``。刻意不匹配 f-string 里的 —— 那些路径
#: 如果写错了是别的 bug（例如 health_check.py 的 f"{base_url}/admin/..."）
#: 会被 `_ADMIN_CALL` 抓住。
_ADMIN_PATH = re.compile(r"""["'](/admin/[^"'`\s]*)""")
#: f-string 形态：``f"{base}/admin/xxx"``
_ADMIN_CALL = re.compile(r"""f["'][^"']*?(/admin/[^"'`$\s]*)""")


def test_no_script_calls_the_retired_admin_prefix() -> None:
    """任何脚本的字符串字面量里都不许出现 ``/admin/`` 路径。"""
    offenders: list[str] = []
    for path in _PY_FILES:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in (_ADMIN_PATH, _ADMIN_CALL):
                match = pattern.search(line)
                if match:
                    offenders.append(f"{path.name}:{lineno} -> {match.group(1)}")
    assert not offenders, (
        "这些脚本在打已下线的 /admin/* （网关只挂 /zkadmin，实测 404）：\n  "
        + "\n  ".join(offenders)
        + "\n改成 /zkadmin/... 即可。控制台前端的 api() 会自动改写前缀，"
        "独立脚本没有那层改写。"
    )


def test_the_retired_prefix_really_is_retired() -> None:
    """反向确认：``/zkadmin`` 才是现役前缀，且网关里没有 ``/admin`` 别名。

    这条不测脚本，测的是「为什么上一条成立」。如果哪天有人给网关加了个
    ``/admin`` → ``/zkadmin`` 的别名，上一条守卫就变成教条，这条会提醒他
    把两条一起改。
    """
    root = Path(__file__).resolve().parent.parent
    main_src = (root / "app" / "main.py").read_text(encoding="utf-8")
    common_src = (root / "app" / "api" / "admin" / "_common.py").read_text(encoding="utf-8")
    assert 'prefix="/zkadmin"' in common_src, "admin 面的前缀不是 /zkadmin 了，守卫需要同步"
    assert '"/admin"' not in main_src, "main.py 里出现了 /admin 挂载，别名回来了？"
    assert "include_router(admin.router)" in main_src
