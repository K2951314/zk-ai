"""各外部 CLI 的命令构造适配器。

每个适配器只负责一件事：把（任务, 工作目录）拼成该工具的非交互 argv。
参数形态来自各工具 --help 的实测记录（docs/2026-10-22-cli-probe.md），
工具升级改参数时只需动这里。
"""
from __future__ import annotations

import json
import os
from typing import Any, ClassVar

from app.services.agent.cli.base import MAX_OUTPUT, CliAdapter, FailureKind, classify_failure

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
        # qwen 的 --approval-mode 只接受 plan/default，没有绕过档；
        # 统一权限的 bypass 只能降级为 default（它自身没有更宽的档位）。
        mode = "plan" if cfg.approval_mode == "plan" else "default"
        argv += [
            "-m", cfg.model,
            "--approval-mode", mode,
            task,
        ]
        return argv


class ClaudeAdapter(CliAdapter):
    """Claude Code（命令名 claude）。

    无头形态：``claude -p <task>``（``-p`` 是布尔开关，任务走位置参数）。

    - ``model`` 留空 = **不传 ``--model``**，用它自己配置的模型。本机是 CC Switch
      选定的那个，走 ``127.0.0.1:15721`` 的本地代理。**别硬塞网关别名**：``-p``
      走 SDK 分支，会在客户端校验模型归属，``zk-k3`` / 连 ``claude-sonnet-4-5``
      都被判 ``unrecognized_model``（2026-10-25 实测，网关日志里一条请求都没有）。
    - ``approval_mode: bypass`` → ``--dangerously-skip-permissions``。Claude Code
      没有无头可用的只读档（``--permission-mode plan`` 同样被客户端判非法），
      所以要用它只能开 bypass——配置里显式写，且只给用户自己 @ 的执行体。
    """

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        argv = [cfg.command]
        if cfg.approval_mode == "bypass":
            argv.append("--dangerously-skip-permissions")
        argv += ["-p", task]
        if cfg.model:
            argv += ["--model", cfg.model]
        return argv


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
        if cfg.approval_mode == "bypass":
            # 统一权限：任务空间内放行，沙箱与审批一起关
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        elif cfg.approval_mode == "plan":
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

    _PERMISSION: ClassVar[dict[str, str]] = {
        "plan": "dont_ask",
        "default": "default",
        "bypass": "bypass_permissions",
    }

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

    #: 统一权限的 bypass 映射到它自己的 bypassPermissions（词表大小写不同）
    _PERMISSION: ClassVar[dict[str, str]] = {
        "plan": "plan",
        "default": "default",
        "bypass": "bypassPermissions",
    }

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        mode = self._PERMISSION.get(cfg.approval_mode, "default")
        argv = [
            cfg.command,
            "--model", cfg.model,
            "--permission-mode", mode,
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


    def parse_usage(self, stdout: str, stderr: str) -> tuple[int | None, int | None, float | None]:
        """把 result 事件里的 usage / total_cost_usd 抽成结构化字段。

        这是「免费档到底烧了多少」的唯一客观口径：CLI 侧没有价目表，
        只能靠每次调用自报的成本累计出实测均价。
        """
        payload = _codebuddy_result_event((stdout or "").strip())
        if payload is None:
            return None, None, None
        tokens_in = tokens_out = None
        usage = payload.get("usage")
        if isinstance(usage, dict):
            raw_in, raw_out = usage.get("input_tokens"), usage.get("output_tokens")
            if isinstance(raw_in, int):
                tokens_in = raw_in
            if isinstance(raw_out, int):
                tokens_out = raw_out
        cost = payload.get("total_cost_usd")
        cost_usd = float(cost) if isinstance(cost, (int, float)) else None
        return tokens_in, tokens_out, cost_usd

    def fatal_output(self, stdout: str, stderr: str) -> FailureKind | None:
        """未登录时 codebuddy 退出码 0、只吐一行纯文本（2.176.0 实测）。

        2026-10-26：控制台把这种委派显示成「成功」，正文却是登录提示。
        这里只认它自己的登录原文，不用通用分类——正常回答里也可能出现
        "401" 之类字样，不能一律当失败。
        """
        text = f"{stdout}\n{stderr}".lower()
        if "authentication required" in text or "please use /login" in text:
            return classify_failure(text)
        return None


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

    #: 统一权限的 bypass 映射到 Gemini 自己的 yolo 档（它没有名为 bypass 的档位）
    _APPROVAL: ClassVar[dict[str, str]] = {
        "plan": "plan",
        "default": "default",
        "bypass": "yolo",
    }

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        mode = self._APPROVAL.get(cfg.approval_mode, "default")
        return [
            cfg.command,
            "-p", task,
            "-m", cfg.model,
            "--approval-mode", mode,
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


class CodeArtsAdapter(CliAdapter):
    """CodeArts CLI / 码道（华为云，命令名 codearts，26.11.1 的 run --help 实测）。

    无头形态：``codearts run [message..]``——位置参数直接收任务文本；
    ``-m provider/model`` 指定模型（``codearts models`` 拿清单，如
    ``huaweicloud-maas/GLM-5.2``）；``--variant`` 是它自己的思考强度档
    （provider-specific，官方示例 high/max/minimal）。

    审批三档（26.11.1 实测词表）：``--manual_approval`` / ``--auto_approval``
    / ``--full_access``。统一权限的映射：
      plan    -> 不传旗，走安装时写好的沙箱默认（bash_mode=sandbox、
                 network_policy=deny_all，install.ps1 落盘的两个 config.json）
      default -> 不传旗，同上
      bypass  -> 不映射（仍是沙箱默认）：``--full_access`` 等于绕过 zkai
                 的两阶段审批卡；注册表不让配置写 bypass，服务层动态提升的
                 ``allow_bypass`` 在这条执行体上同样不生效。
    ⚠ 2026-11-12 授权后实测：**``--manual_approval`` 在无头（stdin=DEVNULL）
    下会挂起等交互审批，90 秒都不返回**；不传旗 / ``--auto_approval`` 都秒回。
    所以 plan 不能再用它——沙箱默认本身就是最保守的可运行档，别为了「看起来
    更安全」把执行体挂死。

    授权与别家不同：**基于华为云 AK/SK 环境变量**（CODEARTS_CLI_AK / SK，
    在 codearts.huaweicloud.com/portal/settings/cli-auth 申请），没有
    「浏览器登录」那条路。未配置时**退出码是 0**、只在输出里报认证失败
    （2026-11-12 实测）——所以 fatal_output 必须认它，否则被记成成功。

    两个 2026-11-12 实测的「位置」坑（环境侧，代码改不了）：
    - 把 AK/SK 存到 ``~/.codeartsdoer/installers/.env`` **没用**：官方
      ``codearts.cmd`` 只设 KERNEL_*/OPENCODE_* 后直接跑 exe，从不加载它；
      CLI 只认进程环境变量。必须写进项目根 ``.env``，网关启动时加载、
      再由 ``run_delegation`` 的 ``{**os.environ, **extra}`` 继承给子进程。
    - 首次无头运行还会撞「文件夹未被信任」：``~/.codeartsdoer/cli-data/
      settings.json`` 的 ``security.folderTrust.enabled`` 默认 true，
      交互 TUI 才能点「信任」，无头一律 exit 1。按官方 FAQ 置 false 即可
      （文件权限仍由 CLI 沙箱 + 审批档控制）。
    CLI 输出是 **UTF-8**（PowerShell 5.1 控制台按 GBK 显示才看着像乱码），
    runner 的 ``decode("utf-8", errors="replace")`` 是对的，不要改。

    Windows 注意：which("codearts") 命中的是华为自己的 codearts.cmd 包装器
    （非 npm shim，launcher 取不出入口，直接报 LauncherError）。所以 Windows
    上 command 要给原生 exe 全路径（~/.codeartsdoer/installers/bin/codearts.exe），
    包装器设的那批 KERNEL_*/OPENCODE_* 环境变量由本适配器的 build_env 补齐
    （照抄 codearts.cmd，含 NODE_TLS_REJECT_UNAUTHORIZED=0——官方包装器
    替用户处理代理/证书场景的原样设置）。
    """

    def build_command(self, task: str, cwd: str) -> list[str]:
        cfg = self.config
        # --print-logs 把内部日志实时打到 stderr：2026-11-12 实测 +8.3s 起、
        # 42s 任务共 1786 行；模型答案仍是一次性出现在 stdout（+39.6s）。
        # 网关据此推进度、当空闲心跳。只是把本来写进文件的日志转到 stderr，
        # **不新增任何上游请求、不额外消耗积分**。
        argv = [cfg.command, "run", "--print-logs"]
        if cfg.model:
            argv += ["-m", cfg.model]
        if cfg.effort:
            argv += ["--variant", cfg.effort]
        # plan/default 都不传审批旗：--manual_approval 在无头下挂死（见类注释
        # 的 2026-11-12 实测），沙箱默认本身就是最保守的可运行档。
        # 也不映射 bypass -> --full_access：那等于绕开 zkai 的两阶段审批卡。
        argv.append(task)
        return argv

    def build_env(self) -> dict[str, str] | None:
        """补齐官方 codearts.cmd 包装器设置的环境变量（Windows 直跑 exe 时必需）。

        路径口径与包装器一致：``~/.codeartsdoer``（expanduser 跨平台）。
        """
        home = os.path.expanduser("~")
        root = os.path.join(home, ".codeartsdoer")
        return {
            "SCENARIO": "codeartsdoer",
            "KERNEL_DATA_DIR": os.path.join(root, "cli-data"),
            "KERNEL_STATE_DIR": os.path.join(root, "state"),
            "KERNEL_CONFIG_DIR": root,
            "OPENCODE_CHANNEL": "latest",
            "OPENCODE_CONFIG": os.path.join(root, "codearts_cli.json"),
            "OPENCODE_CONFIG_FILE": "codearts_cli.json,codearts_cli.jsonc",
            "OPENCODE_MODE": "tui",
            "PLUGIN_ENV": "hc",
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_ALWAYS_NOTIFY_UPDATE": "false",
            "CODEARTS_DISABLE_AUTO_UPDATE": "true",
            "CODEAGENT_USE_MESSAGE_FEEDBACK": "false",
            "OMO_SEND_ANONYMOUS_TELEMETRY": "0",
        }

    def fatal_output(self, stdout: str, stderr: str) -> FailureKind | None:
        """未配 AK/SK 时退出码 0、只报一句认证失败（2026-11-12 实测）。

        CLI 输出 UTF-8，中文正常；锚点仍用 ASCII 的 ``CODEARTS_CLI_AK``，
        这样即便哪天终端/上游把中文弄成乱码（PowerShell 5.1 控制台按 GBK
        显示就是这样），钩子照样认得出来。
        ⚠ ``--print-logs`` 会把「使用环境变量CODEARTS_CLI_AK/SK授权CLI」
        写进 stderr，那是**正常**日志。先剥掉 INFO/DEBUG 行再判断，否则
        每次成功委派都会被误判成未登录。
        """
        noise = ("INFO ", "DEBUG ", "TRACE ")
        stderr_lines = [
            ln for ln in stderr.splitlines() if not ln.lstrip().startswith(noise)
        ]
        text = f"{stdout}\n" + "\n".join(stderr_lines)
        text = text.lower()
        if "codearts_cli_ak" in text or "认证失败" in text:
            return FailureKind.AUTH
        return None


class McpAdapter(CliAdapter):
    """MCP 执行体：不拉子进程，由 runner 直接发 HTTP JSON-RPC。

    只负责审批预览；真正的调用在 ``runner._run_mcp`` 里，避免把
    「起进程读 stdout」那套逻辑套到 MCP 上。
    """

    def build_command(self, task: str, cwd: str) -> list[str]:
        raise NotImplementedError("MCP 执行体不走子进程")

    def preview(self, task: str, cwd: str) -> str:
        tier_zh = {"free": "免费", "cheap": "低价", "paid": "收费"}[self.config.cost_tier]
        args = json.dumps(self.config.mcp_args, ensure_ascii=False)
        if len(args) > 300:
            args = args[:300] + "…"
        return (
            f"委派给 MCP 执行体 {self.config.name}\n"
            f"服务：{self.config.mcp_url}\n"
            f"工具：{self.config.mcp_tool}（{tier_zh}档）\n"
            f"参数模板：{args}\n"
            f"工作目录：{cwd}\n"
            f"超时上限：{int(self.config.timeout)}s\n\n"
            f"任务内容：\n{task}"
        )


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
    "codearts": CodeArtsAdapter,
}
