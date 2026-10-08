"""积分消耗器 endpoints (``/admin/burner/*``).

消耗器是独立进程、不经网关，控制台原本完全看不到它。这里把它的配置
（config/burner.yaml）和账本（data/burn_state.json）接进控制台：窗口余量、
每账号 5h/周边界、剩余可烧条数都能看，参数改完写回 YAML，重启消耗器即生效。
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, HTTPException, Query, status

from app.api.admin._common import logger, router
from app.core.config import load_app_config
from app.services import burner_service


def _burner_paths() -> tuple[Path, Path]:
    """(账本, burner.yaml)。

    账本走 `Settings.burner_state_path`：服务器上 burner 由 systemd 用
    --state-file 指到 `/var/lib/zkai/burner/`，比网关 data_dir 深一层。
    直接拼 data_dir 会让控制台去读写一个不存在的账本——运营者以为校准
    生效了，真正的 burner 什么都没看到。
    """
    settings = load_app_config().settings
    return (settings.burner_state_path,
            settings.resolved_config_dir / "burner.yaml")


@router.get("/burner", summary="积分消耗器配置与运行状态")
async def burner_snapshot() -> dict[str, Any]:
    state_file, config_file = _burner_paths()
    return burner_service.snapshot(state_file, config_file)


@router.put("/burner/config", summary="保存积分消耗器配置")
async def burner_save_config(
    payload: dict[str, Any] = Body(...),
    restart: Annotated[bool, Query(description="保存后请求重启消耗器（改配置才会生效）")] = True,
) -> dict[str, Any]:
    """写入 config/burner.yaml（保留注释），返回新的快照。

    写失败整体回滚：文件用临时文件 + replace，异常时原文件不动，避免界面显示
    成功而磁盘还是旧值。

    ``restart=true``（默认）时顺带留一个重启请求，托盘心跳看到就重启 burner——
    它只在自己启动时读配置，不重启的话这次保存对运行中的实例无效。重启由托盘
    异步完成，这里只承诺「已请求」，所以返回里带 ``restart_pending`` 让界面能提示。
    """
    state_file, config_file = _burner_paths()
    try:
        patch = burner_service.validate_patch(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "burner_config_invalid"}},
        ) from exc
    try:
        changed = burner_service.write_config(config_file, patch)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": {
                "message": f"消耗器配置写入失败，本次改动已取消：{exc}",
                "type": "burner_config_write_failed"}},
        ) from exc
    logger.info("burner config updated: %s", ", ".join(sorted(changed)))
    if restart:
        with contextlib.suppress(OSError):
            burner_service.request_restart(state_file.parent)
        logger.info("burner restart requested (tray will pick it up within ~3s)")
    return burner_service.snapshot(state_file, config_file)

# --------------------------------------------------------------------------- #
# 消耗器：账号核对与费率校准
#
# 运营者从商汤后台看回真实用量后从这里写回。三个端点：
#   GET  /admin/burner/reconcile   当前账本 + 配置的结构化快照（表单初值）
#   POST /admin/burner/reconcile   只读预演，返回逐账号 diff（保存前确认弹窗）
#   PUT  /admin/burner/reconcile   真正写账本 + burner.yaml，并重启
#   POST /admin/burner/calibrate   按实扣积分反推费率（采纳后走 reconcile 落盘）
# --------------------------------------------------------------------------- #
@router.get("/burner/reconcile", summary="账号核对的当前快照（账本 + 配置）")
async def burner_reconcile_state() -> dict[str, Any]:
    state_file, config_file = _burner_paths()
    config = burner_service.read_config(config_file)
    status = burner_service.read_status(state_file, config)
    now = time.time()
    cost = burner_service.estimate_request_cost(config)
    global_week_anchor = burner_service._parse_global_week_anchor(
        str(config.get("week_anchor") or "Mon 00:00"), now
    )
    accounts = [
        burner_service.account_view(a, now, global_week_anchor, status, cost)
        for a in status.accounts
    ]
    accounts.sort(key=lambda a: a["name"])
    return {
        "ok": True,
        "accounts": [
            {
                "name": a["name"],
                "credits_total": a["credits_total"],
                "burned_5h": a["burned_5h"],
                "burned_week": a["burned_week"],
                "cap_5h": a["cap_5h"],
                "cap_week": a["cap_week"],
                "anchor": burner_service._fmt_hhmm(
                    a["next_5h_boundary"]
                ) if a["next_5h_boundary"] else "",
                "week_anchor": burner_service._fmt_weekday_hhmm(
                    a["next_week_boundary"]
                ) if a["next_week_boundary"] else "",
                "is_burning": not a["parked"],
                "parked": a["parked"],
            }
            for a in accounts
        ],
        "config": {
            "only": burner_service.only_to_set(str(config.get("only") or "")),
            "anchors": burner_service.anchors_to_accounts(str(config.get("anchors") or "")),
            "week_anchors": burner_service.week_anchors_to_accounts(
                str(config.get("week_anchors") or "")
            ),
            "rate_in": status.rate_in,
            "rate_out": status.rate_out,
            "window_credits": status.window_credits,
            "weekly_credits": status.weekly_credits,
            "safety_margin": status.safety_margin,
        },
        "ledger_saved_at": status.saved_at,
        "restart_keys": list(burner_service.RECONCILE_RESTART_KEYS),
        "warnings": burner_service._warnings(config, status, accounts),
    }


@router.post("/burner/reconcile", summary="账号核对预演（只读，不写盘）")
async def burner_reconcile_preview(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    state_file, config_file = _burner_paths()
    try:
        diff = burner_service.reconcile_diff(
            state_file, burner_service.read_config(config_file), payload
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "burner_reconcile_invalid"}},
        ) from exc
    return {"ok": True, **diff}


@router.put("/burner/reconcile", summary="账号核对落盘（账本 + 配置 + 重启）")
async def burner_reconcile_apply(
    payload: dict[str, Any] = Body(...),
    restart: Annotated[bool, Query(description="写入后重启消耗器")] = True,
) -> dict[str, Any]:
    state_file, config_file = _burner_paths()
    try:
        result = burner_service.apply_reconcile(state_file, config_file, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "burner_reconcile_invalid"}},
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": {
                "message": f"写入失败：{exc}",
                "type": "burner_reconcile_write_failed"}},
        ) from exc
    logger.info(
        "burner reconciled: ledger=%s config=%s",
        ",".join(result["ledger_written"]) or "-",
        ",".join(result["config_written"]) or "-",
    )
    restart_result: dict[str, Any] = {"mode": "none", "ok": False, "message": "未请求重启"}
    if restart:
        restart_result = burner_service.request_restart_ex(state_file.parent)
    snapshot = burner_service.snapshot(state_file, config_file)
    return {
        "ok": True,
        "reconcile": result,
        "restart": restart_result,
        "snapshot": snapshot,
    }


@router.post("/burner/calibrate", summary="按实扣积分反推费率")
async def burner_calibrate(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    state_file, config_file = _burner_paths()
    try:
        actual = float(payload.get("actual_credits") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": "实扣积分要填数字"}},
        ) from exc
    try:
        result = burner_service.suggest_calibration(
            state_file,
            burner_service.read_config(config_file),
            actual,
            str(payload.get("account") or "").strip(),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"message": str(exc), "type": "burner_calibrate_invalid"}},
        ) from exc
    # 采纳时把建议费率并进 reconcile 的 payload——两条路径共用同一套写入与重启，
    # 否则会出现「校准算了但没写」或「写了但没重启」两条分叉。
    if payload.get("adopt"):
        try:
            burner_service.apply_reconcile(
                state_file,
                config_file,
                {"rate_in": result["suggested"]["rate_in"],
                 "rate_out": result["suggested"]["rate_out"]},
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": {"message": str(exc), "type": "burner_calibrate_invalid"}},
            ) from exc
        result["adopted"] = True
        result["restart"] = burner_service.request_restart_ex(state_file.parent)
    else:
        result["adopted"] = False
    return {"ok": True, "calibration": result}


@router.post("/burner/stop", summary="停止消耗器")
async def burner_stop() -> dict[str, Any]:
    state_file, _ = _burner_paths()
    result = burner_service.stop_burner(state_file.parent)
    logger.info("burner stop requested: %s", result.get("message", ""))
    return {"ok": result["ok"], **result}


@router.post("/burner/start", summary="启动消耗器")
async def burner_start() -> dict[str, Any]:
    state_file, _ = _burner_paths()
    result = burner_service.start_burner(state_file.parent)
    logger.info("burner start requested: %s", result.get("message", ""))
    return {"ok": result["ok"], **result}


@router.get("/burner/running", summary="消耗器是否在运行")
async def burner_running() -> dict[str, Any]:
    state_file, _ = _burner_paths()
    running = burner_service.is_burner_running(state_file.parent)
    mode = burner_service.detect_restart_mode(state_file.parent)
    return {"running": running, "mode": mode}
