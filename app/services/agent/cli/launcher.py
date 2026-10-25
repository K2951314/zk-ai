"""把配置里的命令名解析成**可直接 exec** 的 argv。

为什么需要这一层（2026-10-25 实测踩到）：
``shutil.which("codebuddy")`` 在 Windows 上返回的是 ``codebuddy.cmd``
（npm 生成的 shim），而 ``asyncio.create_subprocess_exec`` 走 CreateProcess，
**不能直接执行 .cmd/.bat**——于是注册表校验通过、真正拉起时却是
``FileNotFoundError: 命令不存在``。qwen / claude / codex 这些 npm 装的工具
全都有这个问题，只是以前没人在 Windows 上真跑过委派。

修法不是退回 ``shell=True``：那会把模型给的任务文本重新交给 cmd 解析，
正好破坏本模块「argv 直传、不经 shell」的安全前提。这里改成读 npm shim
的固定格式，取出它真正要跑的 ``node + 入口脚本``，直接把 argv 交给 node。
解析不出来就明确报错，不静默降级成 shell。
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

#: npm cmd-shim 的固定收尾行形如：
#:   ... & "%_prog%"  "%dp0%\node_modules\<pkg>\<entry>" %*
_ENTRY = re.compile(r'"%dp0%\\([^"]+)"')

#: 内置命令名 -> 解释器的兜底查找
_PWSH = "pwsh"


class LauncherError(RuntimeError):
    """命令存在但无法安全地直接 exec（通常是 shim 格式不认识）。"""


def _npm_entry(shim: Path) -> Path | None:
    """从 .cmd shim 里抠出入口脚本的绝对路径。"""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = _ENTRY.findall(text)
    if not matches:
        return None
    # 同一个 shim 里 dp0 出现多次时，入口是**最后一个**被引号包住的路径
    return shim.parent / matches[-1].replace("\\", "/")


def _node_binary(shim_dir: Path, which=shutil.which) -> str | None:
    """shim 优先用同目录的 node.exe，否则回落到 PATH 里的 node。"""
    sibling = shim_dir / "node.exe"
    if sibling.exists():
        return str(sibling)
    return which("node")


def resolve_command(argv: list[str], which=shutil.which) -> list[str]:
    """返回可安全直接 exec 的 argv（第一个元素是真实可执行文件）。"""
    if not argv:
        return argv
    resolved = which(argv[0]) or argv[0]
    path = Path(resolved)
    suffix = path.suffix.lower()

    if sys.platform == "win32" and suffix in {".cmd", ".bat"}:
        entry = _npm_entry(path)
        node = _node_binary(path.parent, which) if entry else None
        if not entry or not node:
            raise LauncherError(
                f"{argv[0]!r} 解析到批处理 shim {path}，但无法从中取出 node 入口"
                "（或找不到 node）。请改用原生 .exe，或按 npm 的 node_modules 布局检查安装。"
            )
        return [node, str(entry), *argv[1:]]

    if sys.platform == "win32" and suffix == ".ps1":
        # PowerShell 脚本没有 CreateProcess 可用的入口，只能交给解释器；
        # 参数仍按列表直传，不经过任何 shell 拼接。
        pwsh = which(_PWSH) or which("powershell")
        if not pwsh:
            raise LauncherError(f"{argv[0]!r} 是 PowerShell 脚本，但本机找不到 pwsh/powershell")
        return [pwsh, "-NoProfile", "-File", str(path), *argv[1:]]

    return [resolved, *argv[1:]]
