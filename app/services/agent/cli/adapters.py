"""各外部 CLI 的命令构造适配器。

每个适配器只负责一件事：把（任务, 工作目录）拼成该工具的非交互 argv。
参数形态来自各工具 --help 的实测记录（docs/2026-10-22-cli-probe.md），
工具升级改参数时只需动这里。
"""
from __future__ import annotations

import json
from typing import Any, ClassVar

from app.services.agent.cli.base import MAX_OUTPUT, CliAdapter

#: 走本机网关时的默认端点与占位 key（网关 /v1/* 当前不校验 key）
_DEFAULT_GATEWAY_URL = "http://127.0.0.1:8317/v1"
_DEFAULT_GATEWAY_KEY = "zk-local"


class QwenAdapter(CliAdapter):
    """Qwen Code（命令名 qwen）。

    非交互形态（0.24.6 实测）：位置参数直接给 prompt 即一次性模式；
    --approval-mode 只放行 plan/default（注册表已硬拦其他档）；
    backend=gateway 时按 OpenAI 兼容三参数指向本机 zkai 网关。
    """

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        argv = [cfg.command]
        if cfg.backend == "gateway":
            argv += [
                "--auth-type", "openai",
                "--openai-base-url", cfg.base_url or _DEFAULT_GATEWAY_URL,
                "--openai-api-key", cfg.api_key or _DEFAULT_GATEWAY_KEY,
            ]
        argv += [
            "-m", cfg.model,
            "--approval-mode", cfg.approval_mode,
            task,
        ]
        return argv


class ClaudeAdapter(CliAdapter):
    """Claude Code（命令名 claude，待安装）。

    非交互形态：claude -p "任务"（--print）；模型用 --model 指定。
    本适配器按公开文档形态先写好，安装后实测校准。
    """

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        return [cfg.command, "-p", task, "--model", cfg.model]


class CodexAdapter(CliAdapter):
    """Codex CLI（命令名 codex）。

    非交互形态：``codex exec``；``-s read-only`` 对应 plan 档——它自己的沙箱策略，
    比让审批弹窗挂起可靠。``--skip-git-repo-check`` 是给「工作区不是 git 仓库」兜底
    （任务台的 workspace 可以指到任意目录）。

    ⚠ 它走的是 ``~/.codex/config.toml`` 里配的 provider（本机是 zkai 网关），
    所以**网关进程环境里必须有 ``ZKAI_API_TOKEN``**，否则子进程报
    ``Missing environment variable``。
    """

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        argv = [cfg.command, "exec", "--skip-git-repo-check", "-m", cfg.model]
        if cfg.approval_mode == "plan":
            argv += ["-s", "read-only"]
        argv.append(task)
        return argv


class QoderAdapter(CliAdapter):
    """Qoder CLI（命令名 qoder / qodercli，1.1.78 的 --help 实测）。

    参数形态（2026-10-25 本机核对）：
    - ``-p/--print`` 非交互，查询走位置参数；
    - ``-m/--model`` 指定模型；
    - ``--permission-mode`` 只有 default / accept_edits / bypass_permissions /
      dont_ask / auto，**没有 plan 档**。

    所以 plan 映射到 ``dont_ask``：不弹审批、未预授权的一律拒绝。无头场景
    绝不能把 ``default`` 直接透传——它会等人按回车，而 runner 的 stdin 是
    DEVNULL，结果只能挂到超时（一次委派白等 300 秒，比失败更糟）。
    """

    _PERMISSION: ClassVar[dict[str, str]] = {"plan": "dont_ask", "default": "default"}

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        mode = self._PERMISSION.get(cfg.approval_mode, "dont_ask")
        argv = [
            cfg.command,
            "--model", cfg.model,
            "--permission-mode", mode,
        ]
        if cfg.effort:
            argv += ["--reasoning-effort", cfg.effort]
        if cfg.context_window:
            # ⚠ 这是"按多大窗口管理"，不是把模型窗口变大；留空时 Qoder 按 128K 算
            argv += ["--context-window", str(cfg.context_window)]
        argv += ["--print", task]
        return argv


class CodeBuddyAdapter(CliAdapter):
    """CodeBuddy / WorkBuddy CLI（命令名 codebuddy，2.175.0 的 --help 实测）。

    与 Qoder 共享一套 permission-mode 词表，但比它多一个 plan 档，所以直接透传；
    ``--output-format json`` 会多给 usage / total_cost_usd，
    回填里带上是「免费档到底烧了多少」的唯一客观口径。

    ⚠ 首次使用需在交互式 codebuddy 里 ``/login``（登录态与桌面端不共享，
    桌面端已登录也会报 Authentication required）。
    """

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        argv = [
            cfg.command,
            "--model", cfg.model,
            "--permission-mode", cfg.approval_mode,
            "--output-format", "json",
        ]
        if cfg.effort:
            argv += ["--effort", cfg.effort]
        if cfg.context_window:
            # CodeBuddy 没有 --context-window，等价旋钮是"自动压缩阈值"：
            # 给到 1m 表示只在 1M 处才压缩，而不是它默认跟随模型窗口
            argv += ["--autocompact", str(cfg.context_window)]
        argv += ["--print", task]
        return argv

    def parse_output(self, stdout: str, stderr: str) -> str:
        """从 ``--output-format json`` 里取正文，并附上自报用量。

        ⚠ 2.175.0 实测输出是**事件数组**，不是单个对象：
        ``[{type: message, ...}, {type: reasoning...}, ...,
        {type: result, result: "pong", total_cost_usd: 0, usage: {...}}]``。
        数组里前几条 message 事件带着**完整请求体**（system prompt 里会有项目的
        AGENTS.md）。按单个对象解析会失败并回落到裸文本，等于把整份上下文
        回喂给父模型——所以这里必须只挑最后那条 result 事件。
        """
        payload = _codebuddy_result_event((stdout or "").strip())
        if payload is None:
            return super().parse_output(stdout, stderr)
        body = str(payload.get("result") or payload.get("content") or "").strip()
        if not body:
            return super().parse_output(stdout, stderr)
        notes: list[str] = []
        usage = payload.get("usage")
        if isinstance(usage, dict):
            tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
            if tokens_in is not None or tokens_out is not None:
                notes.append(f"上游自报 token：入 {tokens_in} / 出 {tokens_out}")
        cost = payload.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            notes.append(f"自报成本 ${cost:.4f}")
        if payload.get("is_error") is True:
            notes.append("上游标记 is_error=true")
        if notes:
            body += "（" + "；".join(notes) + "）"
        if len(body) > MAX_OUTPUT:
            body = body[:MAX_OUTPUT] + f"\n…(输出已截断，共 {len(body)} 字符)"
        return body


class GeminiAdapter(CliAdapter):
    """Gemini CLI（命令名 gemini，0.61.0 的 --help 实测）。

    免费档里唯一「官方开放、不靠逆向」的执行体：Google AI Studio 的免费
    额度，跑 ``gemini-2.5-flash`` 这类模型。
    ``--approval-mode`` 原生有 plan 档（default/auto_edit/yolo/plan），直接透传；
    yolo 永远不许开（注册表已硬拦 approval_mode 非 plan/default）。
    ⚠ 但 Gemini 的 plan 是「把结论写进 plans/*.md 再回一句已保存」，正文不进
    stdout（实测），所以机械改写要用 default，plan 只给只读分析用。

    ⚠ Node 的 fetch 默认不读环境变量里的代理，跨境访问时必须让网关进程带上
    ``HTTPS_PROXY``；适配器补的 ``NODE_USE_ENV_PROXY=1``（Node 24+）负责让它生效。
    ⚠ 它**没有**思考强度/上下文窗口旋钮（0.61.0 的 --help 里没有），
    ``effort`` 与 ``context_window`` 对这条执行体无效。
    """

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        return [
            cfg.command,
            "-p", task,
            "-m", cfg.model,
            "--approval-mode", cfg.approval_mode,
            "-o", "json",
        ]

    def build_env(self) -> dict[str, str] | None:
        # NODE_USE_ENV_PROXY：Node 的 fetch 默认不读 HTTPS_PROXY（跨境必需）。
        # GEMINI_CLI_TRUST_WORKSPACE：无头模式不信任任何目录，不设就只回一句
        # "not running in a trusted directory" 然后退出。
        return {"NODE_USE_ENV_PROXY": "1", "GEMINI_CLI_TRUST_WORKSPACE": "true"}

    def parse_output(self, stdout: str, stderr: str) -> str:
        """从 ``-o json`` 的单对象里取 response，并附上 token 统计。"""
        text = (stdout or "").strip()
        if not text.startswith("{"):
            return super().parse_output(stdout, stderr)
        try:
            data = json.loads(text)
        except ValueError:
            return super().parse_output(stdout, stderr)
        if not isinstance(data, dict):
            return super().parse_output(stdout, stderr)
        body = str(data.get("response") or "").strip()
        if not body:
            return super().parse_output(stdout, stderr)
        tokens_in, tokens_out = _sum_token_stats(data.get("stats"))
        if tokens_in is not None or tokens_out is not None:
            body += f"（上游自报 token：入 {tokens_in} / 出 {tokens_out}）"
        if len(body) > MAX_OUTPUT:
            body = body[:MAX_OUTPUT] + f"\n…(输出已截断，共 {len(body)} 字符)"
        return body


def _codebuddy_result_event(text: str) -> dict[str, Any] | None:
    """从 CodeBuddy 的 json 输出里挑出 result 事件（兼容对象与事件数组两种形态）。"""
    if not text or text[0] not in "[{":
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for event in reversed(data):
            if isinstance(event, dict) and event.get("type") == "result":
                return event
        for event in reversed(data):
            if isinstance(event, dict) and "result" in event:
                return event
    return None


def _sum_token_stats(stats: object) -> tuple[int | None, int | None]:
    """把 Gemini ``stats.models.*.tokens`` 里的输入/输出 token 求和。

    字段名以 0.61.0 实测为准：``prompt`` 是总输入（含缓存），``candidates``
    是产出；``input`` / ``output`` 只在部分版本出现，用作回落。
    """
    if not isinstance(stats, dict):
        return None, None
    models = stats.get("models")
    if not isinstance(models, dict):
        return None, None
    total_in = total_out = 0
    seen = False
    for entry in models.values():
        if not isinstance(entry, dict):
            continue
        tokens = entry.get("tokens")
        if not isinstance(tokens, dict):
            continue
        for keys, side in ((("prompt", "input"), "in"), (("candidates", "output"), "out")):
            value = next((tokens[k] for k in keys if isinstance(tokens.get(k), int)), None)
            if value is None:
                continue
            seen = True
            if side == "in":
                total_in += value
            else:
                total_out += value
    if not seen:
        return None, None
    return total_in, total_out


class GenericAdapter(CliAdapter):
    """通用兜底：约定「命令 + 任务文本」形态，新工具未写专用适配器时用。"""

    def build_command(self, task: str, cwd: str) -> list[str]:
        return [self.config.command, task]


#: 执行体名 -> 专用适配器；未登记的走 GenericAdapter
ADAPTERS: dict[str, type[CliAdapter]] = {
    "qwen": QwenAdapter,
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "qoder": QoderAdapter,
    "codebuddy": CodeBuddyAdapter,
    "gemini": GeminiAdapter,
}
