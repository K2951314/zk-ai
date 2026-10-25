"""发现某个执行体**真正能调**的模型清单。

原则：能用 CLI 自己的口径就用它的，不拿配置文件里的手抄清单冒充全量。
- qoder：``qoder --list-models``（登录后返回账号可用模型，含活动模型）
- codebuddy：没有列模型的子命令，但 ``--help`` 的 ``--model`` 说明里写了
  "Currently supported: (...)"，那份就是它认的清单
- 其它（gemini 等）：返回空，由调用方回落到配置

结果缓存 5 分钟：这几个命令要起 node，20 秒超时那种开销不该每次刷新都付。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time

from app.services.agent.cli.launcher import LauncherError, resolve_command

_TTL = 300.0
_CACHE: dict[str, tuple[float, list[str]]] = {}

#: codebuddy --help 里的模型清单：`Currently supported: (a, b, c)`
_CB_SUPPORTED = re.compile(r"Currently supported:\s*\(([^)]*)\)", re.S)
#: qoder --list-models 的表头
_QODER_HEADER = "MODEL"


def parse_qoder_models(text: str) -> list[str]:
    """``qoder --list-models`` 的输出是「MODEL + 每行一个名字」。"""
    models: list[str] = []
    for line in text.splitlines():
        name = line.strip()
        if not name or name.upper() == _QODER_HEADER:
            continue
        if name.startswith(("-", "Usage:", "Options:")):
            continue
        models.append(name)
    return models


def parse_codebuddy_models(text: str) -> list[str]:
    """从 ``--help`` 的 --model 说明里抠出清单。"""
    match = _CB_SUPPORTED.search(text)
    if not match:
        return []
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def _run(argv: list[str], timeout: float = 25.0) -> str:
    """跑一次 CLI 抓输出；失败一律当"没有清单"，不让面板因此报错。"""
    try:
        argv = resolve_command(argv)
    except LauncherError:
        return ""
    try:
        # argv 来自 cli_tools.yaml 的命令名 + 已解析的入口，不经 shell；
        # 这里只是"列个模型"，不接任何外部输入拼命令。
        proc = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def discover_models(tool: str, command: str) -> list[str]:
    """返回该执行体的可用模型（空列表 = 这条路径没有可信来源）。"""
    key = f"{tool}:{command}"
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < _TTL:
        return list(cached[1])
    if shutil.which(command) is None:
        return []
    if tool == "qoder":
        models = parse_qoder_models(_run([command, "--list-models"]))
    elif tool == "codebuddy":
        models = parse_codebuddy_models(_run([command, "--help"]))
    else:
        models = []
    if models:
        _CACHE[key] = (time.time(), models)
    return models


def clear_cache() -> None:
    """测试用：清掉进程级缓存。"""
    _CACHE.clear()
