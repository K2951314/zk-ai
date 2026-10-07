"""账号核对与校准：把运营者从商汤后台看回来的真实消耗写回账本。

拆自 burner_service.py。为什么需要它：API 给不出池级信号（AGENTS.md 固化的
结论），账本的 burned_5h / burned_week 全是按费率估算的。运营者有空时登陆
商汤控制台看到真实用量，回来需要一个入口把「真实」写回去——否则窗口估算
漂移之后，要么提前停靠（少烧），要么把专属池烧穿（静默溢出扣 K3 的通用池，
不可逆）。

两条硬约束：
1. **窗口口径必须连续**：写回 burned 时不重建 events，而是注入一个校准事件
   把账本估算拉到运营者给的数上，events 的时间戳序列仍单调——否则重启后
   burner 按「最近 5h 的 events」重算，新旧口径在边界上跳变。
2. **账本不许反过来覆盖显式配置**：费率优先级（命令行 > burner.yaml >
   账本）已由 burn_sensenova 的 _rate_fields 守着；这里写入只碰 burner.yaml
   里没有的键，yaml 有的以 yaml 为准。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from app.services.burner_service.config_io import read_config, write_config
from app.services.burner_service.status import (
    WIN_5H,
    WIN_WEEK,
    _parse_global_week_anchor,
    account_view,
    estimate_request_cost,
    read_status,
)

#: 运营者能改、且改完必须重启才会生效的 burner.yaml 键。
RECONCILE_RESTART_KEYS = ("anchors", "week_anchors", "only", "account_groups")

#: 单个账号可核对的数字字段（单位 = 积分）。
#: 前端现在填的是「剩余」（left_5h / left_week），后端内部换算成 burned。
_ACCOUNT_NUMERIC = ("credits_total", "left_5h", "left_week")
#: 前端旧字段名（burned_5h / burned_week），向后兼容。
_ACCOUNT_NUMERIC_LEGACY = ("burned_5h", "burned_week")

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _fmt_hhmm(ts: float) -> str:
    """5h 窗口锚点的显示值。和周锚点统一用 MM-DD HH:MM 格式。"""
    if not ts:
        return ""
    lt = time.localtime(ts)
    return f"{lt.tm_mon:02d}-{lt.tm_mday:02d} {lt.tm_hour:02d}:{lt.tm_min:02d}"


def _fmt_weekday_hhmm(ts: float) -> str:
    """epoch -> 周锚点表单的显示值。默认给 MM-DD HH:MM（2026-10-07 改）：
    运营者看到的是商汤控制台「下次重置时间」那一列，照抄即可；
    星期几得自己推，是「看不懂怎么填」的直接原因。"""
    if not ts:
        return ""
    lt = time.localtime(ts)
    return f"{lt.tm_mon:02d}-{lt.tm_mday:02d} {lt.tm_hour:02d}:{lt.tm_min:02d}"


def _parse_hhmm(spec: str) -> float:
    """5h 窗口锚点解析。支持中文和数字格式：
      - "10月7日 23:10"（中文，推荐——照抄控制台）
      - "10-07 23:10" / "10/7 23:10"（数字分隔）
      - "HH:MM"（旧）：取今天该时刻。
    """
    from scripts.burn_sensenova import parse_window_anchor

    text = (spec or "").strip()
    if not text:
        return 0.0
    try:
        return parse_window_anchor(text)
    except ValueError as exc:
        raise ValueError(
            f"锚点 {spec!r} 要是 '10月7日 23:10'、'10-07 23:10' 或 'HH:MM'"
        ) from exc


def _parse_weekday_hhmm(spec: str) -> float:
    """周锚点解析。支持中文、数字和旧格式：
      - "10月9日 18:10"（中文，推荐——照抄控制台）
      - "10-09 18:10" / "10/9 18:10"（数字分隔）
      - "Wed 18:10"（旧格式）。
    """
    from scripts.burn_sensenova import parse_week_anchor

    text = (spec or "").strip()
    if not text:
        return 0.0
    try:
        return parse_week_anchor(text)
    except ValueError as exc:
        raise ValueError(
            f"周锚点 {spec!r} 要是 '10月9日 18:10'、'10-09 18:10' 或 'Wed 18:10'"
        ) from exc


def _key_index(name: str) -> int | None:
    """'SENSENOVA_API_KEY_02' -> 2；不带后缀的 -> 1；不认识 -> None。"""
    if name == "SENSENOVA_API_KEY":
        return 1
    if name.startswith("SENSENOVA_API_KEY_"):
        suffix = name.rsplit("_", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
    return None


def _account_key_name(index: int) -> str:
    """账号序号 -> burner.yaml 的 only / anchors 里写的 Key 名。"""
    return "SENSENOVA_API_KEY" if index == 1 else f"SENSENOVA_API_KEY_{index:02d}"


def account_anchor_ts(config: dict[str, Any], name: str, now: float) -> float:
    """这个账号真正生效的 5h 锚点（按账号 anchors 优先，落回全局 anchor ）。
    为什么不直接给布尔：表单要显示管理员编辑的那个锚点值。"""
    per_account = anchors_to_accounts(str(config.get("anchors") or ""))
    spec = (per_account.get(name) or "").strip()
    if not spec:
        spec = str(config.get("anchor") or "").strip()
    if not spec:
        return 0.0
    try:
        return _parse_hhmm(spec)
    except ValueError:
        return 0.0


def account_week_anchor_ts(config: dict[str, Any], name: str, now: float) -> float:
    """这个账号真正生效的 周锚点（week_anchors 优先，落回全局 week_anchor ）。
    为什么不直接给布尔：表单要显示管理员编辑的那个锚点值。"""
    per_account = week_anchors_to_accounts(str(config.get("week_anchors") or ""))
    spec = (per_account.get(name) or "").strip()
    if not spec:
        spec = str(config.get("week_anchor") or "").strip()
    if not spec:
        return 0.0
    try:
        return _parse_weekday_hhmm(spec)
    except ValueError:
        return 0.0


def anchors_to_accounts(spec: str) -> dict[str, str]:
    """'2=15:10;3=15:10' -> {Key 名: 'HH:MM'}；坏项跳过。"""
    out: dict[str, str] = {}
    for item in filter(None, (s.strip() for s in (spec or "").split(";"))):
        num_s, sep, hm = item.partition("=")
        if not sep or not num_s.strip().isdigit():
            continue
        out[_account_key_name(int(num_s.strip()))] = hm.strip()
    return out


def week_anchors_to_accounts(spec: str) -> dict[str, str]:
    """'2=Wed 18:10' -> {Key 名: 'Wed 18:10'}。"""
    out: dict[str, str] = {}
    for item in filter(None, (s.strip() for s in (spec or "").split(";"))):
        num_s, sep, when = item.partition("=")
        if not sep or not num_s.strip().isdigit():
            continue
        out[_account_key_name(int(num_s.strip()))] = when.strip()
    return out


def accounts_to_anchors(mapping: dict[str, str]) -> str:
    """反过来序列化成 burner.yaml 的字符串，按账号序号排序保证可 diff。"""
    parts: list[tuple[int, str]] = []
    for name, hm in mapping.items():
        idx = _key_index(name)
        if idx is None or not (hm or "").strip():
            continue
        parts.append((idx, f"{idx}={hm.strip()}"))
    return ";".join(text for _, text in sorted(parts))


def only_to_set(spec: str) -> list[str]:
    """'K_02,K_03' -> ['SENSENOVA_API_KEY_02', ...]（容忍空格/分号）。"""
    return [raw for raw in re.split(r"[,;\s]+", (spec or "").strip()) if raw]


def set_to_only(names: list[str]) -> str:
    return ",".join(str(n) for n in names)


def reconcile_diff(
    state_file: Path, config: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """逐账号比对，返回 {accounts, config_changes, restart_keys, current}。

    只读不写盘——前端拿它做「保存前确认」弹窗的内容。
    """
    status = read_status(state_file, config)
    now = time.time()
    cost = estimate_request_cost(config)
    global_week_anchor = _parse_global_week_anchor(
        str(config.get("week_anchor") or "Mon 00:00"), now
    )
    by_name = {a.name: a for a in status.accounts}
    rows: list[dict[str, Any]] = []
    for item in payload.get("accounts") or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        acct = by_name.get(name)
        if acct is None:
            raise ValueError(
                f"账本里没有账号 {name!r}；可用：" + ", ".join(sorted(by_name))
            )
        view = account_view(acct, now, global_week_anchor, status, cost)
        row: dict[str, Any] = {"name": name}
        # 前端填「剩余」，后端内部换算成 burned 做比对。
        # left_5h -> burned_5h = cap_5h - left_5h；left_week 同理。
        left_to_burned = {
            "left_5h": float(view.get("cap_5h") or 0.0),
            "left_week": float(view.get("cap_week") or 0.0),
        }
        for field_name in _ACCOUNT_NUMERIC:
            if field_name in left_to_burned:
                current_burned = float(view.get(f"burned_{field_name.split('_', 1)[1]}") or 0.0)
                current_left = left_to_burned[field_name] - current_burned
                raw = item.get(field_name)
                # 也兼容旧字段名 burned_5h / burned_week
                if raw is None or str(raw).strip() == "":
                    raw = item.get(f"burned_{field_name.split('_', 1)[1]}")
                if raw is None or str(raw).strip() == "":
                    row[field_name] = {
                        "current": round(current_left, 1), "filled": None, "delta": None
                    }
                    continue
                try:
                    filled_left = float(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{name} 的 {field_name} 不是数字：{raw!r}"
                    ) from exc
                filled_burned = left_to_burned[field_name] - filled_left
                delta = filled_burned - current_burned
                row[field_name] = {
                    "current": round(current_left, 1),
                    "filled": round(filled_left, 1),
                    "delta": round(delta, 1),
                    "notable": abs(delta) > 2000
                    and (current_burned <= 0 or abs(delta) / max(1.0, current_burned) > 0.05),
                }
            else:
                current = float(view.get(field_name) or 0.0)
                raw = item.get(field_name)
                if raw is None or str(raw).strip() == "":
                    row[field_name] = {
                        "current": round(current, 1), "filled": None, "delta": None
                    }
                    continue
                try:
                    filled = float(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{name} 的 {field_name} 不是数字：{raw!r}"
                    ) from exc
                delta = filled - current
                row[field_name] = {
                    "current": round(current, 1),
                    "filled": round(filled, 1),
                    "delta": round(delta, 1),
                    "notable": abs(delta) > 2000
                    and (current <= 0 or abs(delta) / current > 0.05),
                }
        row["anchor"] = {
            "current": _fmt_hhmm(acct.anchor_ts),
            "filled": str(item.get("anchor") or "").strip(),
        }
        row["week_anchor"] = {
            "current": _fmt_weekday_hhmm(acct.week_anchor_ts),
            "filled": str(item.get("week_anchor") or "").strip(),
        }
        row["parked"] = bool(acct.parked_until and now < acct.parked_until)
        row["burning"] = bool(item.get("burning", True))
        rows.append(row)

    config_changes: dict[str, Any] = {}
    only_names = payload.get("only")
    if only_names is not None:
        config_changes["only"] = set_to_only([str(n) for n in only_names])
    for field_name, key in (
        ("anchors", "anchors"),
        ("week_anchors", "week_anchors"),
    ):
        mapping = payload.get(field_name)
        if mapping:
            config_changes[key] = accounts_to_anchors(
                {str(k): str(v) for k, v in mapping.items()}
            )
    for field_name in ("rate_in", "rate_out"):
        if payload.get(field_name) not in (None, ""):
            try:
                config_changes[field_name] = float(payload[field_name])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{field_name} 不是数字：{payload[field_name]!r}"
                ) from exc

    restart_keys = sorted(k for k in config_changes if k in RECONCILE_RESTART_KEYS)
    return {
        "accounts": rows,
        "config_changes": config_changes,
        "restart_keys": restart_keys,
        "current": {
            "only": str(config.get("only") or ""),
            "anchors": str(config.get("anchors") or ""),
            "week_anchors": str(config.get("week_anchors") or ""),
            "rate_in": status.rate_in,
            "rate_out": status.rate_out,
        },
    }


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".reconcile.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    tmp.replace(path)


def apply_reconcile(
    state_file: Path, config_file: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    """把核对结果写进 burn_state.json + burner.yaml，返回改了什么的摘要。

    校验在动任何一个字节之前全部跑完（reconcile_diff 会抛 ValueError），
    所以「保存失败但写了一半」不会发生。
    """
    config = read_config(config_file)
    diff = reconcile_diff(state_file, config, payload)
    accounts_payload = {
        str(item.get("name") or "").strip(): item
        for item in payload.get("accounts") or []
        if str(item.get("name") or "").strip()
    }
    if not accounts_payload and not diff["config_changes"]:
        raise ValueError("没有可写入的内容：账号与配置都是空的")

    ledger_written: list[str] = []
    if accounts_payload:
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        accounts = data.setdefault("accounts", {})
        keys = data.setdefault("keys", {})
        now = time.time()
        for name, item in accounts_payload.items():
            blob = accounts.setdefault(name, {})
            events = [[float(ts), float(c)] for ts, c in blob.get("events") or []]
            window_start = now - WIN_5H
            burned_5h_now = sum(c for ts, c in events if ts > window_start)
            burned_week_now = sum(c for ts, c in events if ts > now - WIN_WEEK)
            # 前端填「剩余」，后端换算成 burned 注入校准事件。
            # cap 从配置算（与 status.py 的 account_view 同一算法）。
            from scripts.burn_sensenova import DEFAULT_SAFETY_MARGIN
            cap5h = float(config.get("window_credits") or 60000.0) * \
                float(config.get("safety_margin") or DEFAULT_SAFETY_MARGIN)
            capweek = float(config.get("weekly_credits") or 600000.0) * \
                float(config.get("safety_margin") or DEFAULT_SAFETY_MARGIN)
            for field_name, current, cap in (
                ("left_5h", burned_5h_now, cap5h),
                ("left_week", burned_week_now, capweek),
            ):
                raw = item.get(field_name)
                # 兼容旧字段名 burned_5h / burned_week
                if raw is None or str(raw).strip() == "":
                    legacy = f"burned_{field_name.split('_', 1)[1]}"
                    raw = item.get(legacy)
                if raw is None or str(raw).strip() == "":
                    continue
                filled_left = float(raw)
                filled_burned = cap - filled_left
                gap = filled_burned - current
                if abs(gap) < 0.5:
                    continue
                events.append([now, round(gap, 4)])
                ledger_written.append(f"{name}.{field_name}")
            raw_total = item.get("credits_total")
            if raw_total not in (None, ""):
                blob["credits_total"] = round(float(raw_total), 2)
                ledger_written.append(f"{name}.credits_total")
            anchor = str(item.get("anchor") or "").strip()
            if anchor:
                blob["anchor_ts"] = _parse_hhmm(anchor)
                ledger_written.append(f"{name}.anchor_ts")
            week_anchor = str(item.get("week_anchor") or "").strip()
            if week_anchor:
                blob["week_anchor_ts"] = _parse_weekday_hhmm(week_anchor)
                ledger_written.append(f"{name}.week_anchor_ts")
            if item.get("burning") is False:
                # 「暂停这个账号」= 停靠一个窗口周期（burner 的 parked_until 语义）
                blob["parked_until"] = now + WIN_5H
                ledger_written.append(f"{name}.parked_until")
            blob["events"] = events[-20000:]
            accounts[name] = blob
            keys.setdefault(name, {})
        data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data["reconciled_at"] = now
        _atomic_write_json(state_file, data)

    # 账号级的 anchor / week_anchor 也要同步到 burner.yaml 的 anchors / week_anchors，
    # 否则消耗器重启后从配置文件读到旧值，账本里的新锚点被覆盖（2026-10-07 事故）。
    extra_config: dict[str, Any] = {}
    anchor_map = week_anchors_to_accounts(str(config.get("anchors") or ""))
    week_map = week_anchors_to_accounts(str(config.get("week_anchors") or ""))
    for name, item in accounts_payload.items():
        anchor_val = str(item.get("anchor") or "").strip()
        if anchor_val:
            anchor_map[name] = anchor_val
        week_val = str(item.get("week_anchor") or "").strip()
        if week_val:
            week_map[name] = week_val
    if anchor_map:
        new_anchors = accounts_to_anchors(anchor_map)
        if new_anchors != str(config.get("anchors") or ""):
            extra_config["anchors"] = new_anchors
    if week_map:
        new_week_anchors = accounts_to_anchors(week_map)
        if new_week_anchors != str(config.get("week_anchors") or ""):
            extra_config["week_anchors"] = new_week_anchors

    config_written: list[str] = []
    all_config_changes = {**diff["config_changes"], **extra_config}
    if all_config_changes:
        config_written = write_config(config_file, all_config_changes)

    # extra_config 里的 anchors/week_anchors 也是重启键，要补进 restart_keys
    restart_keys = sorted(
        set(diff["restart_keys"])
        | {k for k in extra_config if k in RECONCILE_RESTART_KEYS}
    )

    return {
        "ledger_written": ledger_written,
        "config_written": config_written,
        "restart_keys": restart_keys,
        "accounts": diff["accounts"],
    }


def _resolve_account_name(account: str, available: list[str]) -> str:
    """把简称（S_02 / S_01）还原成账本里的全名。

    前端显示简称省屏幕，但后端按全名查。允许用户填简称是「表单友好」的一部分。
    """
    account = (account or "").strip()
    if not account:
        return ""
    # 精确匹配优先
    if account in available:
        return account
    # 简称匹配：S_02 → SENSENOVA_API_KEY_02, S_01 → SENSENOVA_API_KEY_01
    m = re.fullmatch(r"S_(\d{1,2})", account, re.IGNORECASE)
    if m:
        idx = int(m.group(1))
        full = _account_key_name(idx)
        if full in available:
            return full
    return account  # 返回原值让上游报「找不到」


def suggest_calibration(
    state_file: Path, config: dict[str, Any], actual_credits: float, account: str = ""
) -> dict[str, Any]:
    """按账本 token 量反推费率（与 burn_sensenova.suggest_rates 同一口径）。"""
    if actual_credits <= 0:
        raise ValueError("实扣积分要大于 0")
    status = read_status(state_file, config)
    if account:
        resolved = _resolve_account_name(account, [a.name for a in status.accounts])
        members = [a for a in status.accounts if a.name == resolved]
        if not members:
            raise ValueError(
                f"账本里没有账号 {account!r}；可用：" + ", ".join(a.name for a in status.accounts)
            )
        tin = sum(a.tokens_in for a in members)
        tout = sum(a.tokens_out for a in members)
    else:
        tin = sum(a.tokens_in for a in status.accounts)
        tout = sum(a.tokens_out for a in status.accounts)
    denom = tout + tin / 3
    if denom <= 0:
        raise ValueError("账本里这段时段没有任何 token，无法反推费率")
    r_out = actual_credits * 1e6 / denom
    return {
        "suggested": {"rate_in": round(r_out / 3, 2), "rate_out": round(r_out, 2)},
        "current": {"rate_in": status.rate_in, "rate_out": status.rate_out},
        "actual_credits": round(actual_credits, 2),
        "account": account or "",
        "ledger_tokens": {"tokens_in": tin, "tokens_out": tout},
        "ledger_saved_at": status.saved_at,
        "caveat": (
            "反推用的是账本累计 token；实扣积分必须覆盖同一时段，"
            "否则两者口径不一致，算出来的费率会偏高或偏低。"
        ),
    }


__all__ = [
    "RECONCILE_RESTART_KEYS",
    "_fmt_hhmm",
    "_fmt_weekday_hhmm",
    "account_anchor_ts",
    "account_week_anchor_ts",
    "accounts_to_anchors",
    "anchors_to_accounts",
    "apply_reconcile",
    "only_to_set",
    "reconcile_diff",
    "set_to_only",
    "suggest_calibration",
    "week_anchors_to_accounts",
]
