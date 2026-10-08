"""商汤积分消耗器（scripts/burn_sensenova.py）的控制台读写层。

消耗器是独立进程、不经网关，所以控制台原本看不到它。本模块把两件事接上：

1. **读运行状态**：从 ``data/burn_state.json``（消耗器自己的账本）算出每账号的
   5h/周窗口已烧、熔断线、下一个边界、剩余可烧条数、AIMD 并发目标——也就是
   「什么时候该停、什么时候该启动」的那张表。
2. **读写配置**：``config/burner.yaml``（gitignore，模板 burner.example.yaml 可提交）。
   写入走文本级行替换，保留文件里的注释与排版——操作员靠这些注释记住锚点是怎么
   算出来的，格式化重排会把注释和键挤散。

拆分后的子模块（保持 ``burner_service.xxx`` 的调用方式不变）：
- ``config_io``：配置键表、类型校验、行级写入
- ``status``：账本读取、窗口账计算、派生提示、snapshot 聚合
- ``reconcile``：账号核对、校准注入、费率反推
- ``restart``：托盘文件 / systemd 重启
"""

from __future__ import annotations

from app.services.burner_service.config_io import (
    CONFIG_KEYS,
    FLASH_LITE_REQUIRED,
    FORM_FIELDS,
    is_flash_lite,
    read_config,
    validate_patch,
    write_config,
)
from app.services.burner_service.reconcile import (
    RECONCILE_RESTART_KEYS,
    _fmt_hhmm,
    _fmt_weekday_hhmm,
    _key_index,
    _parse_hhmm,
    _parse_weekday_hhmm,
    account_anchor_ts,
    account_week_anchor_ts,
    accounts_to_anchors,
    anchors_to_accounts,
    apply_reconcile,
    only_to_set,
    reconcile_diff,
    set_to_only,
    suggest_calibration,
    week_anchors_to_accounts,
)
from app.services.burner_service.restart import (
    _SYSTEMD_UNIT,
    detect_restart_mode,
    is_burner_running,
    request_restart,
    request_restart_ex,
    restart_pending,
    start_burner,
    stop_burner,
)
from app.services.burner_service.status import (
    WIN_5H,
    WIN_WEEK,
    AccountStatus,
    BurnerStatus,
    _parse_global_week_anchor,
    _warnings,
    account_view,
    estimate_request_cost,
    read_status,
    snapshot,
)

__all__ = [
    "CONFIG_KEYS",
    "FLASH_LITE_REQUIRED",
    "FORM_FIELDS",
    "RECONCILE_RESTART_KEYS",
    "WIN_5H",
    "WIN_WEEK",
    "_SYSTEMD_UNIT",
    "AccountStatus",
    "BurnerStatus",
    "_fmt_hhmm",
    "_fmt_weekday_hhmm",
    "_key_index",
    "_parse_global_week_anchor",
    "_parse_hhmm",
    "_parse_weekday_hhmm",
    "_warnings",
    "account_anchor_ts",
    "account_view",
    "account_week_anchor_ts",
    "accounts_to_anchors",
    "anchors_to_accounts",
    "apply_reconcile",
    "detect_restart_mode",
    "estimate_request_cost",
    "is_burner_running",
    "is_flash_lite",
    "only_to_set",
    "read_config",
    "read_status",
    "reconcile_diff",
    "request_restart",
    "request_restart_ex",
    "restart_pending",
    "set_to_only",
    "snapshot",
    "start_burner",
    "stop_burner",
    "suggest_calibration",
    "validate_patch",
    "week_anchors_to_accounts",
    "write_config",
]
