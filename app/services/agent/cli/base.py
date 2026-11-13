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
    """委派失败的分类。

    ``TIMEOUT`` / ``CANCELLED`` 是 2026-11-13 从 ``FAILED`` 里拆出来的：
    两者都**不是任务描述的问题**，而原来归到 FAILED 时给模型的建议是
    「调整任务描述后重试一次」——对超时是反向建议（同样的任务重跑大概率
    还超时，白烧一遍额度）。分类不同，可采取的行动本来就不同。
    """

    AUTH = "auth"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(slots=True)
class CliResult:
    """一次 CLI 委派的结构化结果。"""

    ok: bool
    output: str
    failure: FailureKind | None = None
    elapsed: float = 0.0
    exit_code: int | None = None
    #: 上游自报用量（拿不到就是 None，**不要用估算值冒充**）。
    #: 这些字段是"这个执行体到底烧了多少"的唯一客观口径：网关侧有牌价，
    #: CLI 侧没有价目表，只能靠调用后自报的成本累计出实测均价。
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    #: 上游自报的会话标识（claude 的 session_id 等）。有它才有「接着上次跑」
    #: 的可能；拿不到就是空串，**不要瞎猜一个**。
    session_ref: str = ""


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
        # 2026-11-12 实测：CodeArts CLI 未配 AK/SK 时退出码 0、只报一句
        # 「认证失败，没有设置环境变量 CODEARTS_CLI_AK/CODEARTS_CLI_SK」。
        # CLI 输出是 UTF-8（PowerShell 控制台按 GBK 显示才像乱码），所以中文
        # 与 ASCII 变量名都能匹配；保留后者是为了兼容终端转码后的形态。
        r"codearts_cli_ak",
        r"认证失败",
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
    FailureKind.TIMEOUT: (
        "执行超时（不是任务描述写得不好）。同样的任务原样重跑大概率还会超时："
        "把范围缩小、拆成更小的步骤，或换一个更快的执行体；"
        "它可能在被杀之前已经改了文件，重试前先用 read_file/list_dir 看一眼现状。"
    ),
    FailureKind.CANCELLED: (
        "这次委派被用户主动停止了（不是失败）。不要自动重试；"
        "需要继续就先把剩余工作说清楚，等用户确认后再派。"
    ),
    FailureKind.FAILED: (
        "执行失败。可以调整任务描述后重试一次，或改派其他角色；"
        "仍失败就在总结里说明失败原因。"
    ),
}

#: 失败标签（给模型看的中文名）。新增枚举时必须在这里补一行，
#: 否则 ``failure_output`` 会 KeyError——这是刻意的硬失败，不静默降级。
_FAILURE_LABEL: dict[FailureKind, str] = {
    FailureKind.AUTH: "未登录",
    FailureKind.QUOTA: "额度耗尽",
    FailureKind.TIMEOUT: "执行超时",
    FailureKind.CANCELLED: "已取消",
    FailureKind.FAILED: "执行失败",
}


def clip_output(text: str, limit: int = MAX_OUTPUT) -> str:
    """超长输出取「头 + 尾」，不是只留头部。

    报错堆栈、pytest 摘要、结论段落**几乎总在末尾**，只砍尾巴等于把答案砍掉、
    把过程留给模型——2026-11-13 之前的实现正是 ``text[:MAX_OUTPUT]``。
    省略标记写清省略了多少字符，模型才知道自己拿到的是残缺的。
    """
    if len(text) <= limit:
        return text
    head = max(0, limit // 3)
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    return (
        f"{text[:head]}\n…(中间省略 {omitted} 字符)…\n"
        f"（以下是末尾 {tail} 字符）\n{text[-tail:]}"
    )


def failure_output(kind: FailureKind, detail: str) -> str:
    """拼一段回喂父模型的失败文案：分类 + 原始摘要 + 行动建议。

    摘要同样保尾：真实诊断（Traceback 末行、退出原因）都在末尾。
    """
    label = _FAILURE_LABEL[kind]
    detail = detail.strip()
    if len(detail) > 1_000:
        detail = (
            f"{detail[:300]}\n…(中间省略 {len(detail) - 1_000} 字符)…\n"
            f"（以下是末尾 700 字符）\n{detail[-700:]}"
        )
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

    def session_ref(self, stdout: str) -> str:
        """从输出里取上游的会话标识；拿不到返回空串。子类按格式覆盖。"""
        return ""

    def parse_usage(self, stdout: str, stderr: str) -> tuple[int | None, int | None, float | None]:
        """从输出里抽 ``(输入 token, 输出 token, 自报成本 USD)``。

        默认全 None：**拿不到就明确说拿不到**，不用字数估算冒充真实用量——
        价格/额度判断一旦建立在估算上，就等于没有账本。子类按自己的
        输出格式覆盖（目前只有 CodeBuddy 的 ``--output-format json`` 会给）。
        """
        return None, None, None


def build_adapter(config: CliToolConfig) -> CliAdapter:
    """按后端形态与执行体名挑适配器；未专门实现的走通用封装。"""
    from app.services.agent.cli.adapters import ADAPTERS, GenericAdapter, McpAdapter

    if config.backend == "mcp":
        return McpAdapter(config)
    cls = ADAPTERS.get(config.name, GenericAdapter)
    return cls(config)
