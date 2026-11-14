"""agent 工作台的共享符号：常量、_Running 运行态、以及消息/附件的纯函数工具。

为什么单独一个文件（2026-11-14）：``service.py`` 从 1,818 行拆成四个 mixin 后，
这些符号被多个 mixin 同时使用；留在 ``service.py`` 里会让 mixin 反向 import
外壳，形成循环。这里是**依赖的汇点**——mixin 和外壳都只依赖它，它不依赖任何
agent 内部模块。

内容全部**逐字节原样搬移**，没有改一行逻辑。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.errors import ZKAIError
from app.core.logging import get_logger
from app.services.agent.cli import CliRegistry
from app.services.agent.confirm import SessionApprovals

if TYPE_CHECKING:  # 前向引用：_Running.parent 的类型，运行期不需要
    from app.services.agent.service import AgentService

logger = get_logger("services.agent")


logger = get_logger("services.agent")


#: 执行体实时日志推 SSE 的节流（秒）。码道 --print-logs 实测 1786 行/42s，
#: 不节流会把页面刷爆；摘要行由 summarize_cli_line 先过滤。
_CLI_LOG_THROTTLE = 2.0


#: 旧行为：全局静默 180s 判挂死。**已不再使用**，改由 cli_tools.yaml 的
#: ``idle_timeout`` 逐执行体配置（默认 0 = 只看总时长）。保留常量只为
#: 让「谁还记得 180 这个数」有处可查——别再加回去。
_LEGACY_CLI_IDLE_TIMEOUT = 180.0


_ACTIVE_STATUSES = frozenset({"running", "waiting_approval"})


#: followup 允许从哪些状态继续
_RESUMABLE_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})


_COMPACT_TAIL = 10  # 压缩时保留最近的消息条数


#: 任务台权限档。default = 界内写文件直接执行、命令仍需批准；
#: ask = 写与命令都逐条批准；full = 两者都不再逐条批准（黑名单/越界仍硬拦）。
PERMISSION_MODES = frozenset({"default", "ask", "full"})


#: 回填给父模型的子任务结论上限（再长就是拿子会话的原文挤父的上下文）。
_MAX_CHILD_SUMMARY = 4_000


#: 等子任务时的轮询间隔：只用于「顺带看一眼是否卡在等审批」，完成信令本身
#: 是事件驱动（run.done），所以这个间隔不影响响应速度。
_CHILD_POLL = 0.25


@dataclass(slots=True)
class _Running:
    """Live state of one executing session."""

    session_id: str
    workspace: Path
    model: str
    #: 权限档：default / ask / full（见 PERMISSION_MODES）
    permission: str = "default"
    approvals: SessionApprovals = field(default_factory=SessionApprovals)
    subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    cancelled: bool = False
    #: 「停止」不只是改状态：set 之后 runner 会真正 kill 掉子进程
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    #: 本会话启动时的外部执行体花名册快照；空册 = delegate_cli 不出现
    cli_registry: CliRegistry = field(default_factory=CliRegistry)
    # ---- 子任务派发 ----------------------------------------------------- #
    #: 父服务的引用；非 None = 这是子会话，不允许再派发（工具箱里也没有那个 schema）。
    parent: AgentService | None = None
    #: 父会话 id，仅用于事件归属与展示，不作查询键。
    parent_session: str | None = None
    #: 根=1，子=2。
    depth: int = 1
    #: 本会话在跑的子任务数。
    live_kids: int = 0
    #: 本批 tool_calls 里还没起跑的 spawn 数（**不含正在执行的那个**）。
    reserved: int = 0
    #: 本批**已批准**的 spawn 数（同批全程累加，批末不清）。
    #: _execute_tool 是串行 await 的，同批 spawn 天然一个个跑完，第一个结束时
    #: live_kids 已归 0——光看实时并发拦不住同批第二个，用这个补上。
    spawned_batch: int = 0
    #: 父子信令：loop 结束时由 ``_guarded_loop`` 的 finally put 一次。
    #: 与 subscribers（SSE 订阅者列表）刻意分开——那个是列表可多个，这个是单值。
    done: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)


def usage_fields(result: Any) -> dict[str, Any]:
    """把执行体自报的用量整理成前端可展示的字段。

    **只放拿到的**：CLI 没有价目表，拿不到 token/成本就留空，绝不用 0 冒充
    （0 会被读成「免费」，而实际是「不知道」——同 usage_ledger 里那条原则）。
    2026-11-13 起 claude 也走 ``--output-format json``，所以收费档开始有数字了。
    """
    fields: dict[str, Any] = {}
    if getattr(result, "tokens_in", None) is not None:
        fields["tokens_in"] = result.tokens_in
    if getattr(result, "tokens_out", None) is not None:
        fields["tokens_out"] = result.tokens_out
    if getattr(result, "cost_usd", None) is not None:
        fields["cost_usd"] = round(float(result.cost_usd), 4)
    if getattr(result, "session_ref", ""):
        fields["session_ref"] = result.session_ref
    return fields


#: 会话里允许贴的图片（data URL 或 https 直链）。
#: 只要形状对就放行——真正的可达性由上游判定，前端不该替上游猜。
_DATA_IMAGE = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=\s]+$")


_MAX_IMAGES = 8


#: 单张上限：跟 /v1 那条路的 10MB 字节闸对齐，别在这里比它更松
_MAX_IMAGE_CHARS = 10 * 1024 * 1024


def _normalise_images(images: list[str] | None) -> list[str]:
    """校验并归一化前端送来的图片列表；坏图**拒绝并说明**，不静默丢弃。

    为什么不静默丢弃（2026-11-05）：静默丢会让运营者以为图进去了，
    而模型回答「我看不见图里的东西」——排障会绕到模型能力上去，
    实际是接口层吃了。任务台是给人操作的，宁可直接报错。
    """
    if not images:
        return []
    cleaned: list[str] = []
    for index, raw in enumerate(images, 1):
        item = (raw or "").strip()
        if not item:
            continue
        if not (_DATA_IMAGE.match(item) or item.startswith(("https://", "http://"))):
            raise ZKAIError(f"第 {index} 张图片格式不支持（只收 PNG/JPEG/GIF/WebP 的 data URL 或 http 直链）",
                            http_status=400)
        if len(item) > _MAX_IMAGE_CHARS:
            raise ZKAIError(f"第 {index} 张图片太大（上限 {_MAX_IMAGE_CHARS // 1024 // 1024} MB）",
                            http_status=400)
        cleaned.append(item)
    if len(cleaned) > _MAX_IMAGES:
        raise ZKAIError(f"一次最多 {_MAX_IMAGES} 张图片", http_status=400)
    return cleaned


def _attachment_data(
    images: list[str] | None, files: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """落库用的 ``data`` 片段：没有附件就返回 None（保持老行的形状不变）。

    图片和文件分开存：图片要还原成 content parts 发上游，
    文件是**落盘后的路径**，只作记录（正文里已经写了给 Agent 看的那份）。
    """
    kept_images = _normalise_images(images)
    data: dict[str, Any] = {}
    if kept_images:
        data["images"] = kept_images
    if files:
        data["files"] = files
    return data or None


#: 上传文件的落盘目录（相对 data_dir）。**故意不写进 agent 工作区** ——
#: 那里是代码目录，混进用户传的 xlsx/pdf 会让 git status 变脏、也会被
#: Agent 的文件列表工具扫到。放 data/ 下既能被路径读到，又不污染工程。
_UPLOAD_DIR_NAME = "agent_uploads"


#: 单个文件上限。跟图片一致（10MB），理由也一样：/v1 那条路的字节闸就是这个数
_MAX_FILE_BYTES = 10 * 1024 * 1024


_MAX_FILES = 8


#: 换行符常量。写成 ``"\n"`` 字面量在通过 heredoc 生成时会被吃掉反斜杠，
#: 变成一个真实换行、把字符串字面量劈成两半（本项目已经栽过四次）。
_NEWLINE = chr(10)


def _uploads_dir(data_dir: Path) -> Path:
    target = Path(data_dir) / _UPLOAD_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def _safe_name(raw: str) -> str:
    """只留文件名本体，去掉任何路径成分 —— 防 ``../../etc/passwd`` 这类取名。"""
    name = Path(str(raw or "")).name
    name = re.sub(r"[^\w.\-一-鿿]+", "_", name).strip("._")
    return name or "file"


def _store_files(
    files: list[dict[str, Any]] | None, *, data_dir: Path, session_id: str
) -> list[dict[str, Any]]:
    """把前端送来的文件落到 ``data/agent_uploads/<session>/``，返回落盘清单。

    为什么落盘而不是把内容塞进消息（2026-11-05）：Agent 有读写文件的工具，
    给它一个**真实路径**比把二进制塞进 prompt 有用得多——它能自己决定
    读哪一段、跑什么命令处理。所以这里只负责「文件在磁盘上、路径告诉它」，
    不做任何内容解读。

    形状约定（前端照此构造）::

        {"name": "报价单.xlsx", "data": "data:application/...;base64,...."}

    ``data`` 也接受已经落过盘的相对路径（``path``），便于将来复用。
    """
    if not files:
        return []
    if len(files) > _MAX_FILES:
        raise ZKAIError(f"一次最多 {_MAX_FILES} 个文件", http_status=400)
    target_dir = _uploads_dir(data_dir) / _safe_name(session_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    stored: list[dict[str, Any]] = []
    for index, item in enumerate(files, 1):
        if not isinstance(item, dict):
            raise ZKAIError(f"第 {index} 个文件描述不合法", http_status=400)
        name = _safe_name(str(item.get("name") or f"file-{index}"))
        raw = str(item.get("data") or "").strip()
        if not raw:
            raise ZKAIError(f"第 {index} 个文件（{name}）没有内容", http_status=400)
        marker = ";base64,"
        is_data_url = raw.startswith("data:") and marker in raw
        # 裸 base64 也接受 —— 前端偶尔会省掉 mime 头
        payload = raw.split(marker, 1)[1] if is_data_url else raw
        try:
            blob = base64.b64decode(payload, validate=False)
        except (ValueError, binascii.Error) as exc:
            raise ZKAIError(f"第 {index} 个文件（{name}）不是合法的 base64",
                            http_status=400) from exc
        if len(blob) > _MAX_FILE_BYTES:
            raise ZKAIError(
                f"第 {index} 个文件（{name}）超过 {_MAX_FILE_BYTES // 1024 // 1024}MB",
                http_status=400,
            )
        # 同名文件不覆盖：加序号，避免第二次上传把第一次的抹掉
        destination = target_dir / name
        stem, suffix = destination.stem, destination.suffix
        counter = 2
        while destination.exists():
            destination = target_dir / f"{stem}-{counter}{suffix}"
            counter += 1
        destination.write_bytes(blob)
        stored.append({"name": name, "path": str(destination), "bytes": len(blob)})
    return stored


def _describe_files(files: list[dict[str, Any]] | None) -> str:
    """把落盘清单拼成一段给 Agent 看的说明，追加在任务正文后面。

    用**绝对路径**是有意的：Agent 的工作区可能不是 data/，
    相对路径会指错地方；而它本来就有读任意路径的权限。
    """
    if not files:
        return ""
    lines = ["", "", "[附件] 以下文件已存到本机，可直接读取："]
    for item in files:
        size = int(item.get("bytes") or 0)
        shown = f"{size / 1024:.0f} KB" if size >= 1024 else f"{size} B"
        lines.append(f"- {item['path']}（{shown}，原名 {item['name']}）")
    return _NEWLINE.join(lines)


def _user_content(text: str | None, data: dict[str, Any] | None) -> str | list[dict[str, Any]]:
    """把一条 user 消息还原成上游要的形状：带图就是 content parts，不带还是纯串。"""
    images = (data or {}).get("images") or []
    if not images:
        return text or ""
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for url in images:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


#: 保活对 loop 任务的强引用（done callback 里自动清理）
_BACKGROUND_TASKS: set[asyncio.Task] = set()
