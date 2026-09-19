"""ZK-Agent tool belt: the six workspace tools the model may call.

Security model:

* every path is resolved and must stay inside the workspace tree (``..``,
  absolute paths outside, symlinks that resolve outside -> refused);
* ``read_file`` / ``list_dir`` / ``search_files`` / ``grep`` are read-only and
  run immediately;
* ``write_file`` / ``run_command`` go through a two-phase protocol:
  :meth:`ToolBox.prepare` renders the diff / command preview (and enforces the
  command blocklist), the loop asks the user, and only an approval leads to
  :meth:`ToolBox.perform` actually executing.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 工具输出统一上限（回喂模型的文本），防止一次 cat 大文件撑爆上下文
_MAX_OUTPUT = 8_000
_MAX_READ_BYTES = 48_000
_MAX_LIST_ENTRIES = 500
_MAX_SEARCH_HITS = 200
_MAX_GREP_HITS = 100
_MAX_GREP_FILES = 4_000
_MAX_PREVIEW = 16_000
#: run_command 的超时上限（模型可以要求更短，不能更长）
_MAX_COMMAND_TIMEOUT = 120.0

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "data", "dist", "build",
}
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".7z",
    ".rar", ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".mov", ".avi", ".sqlite", ".db", ".pyc", ".class", ".jar",
}

#: run_command 硬黑名单：就算用户手滑点了批准也不执行（匹配即拒，回给模型）
_COMMAND_BLOCKLIST: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\brm\s[^;&|]*-[a-z]*r",            # rm -r / -rf / -fr
        r"\b(rd|rmdir|del)\s+/s",            # Windows 递归删除
        r"\bformat\s+[a-z]:",
        r"\bdiskpart\b",
        r"\bshutdown\b",
        r"\block\b",
        r"\breg(\.exe)?\s+delete\b",
        r"remove-item\s[^;&|]*-recurse",
        r"\bmkfs\b",
        r"\bdd\s+if=",
        r"\bchmod\s+-R\s+777\b",
        r"\btaskkill\s+/f\b",                # 可能杀掉网关自己
        r"\bgit\s+push\b",                   # git 写操作一律走人工
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+clean\b",
        r"\bgit\s+branch\s+-d\b",
        r"\bdrop\s+(table|database)\b",
    )
)


class ToolError(Exception):
    """A user-visible tool failure; the message goes back to the model."""


@dataclass(slots=True)
class ToolOutcome:
    """Result of one tool execution (or of preparing one)."""

    ok: bool = True
    #: text fed back to the model as the tool message
    output: str = ""
    #: extra UI payload (unified diff / command line) shown on the console
    display: dict[str, Any] | None = None
    #: True -> needs user approval before ``perform`` may run
    needs_approval: bool = False


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内一个文本文件的内容片段，输出带行号（从 1 开始）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的文件路径"},
                    "offset": {"type": "integer", "description": "起始行号，默认 1"},
                    "limit": {"type": "integer", "description": "读取行数，默认 400，最大 1000"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出一个目录下的文件与子目录（标注类型和大小）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的目录路径，默认 ."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "按 glob 模式递归查找文件（如 **/*.py），返回相对路径列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，如 scripts/*.py"},
                    "path": {"type": "string", "description": "起始目录，默认 ."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "在工作区文本文件里按内容搜索，返回 路径:行号: 内容 列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "要搜索的文本或正则"},
                    "path": {"type": "string", "description": "起始目录或单个文件，默认 ."},
                    "regex": {"type": "boolean", "description": "true=按正则解释，默认 false"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入/追加文件（需要用户批准）。默认 overwrite 会展示与旧内容的 diff。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的文件路径"},
                    "content": {"type": "string", "description": "要写入的完整内容"},
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "overwrite=整文件覆写（默认），append=尾部追加",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在工作区目录执行一条 shell 命令（需要用户批准；破坏性命令会被硬拦截）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout": {"type": "integer", "description": "超时秒数（默认 60，最大 120）"},
                },
                "required": ["command"],
            },
        },
    },
]

SAFE_TOOLS = frozenset({"read_file", "list_dir", "search_files", "grep"})
DANGEROUS_TOOLS = frozenset({"write_file", "run_command"})


def _cap(text: str, limit: int = _MAX_OUTPUT, *, note: str = "…(输出已截断)") -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n{note}（{len(text)} -> {limit} 字符）"


class ToolBox:
    """Workspace-scoped tool implementations for one agent session."""

    def __init__(self, workspace: Path, *, command_timeout: float = 60.0) -> None:
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.command_timeout = command_timeout

    # ------------------------------------------------------------------ #
    # Path guard
    # ------------------------------------------------------------------ #
    def resolve(self, raw: str) -> Path:
        """Resolve ``raw`` inside the workspace; anything escaping is refused."""
        if not raw or not raw.strip():
            raise ToolError("路径为空")
        candidate = Path(raw.strip())
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ToolError(f"路径无法解析 {raw!r}: {exc}") from exc
        if not self._contains(resolved):
            raise ToolError(
                f"路径越出工作区被拒绝：{raw!r} -> {resolved}（工作区 {self.workspace}）"
            )
        return resolved

    def _contains(self, resolved: Path) -> bool:
        try:
            resolved.relative_to(self.workspace)
            return True
        except ValueError:
            pass
        # Windows 大小写/短路径兜底
        root = os.path.normcase(str(self.workspace))
        child = os.path.normcase(str(resolved))
        return child == root or child.startswith(root + os.sep)

    @staticmethod
    def _skip(path: Path) -> bool:
        return bool(set(path.parts) & _SKIP_DIRS) or path.suffix.lower() in _BINARY_EXTS

    # ------------------------------------------------------------------ #
    # Two-phase protocol for dangerous tools
    # ------------------------------------------------------------------ #
    def prepare(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Validate + render the approval preview WITHOUT executing."""
        if name == "write_file":
            return self._prepare_write(args)
        if name == "run_command":
            return self._prepare_command(args)
        raise ToolError(f"未知工具 {name!r}")

    async def perform(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Execute a tool. For dangerous tools only call after approval."""
        if name == "read_file":
            return self._read_file(args)
        if name == "list_dir":
            return self._list_dir(args)
        if name == "search_files":
            return await asyncio.to_thread(self._search_files, args)
        if name == "grep":
            return await asyncio.to_thread(self._grep, args)
        if name == "write_file":
            return await asyncio.to_thread(self._write_file, args)
        if name == "run_command":
            return await self._run_command(args)
        raise ToolError(f"未知工具 {name!r}")

    # ------------------------------------------------------------------ #
    # Read-only tools
    # ------------------------------------------------------------------ #
    def _read_file(self, args: dict[str, Any]) -> ToolOutcome:
        path = self.resolve(str(args.get("path", "")))
        if not path.exists():
            raise ToolError(f"文件不存在：{path}")
        if path.is_dir():
            raise ToolError(f"{path} 是目录，请用 list_dir")
        raw = path.read_bytes()
        if b"\0" in raw[:8192] or path.suffix.lower() in _BINARY_EXTS:
            raise ToolError(f"{path.name} 疑似二进制文件，不支持读取")
        text = raw[: _MAX_READ_BYTES * 4].decode("utf-8", errors="replace")
        lines = text.splitlines()
        offset = max(1, _as_int(args.get("offset"), 1))
        limit = min(1000, max(1, _as_int(args.get("limit"), 400)))
        window = lines[offset - 1 : offset - 1 + limit]
        body = "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(window, start=offset))
        truncated = len(lines) > offset - 1 + limit or len(raw) > _MAX_READ_BYTES * 4
        shown = f"{offset}~{offset - 1 + len(window)}"
        note = f"\n…（共 {len(lines)} 行，仅显示 {shown} 行）" if truncated else ""
        return ToolOutcome(output=_cap(body + note))

    def _list_dir(self, args: dict[str, Any]) -> ToolOutcome:
        path = self.resolve(str(args.get("path") or "."))
        if not path.is_dir():
            raise ToolError(f"不是目录：{path}")
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            raise ToolError(f"目录读取失败：{exc}") from exc
        lines = []
        for entry in entries[:_MAX_LIST_ENTRIES]:
            if entry.is_dir():
                lines.append(f"dir   {entry.name}/")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = -1
                lines.append(f"file  {entry.name} ({size} B)")
        if len(entries) > _MAX_LIST_ENTRIES:
            lines.append(f"…（其余 {len(entries) - _MAX_LIST_ENTRIES} 项省略）")
        return ToolOutcome(output="\n".join(lines) or "(空目录)")

    def _search_files(self, args: dict[str, Any]) -> ToolOutcome:
        pattern = str(args.get("pattern") or "").strip()
        if not pattern:
            raise ToolError("pattern 为空")
        base = self.resolve(str(args.get("path") or "."))
        hits: list[str] = []
        try:
            for found in base.rglob(pattern):
                if self._skip(found):
                    continue
                hits.append(found.relative_to(self.workspace).as_posix())
                if len(hits) >= _MAX_SEARCH_HITS:
                    break
        except OSError:
            pass  # 无效 glob / 权限问题：返回目前已收集的结果
        if not hits:
            return ToolOutcome(output=f"没有匹配 {pattern!r} 的文件")
        tail = f"\n…（命中超过 {_MAX_SEARCH_HITS}，已截断）" if len(hits) >= _MAX_SEARCH_HITS else ""
        return ToolOutcome(output="\n".join(hits) + tail)

    def _grep(self, args: dict[str, Any]) -> ToolOutcome:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            raise ToolError("pattern 为空")
        try:
            rx = re.compile(pattern if args.get("regex") else re.escape(pattern))
        except re.error as exc:
            raise ToolError(f"正则不合法：{exc}") from exc
        base = self.resolve(str(args.get("path") or "."))
        files = [base] if base.is_file() else self._walk(base)
        matches: list[str] = []
        scanned = 0
        for file in files:
            if scanned >= _MAX_GREP_FILES or len(matches) >= _MAX_GREP_HITS:
                break
            try:
                if file.stat().st_size > 1_000_000:
                    continue
                raw = file.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:8192]:
                continue
            scanned += 1
            rel = file.relative_to(self.workspace).as_posix()
            for lineno, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(matches) >= _MAX_GREP_HITS:
                        break
        if not matches:
            return ToolOutcome(output=f"没有匹配 {pattern!r} 的内容（扫描 {scanned} 个文件）")
        tail = f"\n…（命中超过 {_MAX_GREP_HITS}，已截断）" if len(matches) >= _MAX_GREP_HITS else ""
        return ToolOutcome(output="\n".join(matches) + tail)

    def _walk(self, base: Path) -> list[Path]:
        out: list[Path] = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in files:
                path = Path(root) / name
                if path.suffix.lower() not in _BINARY_EXTS:
                    out.append(path)
                if len(out) >= _MAX_GREP_FILES:
                    return out
        return out

    # ------------------------------------------------------------------ #
    # write_file
    # ------------------------------------------------------------------ #
    def _target_text(self, args: dict[str, Any]) -> tuple[Path, str, str, str]:
        """-> (path, old, new, mode) where ``new`` is the approved end state."""
        mode = str(args.get("mode") or "overwrite")
        if mode not in {"overwrite", "append"}:
            raise ToolError(f"mode 只支持 overwrite/append，收到 {mode!r}")
        path = self.resolve(str(args.get("path", "")))
        if path.is_dir():
            raise ToolError(f"{path} 是目录，不能当文件写")
        content = str(args.get("content") or "")
        old = ""
        if path.exists():
            try:
                old = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise ToolError(f"旧内容读取失败：{exc}") from exc
        new = content if mode == "overwrite" else old + content
        return path, old, new, mode

    def _prepare_write(self, args: dict[str, Any]) -> ToolOutcome:
        path, old, new, mode = self._target_text(args)
        diff = "\n".join(
            difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile=path.name, tofile=path.name, lineterm="",
            )
        )
        summary = (
            f"新建文件 {path.relative_to(self.workspace)}（{len(new)} 字符）"
            if not old
            else f"{'追加' if mode == 'append' else '覆写'} {path.relative_to(self.workspace)}"
                 f"（{len(old)} -> {len(new)} 字符）"
        )
        preview = diff or summary
        if len(preview) > _MAX_PREVIEW:
            preview = preview[:_MAX_PREVIEW] + f"\n…（diff 共 {len(diff)} 字符，已截断）"
        return ToolOutcome(
            needs_approval=True,
            display={"kind": "write_file", "path": path.relative_to(self.workspace).as_posix(),
                     "mode": mode, "summary": summary, "diff": preview},
        )

    def _write_file(self, args: dict[str, Any]) -> ToolOutcome:
        path, _old, new, mode = self._target_text(args)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new, encoding="utf-8", newline="")
        except OSError as exc:
            raise ToolError(f"写入失败：{exc}") from exc
        rel = path.relative_to(self.workspace).as_posix()
        verb = "追加到" if mode == "append" else "写入"
        return ToolOutcome(
            output=f"已{verb} {rel}（现 {len(new)} 字符）",
            display={"kind": "write_file", "path": rel, "mode": mode,
                     "summary": f"已{verb} {rel}"},
        )

    # ------------------------------------------------------------------ #
    # run_command
    # ------------------------------------------------------------------ #
    def _prepare_command(self, args: dict[str, Any]) -> ToolOutcome:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ToolError("command 为空")
        lowered = command.lower()
        for rx in _COMMAND_BLOCKLIST:
            if rx.search(lowered):
                raise ToolError(
                    f"命令命中安全黑名单（{rx.pattern!r}），已被硬拦截，请换用更安全的方案"
                )
        return ToolOutcome(
            needs_approval=True,
            display={"kind": "run_command", "command": command,
                     "summary": f"执行命令：{command}"},
        )

    async def _run_command(self, args: dict[str, Any]) -> ToolOutcome:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ToolError("command 为空")
        asked = float(_as_int(args.get("timeout"), 0) or self.command_timeout)
        timeout = min(_MAX_COMMAND_TIMEOUT, max(1.0, asked))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=flags,
            )
        except OSError as exc:
            raise ToolError(f"命令启动失败：{exc}") from exc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            return ToolOutcome(
                ok=False,
                output=f"命令超时（>{timeout:.0f}s）已被终止，只拿到部分输出",
            )
        text = (out or b"").decode("utf-8", errors="replace")
        code = proc.returncode
        body = f"exit={code}\n{text}" if text else f"exit={code}\n(无输出)"
        return ToolOutcome(
            ok=code == 0,
            output=_cap(body),
            display={"kind": "run_command", "command": command},
        )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "DANGEROUS_TOOLS",
    "SAFE_TOOLS",
    "TOOL_SCHEMAS",
    "ToolBox",
    "ToolError",
    "ToolOutcome",
]
