"""CLI 适配器基类：统一「怎么调一个外部 CLI」的接口与错误分类。

错误分三类（回填文案要让父模型能据此做不同决策）：
- AUTH（未登录/凭据失效）：要人工介入，不该让父模型自动重试
- QUOTA（额度耗尽/限流）：免费池没了，父模型应换执行体重试
- FAILED（执行失败）：普通失败，父模型可自行判断是否换方案
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.agent.cli.registry import CliToolConfig

#: 回喂模型的单次输出上限（与 tools.py 的口径一致，防撑爆上下文）
MAX_OUTPUT = 8_000


class FailureKind(Enum):
    """委派失败的分类。"""

    AUTH = "auth"
    QUOTA = "quota"
    FAILED = "failed"


@dataclass(slots=True)
class CliResult:
    """一次 CLI 委派的结构化结果。"""

    ok: bool
    output: str
    failure: FailureKind | None = None
    elapsed: float = 0.0
    exit_code: int | None = None


#: 「未登录」特征：各工具报错文案不同，逐个收录实测形态
#: 2026-10-25 补 CodeBuddy 的原文（"Authentication required. Please use /login
#: command to sign in"）——它一个既有 pattern 都不匹配，未登录被当成普通失败，
#: 父模型会白重试而不是改派。
_AUTH_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"no auth type is selected",
        r"not logged in",
        r"please log in",
        r"please use /login",
        r"authentication required",
        r"sign in to your account",
        r"login required",
        # 2026-10-26 实测：Gemini CLI 登录态过期时先弹这句（stdin 是 DEVNULL，
        # 它等到 EOF 才继续，最终按失败退出），原文必须认成未登录。
        r"opening authentication page",
        r"unauthorized",
        r"invalid api key",
        r"authentication failed",
        r"401",
    )
)

#: 「额度耗尽」特征：限流、配额、余额不足都归这类
_QUOTA_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"rate limit",
        r"too many requests",
        r"429",
        r"quota exceeded",
        r"quota_exceeded",
        r"insufficient balance",
        r"insufficient_quota",
        r"out of credits",
        r"usage limit",
        r"rpm exhausted",
        r"rps exhausted",
        r"tpm exhausted",
    )
)


def classify_failure(text: str) -> FailureKind:
    """按错误文本分类：AUTH 优先于 QUOTA（401 里可能同时出现 quota 字样）。"""
    if any(p.search(text) for p in _AUTH_PATTERNS):
        return FailureKind.AUTH
    if any(p.search(text) for p in _QUOTA_PATTERNS):
        return FailureKind.QUOTA
    return FailureKind.FAILED


_FAILURE_HINT: dict[FailureKind, str] = {
    FailureKind.AUTH: (
        "该执行体未登录或凭据失效，需要人工处理。"
        "不要重试这个执行体；如有其他可用角色请改派，或直接在总结里说明情况。"
    ),
    FailureKind.QUOTA: (
        "该执行体的额度已耗尽或被限流（免费池可能烧完了）。"
        "请改派其他角色重试本任务；都没有可用时，在总结里说明。"
    ),
    FailureKind.FAILED: (
        "执行失败。可以调整任务描述后重试一次，或改派其他角色；"
        "仍失败就在总结里说明失败原因。"
    ),
}


def failure_output(kind: FailureKind, detail: str) -> str:
    """拼一段回喂父模型的失败文案：分类 + 原始摘要 + 行动建议。"""
    label = {
        FailureKind.AUTH: "未登录",
        FailureKind.QUOTA: "额度耗尽",
        FailureKind.FAILED: "执行失败",
    }[kind]
    detail = detail.strip()
    if len(detail) > 1_000:
        detail = detail[:1_000] + "…(已截断)"
    body = f"（原始输出：{detail}）" if detail else ""
    return f"委派失败【{label}】{body}\n{_FAILURE_HINT[kind]}"


class CliAdapter:
    """适配器协议：把一个外部 CLI 的非交互调用封装成统一命令构造。"""

    def __init__(self, config: CliToolConfig) -> None:
        self.config = config

    def build_command(self, task: str, cwd: str) -> list[str]:
        """拼出完整 argv（不经 shell）。子类实现。"""
        raise NotImplementedError

    def build_env(self) -> dict[str, str] | None:
        """需要追加/覆盖的环境变量；None 表示沿用当前环境。子类可覆盖。"""
        return None

    def fatal_output(self, stdout: str, stderr: str) -> FailureKind | None:
        """退出码 0、但输出其实是失败时，返回失败分类。子类按需覆盖。

        不是所有 CLI 都用退出码表达失败：CodeBuddy 未登录时退出 0、只吐一行
        登录提示（2026-10-26 实测），不拦就会被记成「成功」。
        """
        return None

    def preview(self, task: str, cwd: str) -> str:
        """审批卡片上给操作者看的委派预览。"""
        tier_zh = {"free": "免费", "cheap": "低价", "paid": "收费"}[self.config.cost_tier]
        cmd = " ".join(self.build_command(task, cwd))
        if len(cmd) > 500:
            cmd = cmd[:500] + "…"
        return (
            f"委派给外部执行体 {self.config.name}\n"
            f"模型：{self.config.model}（{tier_zh}档）\n"
            f"工作目录：{cwd}\n"
            f"超时上限：{int(self.config.timeout)}s\n"
            f"完整命令：{cmd}\n\n"
            f"任务内容：\n{task}"
        )

    def parse_output(self, stdout: str, stderr: str) -> str:
        """从原始输出中提取回喂模型的正文。子类可按工具输出格式覆盖。"""
        text = stdout.strip() or stderr.strip()
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + f"\n…(输出已截断，共 {len(text)} 字符)"
        return text


def build_adapter(config: CliToolConfig) -> CliAdapter:
    """按执行体名挑适配器；未专门实现的走通用封装。"""
    from app.services.agent.cli.adapters import ADAPTERS, GenericAdapter

    cls = ADAPTERS.get(config.name, GenericAdapter)
    return cls(config)
