"""给外部执行体构造任务 prompt：任务信封、段间交接、产物清单。

单独成模块的理由（2026-11-13）：``delegate.py`` 已经 import ``runner.py``，
把信封函数放在 delegate 里会让 runner 反向依赖 delegate 形成环。这里只依赖
标准库，谁都能安全 import。

三条都是纯函数，便于单测；也便于将来换执行体时只改这一处。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: 上一段结论注入下一段时的上限（字符）。一次委派光 CLI 自带脚手架就 3.6 万
#: prompt tokens（实测），再灌整段历史等于按倍数烧免费额度。
HANDOFF_CHARS = 2_000


def build_task_prompt(task: str, *, cwd: str) -> str:
    """给外部执行体加一段「任务信封」：它在哪、怎么交活、能不能问。

    为什么需要：七个适配器此前都是把 ``task`` 原样塞进 argv，执行体不知道
    自己在哪个目录、要交什么、能不能追问。Claude Code 的 subagent 模板固定写
    「你无法向用户提问，完成后用简洁报告返回」——第三句最容易被忽略、也最省
    额度：没有它，执行体会花大段输出解释「我需要更多信息」。

    **cwd 必须给绝对路径**：任务空间在 ``<workspace>/.taskspaces/<id>/work``，
    和用户以为的工作区根不是一回事，写相对路径等于让它自己猜。

    信封只加约束、不改写任务：原文完整保留在最后（"【任务】" 之下），
    避免平台替模型改语义。用户/模型想自己写完整契约时，直接把这三行写进 task
    也完全兼容——信封是幂等叠加，不是替换。
    """
    return (
        f"【工作目录】{cwd}\n"
        "【运行方式】你是被另一个 agent 无头调用的执行体，没有人能回答你的追问；"
        "遇到歧义按最合理的假设继续做，并在结论末尾列出你做了哪些假设。\n"
        "【交付】结论直接写在最后一段（调用方只读你最后的回复）；"
        "需要落盘的文件写到当前工作目录下的 outputs/ 里，并在结论里列出相对路径。\n"
        "【任务】\n"
        f"{task}"
    )


def with_context(prev_role: str, prev_output: str, task: str) -> str:
    """把上一段的结论作为「已知信息」注入下一段任务。

    为什么需要：「先让 A 查、再让 B 审」是控制台上最常见的用法，但两段之间
    此前只共享工作目录、不共享上下文——B 拿到的任务里没有 A 的任何结论，
    等于在审空气。这里显式传递（带截断），比靠「同一个 cwd」假装有上下文可靠。

    ``prev_output`` 为空时不注入，避免给第一段塞一个空段落。
    """
    context = (prev_output or "").strip()
    if not context:
        return task
    if len(context) > HANDOFF_CHARS:
        context = context[:HANDOFF_CHARS] + f"…(已截断，原文共 {len(prev_output)} 字符)"
    return (
        f"# 上一段（{prev_role}）的结论\n"
        f"{context}\n\n"
        f"# 你的任务\n"
        f"{task}"
    )


def collect_artifacts(cwd: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """列 ``<cwd>/outputs`` 下的文件（相对路径 + 字节数）。

    任务空间承诺「产物落 outputs/」，但此前全仓没有一处读过它——控制台的
    任务空间页永远显示「（暂无）」，调用方只能靠执行体自己说产出了什么。
    调用方要的是「你产出了什么」，不是「你说你产出了什么」。
    """
    try:
        base = Path(cwd)
        root = base / "outputs"
        if not root.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            items.append({"path": path.relative_to(base).as_posix(), "bytes": size})
            if len(items) >= limit:
                break
        return items
    except OSError:
        return []


def format_artifacts(items: list[dict[str, Any]]) -> str:
    """把产物清单拼成一句给模型看的话；空清单返回空串（不占正文）。"""
    if not items:
        return ""
    parts = [f"{item['path']}（{human_size(int(item['bytes']))}）" for item in items]
    return "（产物：" + "、".join(parts) + "）"


def human_size(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f}MB"
    if value >= 1024:
        return f"{value / 1024:.1f}KB"
    return f"{value}B"
