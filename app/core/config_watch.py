"""Watch the YAML config files and reload when the operator edits them.

Why this exists (2026-09-29, the third adversarial review): the gateway reads
its configuration once at startup and keeps it in memory. Two consequences that
both cost real hours:

1. An edit to ``config/*.yaml`` does nothing until someone reloads - and nothing
   on screen says the running config is stale.
2. Worse, the console renders its forms from **memory** and writes those values
   back on save, so a console save silently reverts the operator's file edit.
   That exact bug ate two changes in one day on 2026-09-28/29 (see the
   "external config edits get silently rolled back" entry in AGENTS.md).

Auto-reload removes the root cause instead of the symptom: memory and disk stop
being able to disagree. Combined with the existing stale-write gate
(:class:`app.core.config_writer.ConfigStaleError`) the console now refuses to
clobber an edit *and* the edit takes effect on its own.

Design decisions, stated because they are the ones that could have gone another
way:

* **Polling, not ``watchdog``/inotify.** Adding a dependency for this is a bad
  trade: the watcher has to survive a read-only mount, an SMB share and a
  container with ``inotify`` limits, and a 2 second poll costs one ``stat`` per
  file. ``ZKAI_WATCH_CONFIG_INTERVAL`` tunes it.
* **Debounced on (mtime, size), not just mtime.** An editor that saves by
  truncate-then-write produces a transient state where a half-written YAML would
  be loaded and *rejected*, logging a scary error for a file that is fine one
  tick later. So a change is only acted on once the signature stops moving
  (``settle`` seconds).
* **A bad file never takes the gateway down.** Reload failure keeps the previous
  config, records the error, and is retried on the next change - the operator
  finishes fixing the file and it just starts working.
* **Our own writes are not special-cased.** After a console save the file's
  signature changes, the watcher reloads, and the reload refreshes the signature
  - so it fires exactly once. Trying to be cleverer (ignoring writes we made)
  is what leaves memory and disk different.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger("config_watch")

#: What a reload callback receives: the paths that changed, and returns nothing.
ChangeHandler = Callable[[list[Path]], Awaitable[None]]


def _signature(paths: Iterable[Path]) -> dict[Path, tuple[int, int]]:
    """``{path: (mtime_ns, size)}`` for the files that exist.

    ``mtime_ns`` because a fast editor can write twice inside one millisecond,
    and ``size`` because a same-length rewrite inside the same tick is otherwise
    invisible.
    """
    out: dict[Path, tuple[int, int]] = {}
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        out[path] = (stat.st_mtime_ns, stat.st_size)
    return out


@dataclass(slots=True)
class ConfigWatcher:
    """Poll a fixed set of files and fire a handler when they settle.

    ``poll_once`` is public and side-effect-free apart from the callback, so
    tests can drive the whole state machine without sleeping.
    """

    paths: list[Path]
    on_change: ChangeHandler
    interval: float = 2.0
    #: 最短「稳定时长」：签名至少连续不变这么久才认它是一次改完的编辑。
    #: 按时钟判而不是按 tick 数，因为 interval 可以被设得比 settle 小
    #: （有人就想 0.2s 一poll）；按 tick 判会让这个字段变成装饰品。
    settle: float = 0.4
    #: Injected so tests can use a fake clock; production uses ``time.monotonic``.
    clock: Callable[[], float] = time.monotonic

    _seen: dict[Path, tuple[int, int]] = field(default_factory=dict, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _pending: dict[Path, tuple[int, int]] = field(default_factory=dict, init=False)
    _pending_since: float = field(default=0.0, init=False)
    _reload_count: int = field(default=0, init=False)
    _last_reload_at: float = field(default=0.0, init=False)
    _last_check_at: float = field(default=0.0, init=False)
    _last_error: str = field(default="", init=False)
    _running: bool = field(default=False, init=False)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Begin polling in the background (idempotent)."""
        if self._running:
            return
        self._running = True
        # Seed the signature *now*, without firing: a restart must not reload
        # just because the files exist.
        self._seen = _signature(self.paths)
        self._task = asyncio.create_task(self._run(), name="zkai-config-watch")
        logger.info(
            "配置监听已启动：%d 个文件，每 %.1fs 一次（编辑 YAML 后自动生效，不用再 reload）",
            len(self._seen),
            self.interval,
        )

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while self._running:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                # A watcher that dies silently is worse than one that logs: the
                # operator would believe auto-reload is working.
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("配置监听轮询失败（监听仍在继续）")
            await asyncio.sleep(self.interval)

    # ------------------------------------------------------------------ #
    # the state machine
    # ------------------------------------------------------------------ #
    async def poll_once(self) -> list[Path]:
        """One poll. Returns the paths that were reloaded (usually empty).

        Three states, in order:

        1. nothing changed since last time -> nothing to do;
        2. changed, but this is the first sighting -> remember it and its
           timestamp, do nothing;
        3. changed and already remembered -> reload only if the signature has
           stopped moving (``settle`` seconds) *and* is still different from what
           we last loaded.

        State 3's second half is what makes a mid-write file harmless: a
        truncate-then-write editor keeps moving the signature, so we never load
        the half-written version.
        """
        self._last_check_at = self.clock()
        current = _signature(self.paths)

        if current == self._seen:
            self._pending.clear()
            self._pending_since = 0.0
            return []

        if self._pending != current:
            # New or still-changing signature: (re)start the settle window.
            self._pending = current
            self._pending_since = self.clock()
            return []

        if self.clock() - self._pending_since < self.settle:
            return []  # 还在 settle 窗口内，等下一次 poll

        changed = sorted(
            path for path, sig in self._pending.items() if self._seen.get(path) != sig
        )
        self._seen = dict(self._pending)
        self._pending.clear()
        self._pending_since = 0.0
        if not changed:
            return []
        return changed if await self._fire(changed) else []

    async def _fire(self, changed: list[Path]) -> bool:
        """Run the handler. Returns True when the reload actually happened.

        ``poll_once`` reports the paths it *reloaded*, so a failed reload must
        come back as "nothing reloaded" - otherwise callers (and the log line
        the operator reads) would claim an edit took effect when it did not.
        """
        names = "、".join(p.name for p in changed)
        try:
            await self.on_change(changed)
        except Exception as exc:
            # The old config stays in place; the operator fixes the file and the
            # next change picks it up. Never let a bad edit break the gateway.
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "配置文件 %s 变了，但重新加载失败（已保留上一份配置）：%s", names, exc
            )
            return False
        self._reload_count += 1
        self._last_reload_at = self.clock()
        self._last_error = ""
        logger.info("配置文件 %s 有改动，已自动 reload", names)
        return True

    # ------------------------------------------------------------------ #
    def reseed(self) -> None:
        """Adopt the current files as the new baseline and drop any pending edit.

        Called after a reload (or an external write we performed ourselves):
        without it the next poll would see the same difference again and reload
        the same change forever, once per interval.
        """
        self._seen = _signature(self.paths)
        self._pending.clear()
        self._pending_since = 0.0

    def status(self) -> dict[str, Any]:
        """For ``/health`` and the console: is the watcher alive, and what did it do."""
        return {
            "enabled": self._running,
            "interval_seconds": round(self.interval, 2),
            "watching": [p.name for p in self.paths],
            "reload_count": self._reload_count,
            "last_reload_seconds_ago": (
                round(self.clock() - self._last_reload_at, 1)
                if self._last_reload_at
                else None
            ),
            # Non-empty means "your last edit did NOT take effect, and here is why".
            "last_error": self._last_error or None,
        }


def watch_paths(config_dir: Path, source_files: dict[str, str]) -> list[Path]:
    """The files to watch: exactly the ones :func:`load_app_config` actually read.

    Deliberately not ``glob("*.yaml")``: the example templates are not loaded,
    so editing one must not trigger a reload, and a stray ``.bak`` in the
    directory is not a config either.
    """
    out: list[Path] = []
    for name in dict.fromkeys(source_files.values()):
        candidate = Path(name)
        path = candidate if candidate.is_absolute() else config_dir / name
        if path.is_file() and path not in out:
            out.append(path)
    return out
