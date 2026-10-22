"""各外部 CLI 的命令构造适配器。

每个适配器只负责一件事：把（任务, 工作目录）拼成该工具的非交互 argv。
参数形态来自各工具 --help 的实测记录（docs/2026-10-22-cli-probe.md），
工具升级改参数时只需动这里。
"""
from __future__ import annotations

from app.services.agent.cli.base import CliAdapter

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
    """Codex CLI（命令名 codex，待安装）。

    非交互形态：codex exec "任务"。安装后实测校准。
    """

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        return [cfg.command, "exec", task, "-m", cfg.model]


class GenericAdapter(CliAdapter):
    """通用兜底：约定「命令 + 任务文本」形态，新工具未写专用适配器时用。"""

    def build_command(self, task: str, cwd: str) -> list[str]:
        return [self.config.command, task]


#: 执行体名 -> 专用适配器；未登记的走 GenericAdapter
ADAPTERS: dict[str, type[CliAdapter]] = {
    "qwen": QwenAdapter,
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
}
