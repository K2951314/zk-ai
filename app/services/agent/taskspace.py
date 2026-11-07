"""任务空间：一次委派开一个独立目录，所有执行体在里面工作。

为什么需要它（2026-11-07）：
- 多个 AI 工具同时改同一个工作区会互相覆盖；任务空间把每次委派隔离开。
- 免费积分模型随时可能中断；只要产物和进度落在 ``state.json`` 里，
  换一个执行体就能接着干（见 docs/总控大脑-能力矩阵与差距分析-20261105.md 附录 F.6）。
- 统一权限的边界靠它落地：``bypass`` 只在任务空间目录内生效，主工作区仍然只读。

目录结构::

    <workspace>/.taskspaces/<task_id>/
        inputs/   放任务输入
        work/     执行体的临时工作区
        outputs/  最终产物（验收只看这里）
        logs/     执行日志
        state.json  进度、产物、交接说明
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 任务空间在 workspace 下的固定目录名。
DIRNAME = ".taskspaces"
STATE_FILE = "state.json"
SUBDIRS = ("inputs", "work", "outputs", "logs")


@dataclass(frozen=True, slots=True)
class TaskSpace:
    """一个已创建的任务空间。"""

    task_id: str
    path: Path

    @property
    def state_path(self) -> Path:
        return self.path / STATE_FILE


def root_for(workspace: Path) -> Path:
    """任务空间的根目录（可能还不存在）。"""
    return Path(workspace) / DIRNAME


def _new_id(session_id: str) -> str:
    """时间戳 + 会话短号 + 随机后缀：可读、可排序、不撞车。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    short = (session_id or "anon").replace("-", "")[:8] or "anon"
    return f"{stamp}-{short}-{uuid.uuid4().hex[:4]}"


def _write(path: Path, data: dict[str, Any]) -> None:
    """原子写 state.json，避免半截文件让接管逻辑读崩。"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def create(
    workspace: Path,
    *,
    session_id: str,
    goal: str,
    task_id: str | None = None,
) -> TaskSpace:
    """建一个任务空间（幂等：同 id 已存在时只补齐缺失目录）。"""
    identifier = task_id or _new_id(session_id)
    path = root_for(workspace) / identifier
    for sub in SUBDIRS:
        (path / sub).mkdir(parents=True, exist_ok=True)
    space = TaskSpace(task_id=identifier, path=path)
    if not space.state_path.exists():
        now = time.time()
        _write(
            space.state_path,
            {
                "task_id": identifier,
                "session_id": session_id,
                "goal": goal.strip()[:500],
                "created_at": now,
                "updated_at": now,
                "steps": [],
                "artifacts": [],
                "handoff_brief": "",
            },
        )
    return space


def load(workspace: Path, task_id: str) -> dict[str, Any] | None:
    """读一个任务空间的进度；不存在或坏文件返回 None。"""
    path = root_for(workspace) / task_id / STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def update(workspace: Path, task_id: str, **fields: Any) -> dict[str, Any] | None:
    """合并更新进度字段，并刷新 ``updated_at``。"""
    data = load(workspace, task_id)
    if data is None:
        return None
    data.update(fields)
    data["updated_at"] = time.time()
    _write(root_for(workspace) / task_id / STATE_FILE, data)
    return data


def list_all(workspace: Path, limit: int = 50) -> list[dict[str, Any]]:
    """列出任务空间摘要，按创建时间倒序。坏文件跳过，不让一个坏目录拖垮列表。"""
    root = root_for(workspace)
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in root.glob(f"*/{STATE_FILE}"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        items.append(
            {
                "task_id": str(data.get("task_id") or path.parent.name),
                "session_id": str(data.get("session_id") or ""),
                "goal": str(data.get("goal") or ""),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "steps": len(data.get("steps") or []),
                "artifacts": len(data.get("artifacts") or []),
                "lease": data.get("lease") if isinstance(data.get("lease"), dict) else None,
            }
        )
    items.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    return items[:limit]


def is_inside(workspace: Path, candidate: Path) -> bool:
    """``candidate`` 是否落在本工作区的任务空间根之内（统一权限的边界判断）。"""
    try:
        return Path(candidate).resolve().is_relative_to(root_for(workspace).resolve())
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# 租约：同一个任务空间同一时刻只允许一个写者（防止多个执行体互相覆盖）
# --------------------------------------------------------------------------- #
#: 锁文件名；用 O_CREAT|O_EXCL 原子创建，跨进程也安全。
LOCK_FILE = "state.lock"
#: 默认租约时长（秒）。执行体最长 1800s，留一点余量。
DEFAULT_LEASE_TTL = 1_900.0


def acquire(
    workspace: Path,
    task_id: str,
    *,
    owner: str,
    ttl: float = DEFAULT_LEASE_TTL,
) -> tuple[bool, str]:
    """尝试占用任务空间。返回 ``(是否拿到, 当前占用者)``。

    过期租约会被抢占；拿不到时把占用者告诉调用方，由它给用户一句中文说明。
    """
    lock = root_for(workspace) / task_id / LOCK_FILE
    now = time.time()
    fd = _try_create(lock)
    if fd is None:
        holder, expires = _read_lock(lock, now)
        if expires > now:
            if holder == owner:
                return True, owner  # 同一个执行体重入，视为已持有
            return False, holder or "另一个执行体"
        # 过期租约：清掉再抢一次（仍可能被别的进程抢先，失败就如实返回）
        with contextlib.suppress(OSError):
            lock.unlink()
        fd = _try_create(lock)
        if fd is None:
            return False, holder or "另一个执行体"
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"owner": owner, "expires_at": now + ttl},
                ensure_ascii=False,
            )
        )
    update(
        workspace,
        task_id,
        lease={"owner": owner, "acquired_at": now, "expires_at": now + ttl},
    )
    return True, owner


def _try_create(path: Path) -> int | None:
    """原子创建一个锁文件；已存在返回 None。"""
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    except OSError:
        return None


def _read_lock(lock: Path, now: float) -> tuple[str, float]:
    """读锁里的持有者与过期时间；坏文件按「无主且已过期」处理。"""
    del now  # 保留参数便于以后做时钟偏移校正
    try:
        raw = lock.read_text(encoding="utf-8")
    except OSError:
        return "", 0.0
    try:
        meta = json.loads(raw)
    except ValueError:
        # 兼容早期纯文本锁：按文件时间 + 默认租约推断
        with contextlib.suppress(OSError):
            return raw.strip(), lock.stat().st_mtime + DEFAULT_LEASE_TTL
        return raw.strip(), 0.0
    if not isinstance(meta, dict):
        return "", 0.0
    holder = str(meta.get("owner") or "")
    try:
        expires = float(meta.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        expires = 0.0
    return holder, expires


def release(workspace: Path, task_id: str, *, owner: str) -> None:
    """释放租约；只有持有者能释放，避免误删别人的锁。"""
    lock = root_for(workspace) / task_id / LOCK_FILE
    holder, _ = _read_lock(lock, time.time())
    if holder == owner:
        with contextlib.suppress(OSError):
            lock.unlink()
    data = load(workspace, task_id)
    lease = data.get("lease") if isinstance(data, dict) else None
    if isinstance(lease, dict) and lease.get("owner") == owner:
        update(workspace, task_id, lease=None)


def force_release(workspace: Path, task_id: str) -> str:
    """人工干预：无条件清掉租约，返回原来的占用者（没有则空串）。

    只给控制台的「释放占用」按钮用；正常路径一律走 ``release``（校验持有者）。
    用于执行体崩溃、租约还没到期但人确认它已经死掉的情况。
    """
    lock = root_for(workspace) / task_id / LOCK_FILE
    holder, _ = _read_lock(lock, time.time())
    with contextlib.suppress(OSError):
        lock.unlink()
    data = load(workspace, task_id)
    lease = data.get("lease") if isinstance(data, dict) else None
    if isinstance(lease, dict) and lease.get("owner"):
        holder = holder or str(lease["owner"])
        update(workspace, task_id, lease=None)
    return holder
