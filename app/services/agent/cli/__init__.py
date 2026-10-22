"""外部 CLI 执行体适配层：把本机命令行 Agent 工具纳入统领大脑的调度。

公开面：
- load_cli_registry —— 从 config/cli_tools.yaml 加载执行体花名册
- CliRegistry —— 角色/工具名到执行体配置的查询口
- CliToolConfig —— 单个执行体的配置（命令、模型、成本档位、超时）
"""
from app.services.agent.cli.registry import (
    CliRegistry,
    CliToolConfig,
    RoleConfig,
    load_cli_registry,
)

__all__ = [
    "CliRegistry",
    "CliToolConfig",
    "RoleConfig",
    "load_cli_registry",
]
