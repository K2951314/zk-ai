"""业务侧巡检探针：三菱官网可用性 + smart-selection 数据新鲜度。

为什么单独一个文件（2026-11-19）：
``event_watch.py`` 原来只看网关自身的 ``/health``，而报告里两条最该自动化的
信号都在网关之外——三菱库存接口（当前 401 fail-closed）与 smart-selection 的
「真源 → 派生」新鲜度（曾静默漂移 5 天没人发现）。新建调度器违背项目既有决策
（不新建编排层），所以这里只做**探针**，由 ``event_watch.py`` 的既有轮询循环
按小时调用；规则点火仍走 ``events.yaml``。

设计约束（沿用采集器的口径）：
- **只读**：不写业务项目的数据文件，不触发重抽/生成器。
- **变化才发**：状态存进 ``event_watch_state.json``，同状态不重复发事件。
  「从坏变好」也要发——运维需要知道什么时候恢复的。
- **拿不到不算异常**：项目没装/服务没起 → 返回 ``None``（这一路静默跳过），
  不能因为没配业务项目就每天报一次「巡检失败」。
- **参数走环境变量**：``SQ_HOME`` / ``SS_HOME``（与技能同一套语义），
  不写死盘符。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

#: 巡检状态在 state 文件里的键前缀
MITSUBISHI_KEY = "mitsubishi_canary"
FRESHNESS_KEY = "smart_selection_freshness"

#: 三菱探针的默认探测型号（与项目金丝雀默认值一致：长期在售、现场确认有货）
DEFAULT_CANARY_MODEL = "WNMG080408-MA"
DEFAULT_CANARY_MATERIAL = "UC5115"

#: 新鲜度巡检超时。``_health.py --gates`` 会把 OSG 页级对账带进来，约 1~2 分钟。
FRESHNESS_TIMEOUT_SECONDS = 240


# --------------------------------------------------------------------------- #
# 项目定位（与 my-skills 技能同一套语义：显式环境变量必须有效）
# --------------------------------------------------------------------------- #
def resolve_sq_home() -> Path | None:
    """智能询价项目根（判据 backend/smart_quotation/mitsubishi_stock.py）。"""
    return _resolve_home(
        "SQ_HOME",
        "backend/smart_quotation/mitsubishi_stock.py",
        ["智能询价"],
    )


def resolve_ss_home() -> Path | None:
    """smart-selection 项目根（判据 mpgen/tools/_health.py）。"""
    return _resolve_home(
        "SS_HOME",
        "mpgen/tools/_health.py",
        ["smart-selection"],
    )


def _resolve_home(env_name: str, marker: str, dir_names: list[str]) -> Path | None:
    """定位项目根。

    与技能脚本的差别：巡检**不应因为环境变量配错就崩掉整个采集器**——
    这里返回 None 并让调用方记一条告警，而不是 ``SystemExit``。
    但环境变量设了却无效必须能被看见，所以调用方拿到 None 时会在事件里注明
    「环境变量无效」，而不是假装「项目没装」。
    """
    env = os.environ.get(env_name, "").strip()
    if env:
        candidate = Path(env)
        return candidate if (candidate / marker).is_file() else None
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [repo_root.parent / name for name in dir_names]
    candidates += [Path.home() / name for name in dir_names]
    for candidate in candidates:
        if (candidate / marker).is_file():
            return candidate
    return None


def env_misconfigured(env_name: str, root: Path | None) -> bool:
    """环境变量设了但定位失败——这跟「没装项目」是两回事，必须分开报。"""
    return bool(os.environ.get(env_name, "").strip()) and root is None


# --------------------------------------------------------------------------- #
# 三菱官网可用性
# --------------------------------------------------------------------------- #
#: 项目自带虚拟环境（相对项目根的路径）。存在就优先——它的依赖契约最准。
_VENV_PYTHONS = (
    Path(".venv") / "Scripts" / "python.exe",   # Windows
    Path(".venv") / "bin" / "python",           # POSIX
)


def _python_for(root: Path) -> list[str]:
    """选解释器命令前缀：项目 venv → 系统 3.13 → 当前解释器。

    三档来自两次实测（2026-11-19）：

    1. 直接用采集器的 ``sys.executable``（ZK-AI 的 venv）会假报——
       ``ModuleNotFoundError: requests``，看起来像「三菱接口坏了」。
    2. 但**也不是有 venv 就一定对**：smart-selection 的 ``_health.py`` 用
       系统 3.13 是 1 条闸门失败，用别的解释器会多报 3 条 —— 它的闸门
       脚本依赖系统 3.13 里的包。用错解释器会把「环境差异」伪装成
       「数据异常」，这是巡检最危险的误报形态。
    3. 所以顺序是：项目 venv（若有）→ 系统 3.13 → 当前解释器。
       结论里带 ``interpreter`` 字段，误报能一眼归因。

    返回**列表**（Windows 的 ``py -3.13`` 是「启动器 + 参数」两个词）。
    """
    for relative in _VENV_PYTHONS:
        candidate = root / relative
        if candidate.is_file():
            return [str(candidate)]
    launcher = shutil.which("py")
    if launcher:
        return [launcher, "-3.13"]
    for name in ("python3.13", "python3"):
        found = shutil.which(name)
        if found:
            return [found]
    return [sys.executable]


def probe_mitsubishi(root: Path | None) -> dict[str, Any] | None:
    """跑一次金丝雀，返回 ``{ok, reason, detail}``；项目不可用返回 None。

    只调 ``ensure_canary()``：它本身就是「用已知型号探测一次真实 RPC」，
    失败即 fail-closed。不在这里查具体型号——巡检要的是链路健康，
    不是某一型号有没有货。
    """
    if root is None:
        return None
    script = (
        "import json, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "from backend.smart_quotation.mitsubishi_stock import get_engine\n"
        "engine = get_engine()\n"
        "ok = engine.ensure_canary()\n"
        "print(json.dumps({'ok': bool(ok)}))\n"
    )
    try:
        # 参数由本模块构造（解释器路径 + 常量脚本），无用户输入拼接
        result = subprocess.run(  # noqa: S603 - 见上
            [*_python_for(root), "-c", script],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=90,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "probe_error",
                "detail": f"{type(exc).__name__}: {exc}"}

    stdout = (result.stdout or "").strip()
    payload: dict[str, Any] | None = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if payload is not None and "ok" in payload:
        payload.setdefault("reason", "" if payload["ok"] else "canary_failed")
        return payload

    stderr = (result.stderr or "").strip()
    reason = "credential_invalid" if ("401" in stderr or "认证" in stderr) else "canary_failed"
    return {
        "ok": False,
        "reason": reason,
        "detail": (stderr[-600:] or f"退出码 {result.returncode}，无输出"),
    }


def mitsubishi_event(
    current: dict[str, Any], previous: dict[str, Any] | None, *, source: str,
) -> dict[str, Any] | None:
    """状态变化才产出事件（坏→好 也要报，运维需要知道恢复时间）。"""
    prev_ok = None if previous is None else bool(previous.get("ok"))
    now_ok = bool(current.get("ok"))
    if prev_ok is not None and prev_ok == now_ok:
        return None
    if now_ok:
        return {
            "source": source,
            "type": "inventory.canary_recovered",
            "object": "mitsubishi-canary",
            "summary": "三菱库存接口自检已恢复（此前告警）",
            "link": "",
            # 恢复事件的指纹带时间桶：避免它与上一次恢复撞键被去重吃掉
            "payload": {"fingerprint": f"mitsubishi|recovered|{_hour_bucket()}"},
        }
    detail = str(current.get("detail") or "")[:500]
    return {
        "source": source,
        "type": "inventory.canary_failed",
        "object": "mitsubishi-canary",
        "summary": "三菱库存接口自检失败：%s（库存查询将 fail-closed）"
                   % (current.get("reason") or "unknown"),
        "link": "",
        "payload": {
            "reason": current.get("reason") or "unknown",
            "detail": detail,
            # 指纹不含 detail：同一种失败不应因为错误消息里的时间戳/序号而重复报
            "fingerprint": "mitsubishi|failed|%s" % (current.get("reason") or "unknown"),
        },
    }


# --------------------------------------------------------------------------- #
# smart-selection 数据新鲜度
# --------------------------------------------------------------------------- #
def probe_freshness(root: Path | None) -> dict[str, Any] | None:
    """跑一次 ``_health.py --gates``，按退出码判定；项目不可用返回 None。

    退出码语义（项目自己定义的）：0 = 数据面/构建边/闸门全绿；1 = 有异常。
    这里**只信退出码**，不解析人类可读输出——输出格式会变，退出码是契约。
    """
    if root is None:
        return None
    script = root / "mpgen" / "tools" / "_health.py"
    try:
        # 参数由本模块构造（解释器路径 + 常量脚本），无用户输入拼接
        result = subprocess.run(  # noqa: S603 - 见上
            [*_python_for(root), str(script), "--gates"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=FRESHNESS_TIMEOUT_SECONDS,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "probe_error",
                "detail": f"{type(exc).__name__}: {exc}"}

    output = (result.stdout or "")
    # 结论行是稳定契约（脚本自己格式化）；取不到就退回退出码。
    conclusion = ""
    for line in reversed(output.splitlines()):
        if line.startswith("结论"):
            conclusion = line.strip()
            break
    return {
        "ok": result.returncode == 0,
        "reason": "" if result.returncode == 0 else "gates_failed",
        "detail": conclusion or (output[-600:].strip()),
        "returncode": result.returncode,
        # 解释器会影响闸门结果（实测同一份数据 1 条 vs 4 条失败）。
        # 把实际用的解释器写进结论，误报能一眼归因到「环境差异」。
        "interpreter": _python_for(root)[0],
    }


def freshness_event(
    current: dict[str, Any], previous: dict[str, Any] | None, *, source: str,
) -> dict[str, Any] | None:
    """状态变化才产出事件。"""
    prev_ok = None if previous is None else bool(previous.get("ok"))
    now_ok = bool(current.get("ok"))
    if prev_ok is not None and prev_ok == now_ok:
        return None
    if now_ok:
        return {
            "source": source,
            "type": "data.freshness_recovered",
            "object": "smart-selection",
            "summary": "smart-selection 数据面与构建闸门已恢复全绿",
            "link": "",
            "payload": {"fingerprint": f"freshness|recovered|{_hour_bucket()}"},
        }
    detail = str(current.get("detail") or "")[:600]
    return {
        "source": source,
        "type": "data.freshness_failed",
        "object": "smart-selection",
        "summary": "smart-selection 数据新鲜度/闸门异常（真源→派生可能已陈旧）",
        "link": "",
        "payload": {
            "reason": current.get("reason") or "gates_failed",
            "detail": detail,
            "returncode": current.get("returncode"),
            # 指纹只取结论摘要里的稳定部分（行数/闸门数），不含时间戳
            "fingerprint": f"freshness|failed|{_stable_fingerprint(detail)}",
        },
    }


def _stable_fingerprint(text: str) -> str:
    """从结论句里抽稳定数字做指纹（「3 个数据面异常、1 条构建滞后」）。"""
    digits = "".join(ch if ch.isdigit() else " " for ch in text)
    parts = [p for p in digits.split() if p]
    return ",".join(parts[:4]) or "unknown"


def _hour_bucket() -> int:
    import time

    return int(time.time() // 3600)
