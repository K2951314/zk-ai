"""外部 CLI 执行体注册表：加载 config/cli_tools.yaml 并回答「这个角色派给谁」。

与内部角色注册表（roles.py）同一套哲学：父模型派单时只说角色名，
具体调哪个 CLI、用什么模型、免费还是收费，全部由配置决定——
模型看不到也改不了这张表，防止它绕过成本档位自己挑收费渠道。

配置加载失败（文件不存在、YAML 语法错）不致命：返回空注册表，
delegate_cli 工具随之不可用并在 schema 层面消失，不影响网关其他功能。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Any

import yaml

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger("services.agent.cli")

#: 成本档位合法值：审批卡片上展示给操作者，也是日汇总告警的分组维度
COST_TIERS = frozenset({"free", "cheap", "paid"})

#: 后端形态：gateway = 指向本机 zkai 网关（OpenAI 兼容）；
#: official = 走工具自家的登录态与额度
_BACKENDS = frozenset({"gateway", "official"})

#: 外部 CLI 自带的审批级别：只允许这两档。
#: yolo/auto-edit 之类「自动同意写文件/跑命令」的模式绝不允许——
#: 那等于让外部工具绕过 zkai 自己的两阶段审批卡。
_APPROVAL_MODES = frozenset({"plan", "default"})

#: 单次委派超时允许区间（秒）：防止配置写出 0 或一整天
_TIMEOUT_MIN = 10
_TIMEOUT_MAX = 1800
_TIMEOUT_DEFAULT = 300

#: 思考强度档位（CodeBuddy 的 --effort 取值；Qoder 的 max 已实测接受）。
#: 不同 CLI 支持的档位可能更少，写超集由各适配器按工具映射。
EFFORT_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})

#: 上下文窗口上限：只做粗校验（2M 已经超过现役所有模型），0 = 用工具默认
_CONTEXT_WINDOW_MAX = 2_000_000


@dataclass(slots=True, frozen=True)
class CliToolConfig:
    """单个执行体的配置。"""

    name: str
    command: str
    backend: str
    model: str
    cost_tier: str
    timeout: float
    approval_mode: str
    #: backend=gateway 时的 OpenAI 兼容端点；空串由适配器落默认值
    base_url: str = ""
    #: backend=gateway 时随调用带上的 key；本机网关不校验时填占位即可
    api_key: str = ""
    #: 思考强度（空串 = 用工具默认）；语义是"上限提示"，不同 CLI 支持档位不同
    effort: str = ""
    #: 上下文窗口（0 = 用工具默认）。注意：这是**告诉 CLI 按多大管理**，
    #: 不会把模型窗口变大；Qoder 留空时自己按 128K 算
    context_window: int = 0
    #: 控制台「执行体」面板里可选的模型清单（空 = 只认 model 那一个）
    models: list[str] = field(default_factory=list)
    #: models 里哪些按免费档展示（只是标签，真正的计费以工具自报为准）
    free_models: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class RoleConfig:
    """一个对外角色：名字 + 目标执行体 + 给人/模型看的能力描述。"""

    name: str
    tool: str
    hint: str


@dataclass(slots=True)
class CliRegistry:
    """执行体花名册。加载时未通过校验的工具不进表（等于不存在）。"""

    tools: dict[str, CliToolConfig] = field(default_factory=dict)
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    #: 加载/校验阶段的告警（配置写了但被丢弃的原因），供体检脚本透出
    warnings: list[str] = field(default_factory=list)

    def tool_for_role(self, role: str) -> CliToolConfig | None:
        """角色名 -> 执行体配置；未知角色或目标工具不可用返回 None。"""
        rc = self.roles.get((role or "").strip())
        if rc is None:
            return None
        return self.tools.get(rc.tool)

    def known_roles(self) -> list[str]:
        """当前真正可派的角色（目标工具在册）。"""
        return sorted(name for name, rc in self.roles.items() if rc.tool in self.tools)

    def describe_roles(self) -> str:
        """给系统提示词用的角色清单文本，含成本档位（免费优先的决策依据）。"""
        parts = []
        for name in self.known_roles():
            rc = self.roles[name]
            tool = self.tools[rc.tool]
            tier_zh = {"free": "免费", "cheap": "低价", "paid": "收费"}[tool.cost_tier]
            hint = f"{rc.hint}，" if rc.hint else ""
            parts.append(f"{name}（{hint}执行体 {rc.tool}，模型 {tool.model}，{tier_zh}）")
        return "、".join(parts)


def _parse_tool(name: str, raw: Any, warnings: list[str]) -> CliToolConfig | None:
    """校验单个工具配置；不合法时记告警并返回 None（不进册）。"""
    if not isinstance(raw, dict):
        warnings.append(f"cli_tools: 工具 {name!r} 的配置不是映射，已忽略")
        return None
    if not raw.get("enabled", False):
        return None  # 没启用不算错，静默跳过
    command = str(raw.get("command") or "").strip()
    if not command:
        warnings.append(f"cli_tools: 工具 {name!r} 已启用但 command 为空，已忽略")
        return None
    if shutil.which(command) is None:
        warnings.append(
            f"cli_tools: 工具 {name!r} 已启用但找不到命令 {command!r}（未安装？），已忽略"
        )
        return None
    backend = str(raw.get("backend") or "official").strip()
    if backend not in _BACKENDS:
        warnings.append(
            f"cli_tools: 工具 {name!r} 的 backend={backend!r} 不合法"
            f"（可选 {sorted(_BACKENDS)}），已忽略"
        )
        return None
    model = str(raw.get("model") or "").strip()
    if not model:
        warnings.append(f"cli_tools: 工具 {name!r} 已启用但 model 为空，已忽略")
        return None
    cost_tier = str(raw.get("cost_tier") or "").strip()
    if cost_tier not in COST_TIERS:
        warnings.append(
            f"cli_tools: 工具 {name!r} 的 cost_tier={cost_tier!r} 不合法"
            f"（可选 {sorted(COST_TIERS)}），已忽略"
        )
        return None
    approval_mode = str(raw.get("approval_mode") or "default").strip()
    if approval_mode not in _APPROVAL_MODES:
        warnings.append(
            f"cli_tools: 工具 {name!r} 的 approval_mode={approval_mode!r} 不合法"
            f"（只允许 {sorted(_APPROVAL_MODES)}，外部工具的自动同意模式会绕过审批卡），已忽略"
        )
        return None
    try:
        timeout = float(raw.get("timeout", _TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        timeout = _TIMEOUT_DEFAULT
    timeout = max(_TIMEOUT_MIN, min(_TIMEOUT_MAX, timeout))
    effort = str(raw.get("effort") or "").strip().lower()
    if effort and effort not in EFFORT_LEVELS:
        warnings.append(
            f"cli_tools: 工具 {name!r} 的 effort={effort!r} 不在已知档位"
            f"{sorted(EFFORT_LEVELS)} 里，已按工具默认处理"
        )
        effort = ""
    try:
        context_window = int(raw.get("context_window") or 0)
    except (TypeError, ValueError):
        context_window = 0
    context_window = max(0, min(_CONTEXT_WINDOW_MAX, context_window))

    def _str_list(key: str) -> list[str]:
        value = raw.get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            warnings.append(f"cli_tools: 工具 {name!r} 的 {key} 不是列表，已忽略")
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    return CliToolConfig(
        name=name,
        command=command,
        backend=backend,
        model=model,
        cost_tier=cost_tier,
        timeout=timeout,
        approval_mode=approval_mode,
        base_url=str(raw.get("base_url") or "").strip(),
        api_key=str(raw.get("api_key") or "").strip(),
        effort=effort,
        context_window=context_window,
        models=_str_list("models"),
        free_models=_str_list("free_models"),
    )


def _parse_roles(raw: Any, tools: dict[str, CliToolConfig], warnings: list[str]) -> dict[str, RoleConfig]:
    """解析角色映射；指向不在册工具的角色记告警并丢弃。"""
    roles: dict[str, RoleConfig] = {}
    if raw is None:
        return roles
    if not isinstance(raw, dict):
        warnings.append("cli_tools: roles 段不是映射，已忽略")
        return roles
    for name, item in raw.items():
        role_name = str(name).strip()
        if not role_name:
            continue
        if not isinstance(item, dict):
            warnings.append(f"cli_tools: 角色 {role_name!r} 的配置不是映射，已忽略")
            continue
        tool = str(item.get("tool") or "").strip()
        if tool not in tools:
            warnings.append(
                f"cli_tools: 角色 {role_name!r} 指向不可用工具 {tool!r}"
                "（未启用或校验未过），已忽略"
            )
            continue
        roles[role_name] = RoleConfig(
            name=role_name,
            tool=tool,
            hint=str(item.get("hint") or "").strip(),
        )
    return roles


def load_cli_registry(settings: Settings) -> CliRegistry:
    """从配置目录加载 cli_tools.yaml；文件缺失或解析失败时返回空册。"""
    config_dir = settings.resolved_config_dir
    path = None
    for candidate in (config_dir / "cli_tools.yaml", config_dir / "cli_tools.yml"):
        if candidate.exists():
            path = candidate
            break
    if path is None:
        return CliRegistry()  # 模板都没复制：功能未启用，属正常状态
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("cli_tools 配置读取失败 %s: %s", path, exc)
        return CliRegistry(warnings=[f"cli_tools: 配置读取失败：{exc}"])
    if raw is None:
        return CliRegistry()
    if not isinstance(raw, dict):
        return CliRegistry(warnings=["cli_tools: 配置顶层不是映射，已整体忽略"])
    warnings: list[str] = []
    tools: dict[str, CliToolConfig] = {}
    raw_tools = raw.get("tools") or {}
    if not isinstance(raw_tools, dict):
        warnings.append("cli_tools: tools 段不是映射，已忽略")
        raw_tools = {}
    for name, item in raw_tools.items():
        parsed = _parse_tool(str(name), item, warnings)
        if parsed is not None:
            tools[parsed.name] = parsed
    roles = _parse_roles(raw.get("roles"), tools, warnings)
    registry = CliRegistry(tools=tools, roles=roles, warnings=warnings)
    for item in warnings:
        logger.warning("%s", item)
    return registry
