#!/usr/bin/env python3
"""商汤 sensenova-6.8-flash-lite 积分消耗器（后台常驻，积分池感知 + 自适应并发）。

商汤 Token Plan 的积分池规则（为什么只敢烧到「专属池窗口上限」为止）：

  Flash-lite 模型扣减顺序：
    1. Flash-lite 专属池 · 周期积分      ← 只有烧这里才有 1:1 折算回充
    2. 不足时消耗 通用池 · 周期积分      ← 这是 kimi-k3 的口粮，烧了纯亏
    3. 仍不足时消耗 通用池 · 活动固定积分 ← 同上
  其他模型（kimi-k3 等）只扣通用池。池切到下一步时请求照常成功、完全无感，
  API 不返回任何「当前扣的哪个池」的信号（已实测：响应头/body 无池信息，
  兼容层也没有余额查询端点），所以唯一安全的做法是**按积分记账、预算熔断**。

预算模型（2026-09-19 定稿，修「烧穿专属池溢出」事故）：
  - 5h 窗口：滚动记账（未配 --anchors 时），窗口上限只防瞬时爆发
  - 周窗口：**固定窗口记账**——从 --week-anchor（默认周一 00:00）起累计，
    到线停靠到下周锚点。旧版按滚动 7 天记账会「遗忘」一周前的消耗，且
    默认费率取实测区间下沿，按烧速推算熔断线物理上永远触不到 → 专属池
    被烧穿后无感溢出扣通用池（2026-09 实测事故：26h 烧 ≈13 万积分/账号，
    真实费率在区间上沿时周实扣正好贴近官方 60 万上限）
  - 安全系数默认 0.45：内建 2 倍费率不确定性（实测区间 出333~720，估算
    取 360），估算口径熔断时实扣也不超过官方上限的一半
  - 绝对上限 --pool-total-credits：账号累计（持久化账本口径）烧到即永久
    停靠，防赠送池过期后空转。0 = 关闭
  宁可少烧（少转换），绝不溢出（烧 K3 口粮）。

速度模型（2026-09-12 实测）：
  - 单流生成速度固定 ~70 tok/s，总速率 = 70 × 在飞请求数 → 唯一杠杆是并发
  - 12 并发时 429 极少（47 分钟仅 4 次），离供应商上限很远 → 用 AIMD 自适应：
    每账号从 --per-account-start 起步，每成功 5 条升 1 档，撞 429 目标减半，
    上限 --per-account-max。自动找到每个账号的可持续并发点
  - 在飞请求的成本先记账（dispatch 时按预估扣，完成时按实扣修正），
    高并发下也不会超订阅 5h 窗口

费率（2026-09-24 三次控制台读数交叉校准，覆盖 09-12 的旧结论）：
  旧默认 入120 / 出360 **偏低约 7 倍**，是 2026-09 「烧穿专属池」事故的根因之一
  （熔断线按错误费率推算，物理上永远触不到）。现默认 入830 / 出2500 积分/百万token：
  用控制台「本周剩余」的两次读数差反推，两个账号各自算出 2516 / 2441（相差 3%），
  按 r_in = r_out/3 摊后取整。费率写进 data/burn_state.json，重启后自动恢复。
  注意：网关侧的 flash-lite 流量也扣同一个池，校准前要确认它当时没有在跑。

窗口刷新模型（2026-09-13 与 zk-k3 讨论定稿）：
  - 控制台显示每池的「重置时间」且每账号不同。两种可能：a) 固定锚点窗口
    （边界对齐某时刻，重置时刻相对固定）；b) 滚动窗口（控制台的「重置时间」
    = 当前窗口烧量全部过期的时刻 ≈ 我们烧干它的时间 + 5h，随烧随变）。
  - API 对此零信号，「扣专属池成功」与「扣通用池成功」观测不可区分，
    纯自动识别在信息论上不可行。两种模型安全性不对称：锚点误判为滚动 =
    少烧（安全）；滚动误判为锚点 = 烧穿（亏 K3 口粮）。故默认滚动模型。
  - 确认是固定锚点后：--anchors "1=03:30;3=07:15"（数字=Key 序号，1 即
    SENSENOVA_API_KEY）切换为固定窗口爆发模式（边界后满血烧干再停靠），
    锚点持久化进 burn_state.json，CLI 传入优先于持久化值。

用法：
    .venv\\Scripts\\python.exe scripts\\burn_sensenova.py              # 常驻烧
    .venv\\Scripts\\python.exe scripts\\burn_sensenova.py --once       # 每把 Key 各烧一次（自检）
    .venv\\Scripts\\python.exe scripts\\burn_sensenova.py --help      # 全部参数
    scripts\\start_burner.cmd                                          # 双击：最小化后台窗口
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "data" / "burn_sensenova.log"
STATE_FILE = ROOT / "data" / "burn_state.json"
# 本地配置入口（gitignore，可提交模板 config/burner.example.yaml）。
# 存在时其中的键会作为默认值，命令行显式传入的仍然优先——这样「双击启动」
# 和「临时加参数」两条路都能走，控制台/锚点改动不用再记命令行。
CONFIG_FILE = ROOT / "config" / "burner.yaml"

FILLER = "下面是一段用于接口压测的填充材料，请直接忽略它的内容，不要评论它。"

# 只有 Flash-lite 家族的模型扣「专属池周期积分」，而专属池才是唯一能 1:1 折算
# 回充成 K3 可用积分的池。换成别的模型（kimi-k3 / glm / deepseek 等）会直接扣
# 通用池——那是 kimi-k3 的口粮，烧了纯亏，且 API 零信号、事后无从发现。
# 所以启动时硬校验：模型名不含 "flash-lite" 就拒绝跑。
FLASH_LITE_REQUIRED = "flash-lite"


def is_flash_lite(model: str) -> bool:
    return FLASH_LITE_REQUIRED in model.lower()

# 额度耗尽的强特征词；命中且不带「频率/限流」字样才长停靠，避免把普通 429 判成额度用尽
QUOTA_STRONG = ("quota", "余额", "欠费", "arrears", "insufficient", "exhaust",
                "用尽", "已用完", "余额不足")
FREQ_HINTS = ("rate limit", "限流", "频率", "too many", "并发", "requests per",
              "rpm", "tps", "throttl")

WIN_5H = 5 * 3600
WIN_WEEK = 7 * 86400


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class AccountState:
    """积分预算与并发按账号记账（同账号的多把 Key 共享一份池与限流）。"""

    name: str
    events: deque = field(default_factory=deque)  # (timestamp, credits) 每次成功扣减
    parked_until: float = 0.0
    park_reason: str = ""
    credits_total: float = 0.0
    # AIMD 自适应并发：429 减半；静默（无 429）每分钟 +1，自动贴住供应商上限
    inflight: int = 0
    target: float = 8.0
    last_429: float = 0.0
    inflight_cost: float = 0.0  # 在飞请求的预估积分（完成时用实扣修正）
    # 固定锚点窗口（可选）：anchor_ts 是控制台「重置时间」对应的 epoch，
    # 边界 = anchor_ts + k*5h。=0 表示未配置，走滚动窗口模型（保守、安全）。
    anchor_ts: float = 0.0
    # 按账号的周锚点（可选）：控制台「周刷新」对应的 epoch。0 = 用全局
    # --week-anchor。各账号周刷新时刻不同（实测 = 创建时刻 + N×7天），
    # 全局单值会让早刷新的账号被少算、晚刷新的被多算。
    week_anchor_ts: float = 0.0

    def week_start(self, now: float, fallback: float = 0.0) -> float:
        """本周固定窗口起点：优先本账号的 week_anchor_ts，否则用全局锚点。
        两者都为 0 → 返回 0，调用方退回滚动 7 天记账（逃生口）。"""
        base = self.week_anchor_ts or fallback
        if not base:
            return 0.0
        k = int((now - base) // WIN_WEEK)
        return base + k * WIN_WEEK

    def window_start(self, now: float) -> float:
        """当前 5h 窗口的起点（仅锚点模式有意义）。"""
        if not self.anchor_ts:
            return 0.0
        k = int((now - self.anchor_ts) // WIN_5H)
        return self.anchor_ts + k * WIN_5H

    def next_boundary(self, now: float) -> float:
        return self.window_start(now) + WIN_5H if self.anchor_ts else 0.0

    def burned(self, now: float, window: float) -> float:
        return sum(c for ts, c in self.events if ts > now - window)

    def burned_since(self, start: float) -> float:
        """固定窗口记账：自 start（周锚点的当前窗口起点）起的累计消耗。"""
        return sum(c for ts, c in self.events if ts >= start)

    def available(self, now: float, cap5h: float, capweek: float,
                  week_start: float = 0.0) -> float:
        """窗口余量（已扣掉在飞请求的预估成本）。
        5h 约束：锚点模式按「当前固定窗口内烧量」，滚动模式按「最近 5h 烧量」。
        周约束：week_start>0 时按固定窗口（自周锚点累计，防「滚动遗忘」烧穿）；
        =0 时退回滚动 7 天（--week-anchor 显式置空的逃生口，不推荐）。"""
        if self.anchor_ts:
            ws = self.window_start(now)
            avail5h = cap5h - sum(c for ts, c in self.events if ts >= ws)
        else:
            avail5h = cap5h - self.burned(now, WIN_5H)
        if week_start:
            avail_week = capweek - self.burned_since(week_start)
        else:
            avail_week = capweek - self.burned(now, WIN_WEEK)
        return min(avail5h, avail_week) - self.inflight_cost

    def resume_time(self, now: float, cap5h: float, capweek: float, need: float,
                    week_start: float = 0.0) -> float:
        """最早什么时候各约束都能腾出 need 积分（取各约束要求时刻的最大值）。"""
        cands: list[float] = []
        if self.anchor_ts:
            ws = self.window_start(now)
            burned5 = sum(c for ts, c in self.events if ts >= ws)
            if cap5h >= need:
                if burned5 <= cap5h - need:
                    cands.append(now + 60)
                else:
                    # 窗口烧干：等到下一个边界（重置瞬间回满）
                    cands.append(ws + WIN_5H + 30)
            else:
                cands.append(now + 86400.0)
        else:
            # 滚动模型：把最老的事件滚出窗口来算
            evs5 = sorted((ts, c) for ts, c in self.events if ts > now - WIN_5H)
            total5 = sum(c for _, c in evs5)
            if cap5h < need:
                cands.append(now + 86400.0)
            else:
                need_release = total5 - (cap5h - need)
                if need_release <= 0:
                    cands.append(now + 60)
                else:
                    acc = 0.0
                    for ts, c in evs5:
                        acc += c
                        if acc >= need_release:
                            cands.append(ts + WIN_5H + 30)
                            break
                    else:
                        cands.append(now + 86400.0)
        # 周约束：固定窗口到线就停靠到下周锚点（滚动分支只是逃生口）
        if week_start:
            burned_w = self.burned_since(week_start)
            if capweek < need:
                cands.append(now + 86400.0)
            elif burned_w > capweek - need:
                cands.append(week_start + WIN_WEEK + 30)
        else:
            evs_w = sorted((ts, c) for ts, c in self.events if ts > now - WIN_WEEK)
            total_w = sum(c for _, c in evs_w)
            if capweek < need:
                cands.append(now + 86400.0)
            else:
                need_release = total_w - (capweek - need)
                if need_release > 0:
                    acc = 0.0
                    for ts, c in evs_w:
                        acc += c
                        if acc >= need_release:
                            cands.append(ts + WIN_WEEK + 30)
                            break
                    else:
                        cands.append(now + 86400.0)
        if not cands:
            cands.append(now + 60)
        return max(cands)


@dataclass
class KeyState:
    name: str
    key: str
    account: AccountState
    inflight: int = 0
    cooldown_until: float = 0.0
    streak: int = 0            # 连续 429 次数，决定本次冷却时长
    parked_until: float = 0.0  # Key 级停靠（仅用于 Key 失效等永久性问题）
    park_reason: str = ""
    attempts: int = 0
    ok: int = 0
    fail: int = 0
    rate_limited: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    last_err: str = ""

    def available(self, now: float) -> bool:
        return now >= self.cooldown_until and now >= self.parked_until


@dataclass
class Totals:
    tokens_in: int = 0
    tokens_out: int = 0
    credits: float = 0.0
    ok: int = 0
    fail: int = 0
    rate_limited: int = 0
    recent: deque = field(default_factory=lambda: deque(maxlen=16))
    global_pause_until: float = 0.0
    global_backoff_n: int = 0


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def htokens(n: float) -> str:
    if n >= 1e8:
        return f"{n / 1e8:.2f}亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f}万"
    return f"{n:.0f}"


def estimate_tokens(text: str) -> int:
    """粗估 token：CJK 按 1 字 1 token，其余按 3.5 字符 1 token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, int(cjk + (len(text) - cjk) / 3.5))


def build_messages(args: argparse.Namespace) -> list[dict]:
    # 每条请求的盐前缀都不同 → 服务端提示词缓存永远打不中，输入 token 全价计费
    salt = uuid.uuid4().hex
    filler = FILLER * max(1, args.filler_chars // len(FILLER))
    task = (
        "读完上面的材料后，请从 1 开始逐个报数，一直数到 999999，"
        "每行只写一个数字，不要省略、不要解释、不要总结、不要停止，"
        "直到写满你的全部输出额度为止。"
    )
    text = f"[压测编号 {salt}]\n{filler}\n\n{task}"
    return [{"role": "user", "content": text}]


def load_keys() -> list[KeyState]:
    names = sorted(
        name for name, val in os.environ.items()
        if name.startswith("SENSENOVA_API_KEY") and val.strip()
    )
    return [KeyState(name=n, key=os.environ[n].strip(), account=AccountState(name=n))
            for n in names]


def group_accounts(keys: list[KeyState], groups_spec: str) -> None:
    """把同账号的 Key 归到同一份预算。groups_spec 形如
    "SENSENOVA_API_KEY,SENSENOVA_API_KEY_02;SENSENOVA_API_KEY_03,SENSENOVA_API_KEY_04"
    （分号分组，逗号分 Key）；不在任何组里的 Key 各自独立记账。"""
    for group in filter(None, (g.strip() for g in groups_spec.split(";"))):
        members = [k for k in keys if k.name in {x.strip() for x in group.split(",")}]
        if len(members) > 1:
            shared = AccountState(name="+".join(m.name.replace("SENSENOVA_API_KEY", "K")
                                                for m in members))
            for m in members:
                m.account = shared


WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_week_anchor(spec: str) -> float:
    """解析 --week-anchor "Mon 00:00"，返回当前 7 天固定窗口的起点 epoch。

    取「最近一个（含今天）该星期几的 HH:MM」；若该时刻在今天还没到，
    则再往前推一周。确定性只依赖本地时钟，无需持久化。"""
    m = re.fullmatch(r"([A-Za-z]{3})\s*(\d{1,2}):(\d{2})", spec.strip())
    if not m or m.group(1).lower() not in WEEKDAY_NAMES:
        raise ValueError('应为 "Mon 00:00" 形式（星期几缩写 + HH:MM）')
    wd = WEEKDAY_NAMES.index(m.group(1).lower())
    hh, mm = int(m.group(2)), int(m.group(3))
    if hh > 23 or mm > 59:
        raise ValueError("HH:MM 时刻非法")
    lt = time.localtime()
    days_back = (lt.tm_wday - wd) % 7
    ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - days_back, hh, mm, 0, 0, 0, -1))
    if ts > time.time():
        ts -= WIN_WEEK
    return ts


#: 配置键 → argparse dest 的映射。值类型不同（bool/int/float/str），
#: 故不自动推断，逐键声明，写错类型会在这里炸而不是烧到一半才炸。
_CONFIG_KEYS: dict[str, type] = {
    "model": str,
    "base_url": str,
    "max_seconds": float,
    "once": bool,
    "concurrency": int,
    "per_account_max": int,
    "per_account_start": int,
    "max_tokens": int,
    "filler_chars": int,
    "window_credits": float,
    "weekly_credits": float,
    "safety_margin": float,
    "week_anchor": str,
    "week_anchors": str,
    "pool_total_credits": float,
    "rate_in": float,
    "rate_out": float,
    "quota_park_hours": float,
    "account_groups": str,
    "anchors": str,
    "cooldown_base": float,
    "cooldown_max": float,
    "connect_timeout": float,
    "read_timeout": float,
    "summary_interval": float,
    "only": str,
}


def load_config(path: Path | None = None) -> dict:
    """读 config/burner.yaml（gitignore 的本地配置入口）→ argparse dest 字典。

    文件不存在或读不了 → {}（不阻断启动：命令行与 .env 仍然有效）。
    未知键告警后忽略；类型不符直接抛错——宁可启动失败，也不要在
    烧到一半时才发现「安全系数被写成了字符串」。
    """
    cfg_path = path or CONFIG_FILE
    if not cfg_path.exists():
        return {}
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"ERROR: 读不了 {cfg_path}：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not isinstance(raw, dict):
        print(f"ERROR: {cfg_path} 顶层必须是映射（键: 值）", file=sys.stderr)
        raise SystemExit(2)
    out: dict = {}
    for key, value in raw.items():
        dest = str(key).replace("-", "_")
        want = _CONFIG_KEYS.get(dest)
        if want is None:
            print(f"WARN: {cfg_path} 里的 {key!r} 不是已知参数，已忽略", file=sys.stderr)
            continue
        if isinstance(value, bool) and want is not bool:
            print(f"ERROR: {cfg_path} 的 {key} 应是 {want.__name__}，"
                  f"却给了布尔值 {value!r}", file=sys.stderr)
            raise SystemExit(2)
        if not isinstance(value, want):
            # int 接受 float 的整数值（YAML 里 60000 也常常被写成 6e4）
            if want is float and isinstance(value, int):
                value = float(value)
            else:
                print(f"ERROR: {cfg_path} 的 {key} 应是 {want.__name__}，"
                      f"却是 {type(value).__name__}", file=sys.stderr)
                raise SystemExit(2)
        out[dest] = value
    return out


def parse_week_anchors(spec: str) -> dict[int, float]:
    """解析 --week-anchors "2=Wed 18:10;10=Thu 09:36" → {Key序号: epoch}。

    时刻取「最近一个（含今天）该星期几的 HH:MM」，已过则再往前推一周
    （与 parse_week_anchor 同一套确定性规则）。各账号的周刷新时刻不同
    （实测 = 账号创建时刻 + N×7天），全局单值必然错配，故按账号给。
    """
    out: dict[int, float] = {}
    for item in filter(None, (s.strip() for s in spec.split(";"))):
        num_s, sep, when = item.partition("=")
        num_s, when = num_s.strip(), when.strip()
        if not sep or not num_s.isdigit():
            continue
        try:
            out[int(num_s)] = parse_week_anchor(when)
        except ValueError:
            continue
    return out


def apply_week_anchors(spec: str, keys: list[KeyState], log) -> int:
    """把 --week-anchors 应用到对应账号（数字 = Key 序号，同 apply_anchors）。"""
    if not spec.strip():
        return 0
    mapping = parse_week_anchors(spec)
    seen: set[int] = set()
    for idx, ts in sorted(mapping.items()):
        names = ["SENSENOVA_API_KEY"] if idx == 1 else [f"SENSENOVA_API_KEY_{idx:02d}"]
        target = next((k.account for k in keys if k.name in names), None)
        if target is None:
            log(f"周锚点项 {idx!r} 没有匹配到已加载的 Key，已跳过", "WARN")
            continue
        if target.week_anchor_ts and target.week_anchor_ts != ts:
            log(f"账号 {target.name} 已有周锚点，保留先出现的", "WARN")
            continue
        target.week_anchor_ts = ts
        seen.add(id(target))
    return len(seen)


def apply_anchors(spec: str, keys: list[KeyState], log) -> int:
    """把 "1=03:30;3=07:15" 形式的窗口重置时刻应用到对应账号。

    数字 = Key 序号（1 即不带后缀的 SENSENOVA_API_KEY，与 02 同账号）；
    同账号多把 Key 共用一个锚点，冲突时取先出现的并告警。
    时刻取今天该 HH:MM 作为一次真实边界（此后按 +5h 递推）。
    返回生效的账号数。"""
    seen: set[int] = set()
    for item in filter(None, (s.strip() for s in spec.split(";"))):
        num_s, sep, hm = item.partition("=")
        num_s, hm = num_s.strip(), hm.strip()
        if not sep or not num_s.isdigit() or not re.fullmatch(r"\d{1,2}:\d{2}", hm):
            log(f"锚点项 {item!r} 无法解析（应为 数字=HH:MM），已跳过", "WARN")
            continue
        idx = int(num_s)
        names = ["SENSENOVA_API_KEY"] if idx == 1 else [f"SENSENOVA_API_KEY_{idx:02d}"]
        target = next((k.account for k in keys if k.name in names), None)
        if target is None:
            log(f"锚点项 {item!r} 没有匹配到已加载的 Key，已跳过", "WARN")
            continue
        hh, mm = (int(x) for x in hm.split(":"))
        lt = time.localtime()
        try:
            ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
        except (OverflowError, ValueError):
            log(f"锚点项 {item!r} 时刻非法，已跳过", "WARN")
            continue
        if target.anchor_ts and target.anchor_ts != ts:
            log(f"账号 {target.name} 收到多个不同锚点，保留先出现的", "WARN")
            continue
        target.anchor_ts = ts
        seen.add(id(target))
    return len(seen)


# ---------------------------------------------------------------------------
# 消耗器主体
# ---------------------------------------------------------------------------


class Burner:
    def __init__(self, args: argparse.Namespace, keys: list[KeyState]):
        self.args = args
        self.keys = keys
        self.accounts = list({id(k.account): k.account for k in keys}.values())
        self.acct_keys: list[tuple[AccountState, list[KeyState]]] = []
        for acct in self.accounts:
            self.acct_keys.append((acct, [k for k in keys if k.account is acct]))
        for acct in self.accounts:
            acct.target = float(args.per_account_start)
        self.cap5h = args.window_credits * args.safety_margin
        self.capweek = args.weekly_credits * args.safety_margin
        # 周预算用固定窗口（自锚点累计），防滚动记账「遗忘」旧消耗后烧穿专属池
        self.week_anchor_ts = parse_week_anchor(args.week_anchor)
        # 绝对上限：账号累计烧到即永久停靠（0 = 关闭）
        self.cap_total = args.pool_total_credits
        self.total = Totals()
        self.stop = asyncio.Event()
        self.started = time.time()
        self.use_stream_options = True
        self.url = args.base_url.rstrip("/") + "/chat/completions"
        # 自动校准状态
        self._calibrating = False
        self._calibrate_start_in = 0
        self._calibrate_start_out = 0
        self.log_path = Path(args.log_file)
        self.state_file = Path(args.state_file)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # 本段运行的基线（恢复账本后「速率」只算本次运行新增的量）
        self.base_in = 0
        self.base_out = 0

    def week_start(self, now: float) -> float:
        """当前 7 天固定窗口的起点（周锚点 + k×7d）。"""
        k = int((now - self.week_anchor_ts) // WIN_WEEK)
        return self.week_anchor_ts + k * WIN_WEEK

    # ---- 预算 --------------------------------------------------------------
    def request_cost(self, messages: list[dict]) -> float:
        """单条请求的预估积分消耗（按估算输入 + max_tokens 输出算上限）。"""
        prompt_chars = sum(len(m["content"]) for m in messages)
        return (self.args.rate_in * estimate_tokens("字" * prompt_chars) / 1e6
                + self.args.rate_out * self.args.max_tokens / 1e6)

    def budget_allow(self, acct: AccountState, cost: float) -> bool:
        """预算够且没有停靠 → True。不够则把账号停靠到能腾出 cost 的时刻。
        在飞请求的预估成本已计入 available()，高并发下不会超订阅窗口。"""
        now = time.time()
        if now < acct.parked_until:
            return False
        # 绝对上限（按持久化账本的累计口径）：到线永久停靠，绝不溢出
        if self.cap_total > 0 and acct.credits_total >= self.cap_total:
            if acct.parked_until != float("inf"):
                acct.parked_until = float("inf")
                acct.park_reason = (f"累计 ≈{acct.credits_total:.0f} 积分已达绝对上限"
                                    f" {self.cap_total:.0f}（--pool-total-credits）")
                self.log(f"[账号 {acct.name}] {acct.park_reason}，永久停靠。"
                         "如确认池已重置，删除账本里该账号的 credits_total 后重启", "ERROR")
            return False
        if self.cap5h <= 0 and self.capweek <= 0:
            return True  # 预算显式关闭（危险，启动时已大声警告）
        ws = acct.week_start(now, self.week_anchor_ts)
        avail = acct.available(now, self.cap5h, self.capweek, ws)
        if avail >= cost:
            return True
        # 能走到这里说明账号此前未在停靠 → 这是一次新的停靠，记一条日志
        acct.parked_until = acct.resume_time(now, self.cap5h, self.capweek, cost, ws)
        acct.park_reason = f"预算触顶（窗口余 {avail:.0f} < 需 {cost:.0f} 积分）"
        self.log(f"[账号 {acct.name}] {acct.park_reason}，停靠至 "
                 f"{time.strftime('%m-%d %H:%M', time.localtime(acct.parked_until))} 后继续",
                 "WARN")
        return False

    # ---- 账本持久化：重启不清零，和控制台的记账口径保持连续 ----------------
    def save_state(self) -> None:
        import json

        data = {
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": {a.name: {"events": [[ts, c] for ts, c in a.events],
                                  "credits_total": a.credits_total,
                                  "target": a.target,
                                  "anchor_ts": a.anchor_ts,
                                  "week_anchor_ts": a.week_anchor_ts}
                         for a in self.accounts},
            "keys": {k.name: {"ok": k.ok, "fail": k.fail, "rate_limited": k.rate_limited,
                              "tokens_in": k.tokens_in, "tokens_out": k.tokens_out}
                     for k in self.keys},
            # 费率与安全系数必须一起落盘：_save_rates_to_state 先写、本函数后写，
            # 早先的整体覆盖把这两个键冲掉 → 重启后校准丢失、退回默认费率
            # （2026-09-24 实测踩到：真实费率比旧默认高近 7 倍都没能记住）。
            "rate_in": self.args.rate_in,
            "rate_out": self.args.rate_out,
            "safety_margin": self.args.safety_margin,
        }
        with contextlib.suppress(OSError):
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_file)

    def load_state(self) -> bool:
        """恢复上次（或上几次）的账本。返回是否加载到了历史数据。"""
        import json

        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        loaded = False
        by_name = {a.name: a for a in self.accounts}
        cutoff = time.time() - (WIN_WEEK + 86400)
        for name, blob in data.get("accounts", {}).items():
            acct = by_name.get(name)
            if acct is None:
                continue
            acct.events = deque((ts, c) for ts, c in blob.get("events", []) if ts > cutoff)
            acct.credits_total = float(blob.get("credits_total") or 0.0)
            # 恢复学到的并发目标（夹在 [起点, 本次数值上限] 内，避免跨配置残留）
            acct.target = min(max(float(blob.get("target") or self.args.per_account_start), 1.0),
                              float(self.args.per_account_max))
            # 锚点：CLI 传入的优先；未传时恢复上次持久化的
            if acct.anchor_ts == 0:
                acct.anchor_ts = float(blob.get("anchor_ts") or 0)
            if acct.week_anchor_ts == 0:
                acct.week_anchor_ts = float(blob.get("week_anchor_ts") or 0)
            loaded = True
        by_key = {k.name: k for k in self.keys}
        for name, blob in data.get("keys", {}).items():
            ks = by_key.get(name)
            if ks is None:
                continue
            ks.ok = int(blob.get("ok") or 0)
            ks.fail = int(blob.get("fail") or 0)
            ks.rate_limited = int(blob.get("rate_limited") or 0)
            ks.tokens_in = int(blob.get("tokens_in") or 0)
            ks.tokens_out = int(blob.get("tokens_out") or 0)
        # 全局总量 = 各 Key 之和（credits 从账号账本取）
        self.total.tokens_in = sum(k.tokens_in for k in self.keys)
        self.total.tokens_out = sum(k.tokens_out for k in self.keys)
        self.total.credits = sum(a.credits_total for a in self.accounts)
        self.total.ok = sum(k.ok for k in self.keys)
        self.total.fail = sum(k.fail for k in self.keys)
        self.total.rate_limited = sum(k.rate_limited for k in self.keys)
        return loaded

    # ---- 校准：把控制台实扣数换算成精确费率 --------------------------------
    def suggest_rates(self, actual_credits: float) -> tuple[float, float]:
        """按持久化账本里的全部 token 量反推费率（假设 r_in = r_out/3，
        该比例来自商汤参考价，即使偏差一倍对输出主导的烧法影响也很小）。"""
        tin = sum(k.tokens_in for k in self.keys)
        tout = sum(k.tokens_out for k in self.keys)
        denom = tout + tin / 3
        r_out = actual_credits * 1e6 / denom if denom else 0.0
        return r_out / 3, r_out

    # ---- 日志：控制台 + 文件双写 -----------------------------------------
    def log(self, msg: str, level: str = "INFO") -> None:
        line = f"{time.strftime('%H:%M:%S')} {level:<5} {msg}"
        print(line, flush=True)
        with contextlib.suppress(OSError), open(self.log_path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d ") + line + "\n")

    # ---- 选 Key：有容量的账号里挑在飞最少的，再挑该账号在飞最少的 Key ------
    def pick_key(self) -> KeyState | None:
        now = time.time()
        best: tuple[AccountState, KeyState] | None = None
        for acct, ks_list in self.acct_keys:
            if acct.inflight >= min(acct.target, self.args.per_account_max):
                continue
            if now < acct.parked_until:
                continue
            usable = [ks for ks in ks_list if ks.available(now)
                      and not (self.args.once and ks.attempts > 0)]
            if not usable:
                continue
            ks = min(usable, key=lambda k: k.inflight)
            if best is None or acct.inflight < best[0].inflight:
                best = (acct, ks)
        return best[1] if best else None

    def all_keys_dead(self) -> bool:
        """所有 Key 都永久失效（401 等）→ 没有任何恢复可能，任务结束。"""
        return all(ks.parked_until == float("inf") for ks in self.keys)

    # ---- 单次请求 ----------------------------------------------------------
    async def burn_once(self, client: httpx.AsyncClient, ks: KeyState) -> None:
        acct = ks.account
        ks.attempts += 1
        messages = build_messages(self.args)
        payload = {
            "model": self.args.model,
            "messages": messages,
            "max_tokens": self.args.max_tokens,
            "temperature": 0.6,
            "stream": True,
        }
        if self.use_stream_options:
            payload["stream_options"] = {"include_usage": True}
        headers = {"Authorization": f"Bearer {ks.key}"}

        text_parts: list[str] = []
        usage: dict | None = None
        try:
            async with client.stream("POST", self.url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")[:400]
                    self.on_error(ks, resp.status_code, body)
                    return
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj.get("usage"), dict):
                        usage = obj["usage"]
                    for ch in obj.get("choices", []):
                        delta = ch.get("delta") or {}
                        piece = delta.get("content") or delta.get("reasoning_content") or ""
                        if piece:
                            text_parts.append(piece)
        except httpx.HTTPError as exc:
            ks.fail += 1
            self.total.fail += 1
            ks.last_err = f"network: {exc!r:.120}"
            ks.cooldown_until = time.time() + 15
            self.log(f"[{ks.name}] 网络异常，冷却 15s：{exc!r:.160}", "WARN")
            return

        text = "".join(text_parts)
        if usage:
            tin = int(usage.get("prompt_tokens") or 0)
            tout = int(usage.get("completion_tokens") or 0)
        else:
            # 上游没回 usage 时按文本粗估（估的只影响显示，不影响烧的量）
            tin, tout = 0, estimate_tokens(text)
        credits = (self.args.rate_in * tin + self.args.rate_out * tout) / 1e6
        ks.ok += 1
        ks.streak = 0
        ks.tokens_in += tin
        ks.tokens_out += tout
        self.total.ok += 1
        self.total.tokens_in += tin
        self.total.tokens_out += tout
        self.total.credits += credits
        self.total.recent.append("ok")
        self.total.global_backoff_n = 0
        self.total.global_pause_until = 0.0
        # 记账：成功扣减的积分进账号窗口（只有成功响应才真的扣了积分）
        now = time.time()
        acct.events.append((now, credits))
        while acct.events and acct.events[0][0] <= now - (WIN_WEEK + 86400):
            acct.events.popleft()  # 裁剪：8 天外的记账点对任何窗口都无影响
        acct.credits_total += credits
        rate = (self.total.tokens_out - self.base_out) / max(1e-9, time.time() - self.started)
        self.log(
            f"[{ks.name}] +{htokens(tin)}入 +{htokens(tout)}出 ≈{credits:.1f}积分"
            f" | 累计 {htokens(self.total.tokens_in)}入 {htokens(self.total.tokens_out)}出"
            f" ≈{self.total.credits:.0f}积分 | 出均速 {htokens(rate)}/s"
            f" | 账号并发目标 {acct.target:.0f}"
        )

    # ---- 错误分类：限流 > Key 失效 > 额度耗尽 ------------------------------
    def on_error(self, ks: KeyState, status: int, body: str) -> None:
        low = body.lower()
        acct = ks.account
        self.total.recent.append("err")
        is_freq = any(h in low or h in body for h in FREQ_HINTS)

        if status == 429 or is_freq:
            ks.rate_limited += 1
            self.total.rate_limited += 1
            # AIMD 乘性减：撞 429 说明该账号并发顶到供应商上限，目标减半
            old_target = acct.target
            acct.target = max(1.0, acct.target * 0.5)
            acct.last_429 = time.time()
            if acct.target <= 1.0:
                # 并发已到底还 429：多为 RPM 窗口未清，短冷却试探即可，不再指数升级
                ks.streak = min(ks.streak + 1, 2)
                cd = self.args.cooldown_base
            else:
                ks.streak += 1
                cd = min(self.args.cooldown_base * 2 ** (ks.streak - 1), self.args.cooldown_max)
            ks.cooldown_until = time.time() + cd
            ks.last_err = f"429 x{ks.streak}"
            self.log(f"[{ks.name}] 429 限流，冷却 {cd:.0f}s，账号并发目标 {old_target:.0f}→{acct.target:.0f}")
            self.maybe_global_backoff()
            return

        if status in (401, 403):
            ks.parked_until = float("inf")
            ks.park_reason = f"HTTP {status} Key 无效/无权限"
            ks.fail += 1
            self.total.fail += 1
            ks.last_err = f"HTTP {status}"
            self.log(f"[{ks.name}] HTTP {status}，永久停靠：{body[:200]}", "ERROR")
            return

        # 额度/积分耗尽：说明专属池和通用池都空了（扣减顺序走到底才会报错），
        # 此时再打只会空转，长停靠等周期发放。任何状态码都可能带这种文案。
        if any(h in low or h in body for h in QUOTA_STRONG) and status in (402, 403, 429):
            acct.parked_until = time.time() + self.args.quota_park_hours * 3600
            acct.park_reason = "疑似额度/积分耗尽"
            ks.rate_limited += 1
            self.total.rate_limited += 1
            self.log(
                f"[{ks.name}] 疑似积分耗尽（专属池+通用池都已扣完），账号停靠 "
                f"{self.args.quota_park_hours:.0f}h（{body[:160]}）", "ERROR",
            )
            self.log(
                "⚠ 溢出实锤：走到这一步说明专属池此前已被烧穿、通用池/活动池已受损。"
                "请到商汤控制台核对「Flash-lite 专属池」的实际规模/重置时刻，用 "
                f"--weekly-credits / --safety-margin（当前 {self.args.safety_margin}）/ "
                "--week-anchor 收紧预算，或 --pool-total-credits 设绝对上限",
                "ERROR",
            )
            return

        # 5xx / 其他：短冷却换个 Key 顶上
        ks.fail += 1
        self.total.fail += 1
        ks.last_err = f"HTTP {status}"
        ks.cooldown_until = time.time() + 20
        self.log(f"[{ks.name}] HTTP {status}，冷却 20s：{body[:200]}", "WARN")

    def maybe_global_backoff(self) -> None:
        r = self.total.recent
        # 最近 16 次请求全是失败且没有在飞请求 → 疑似所有账号都不在免费窗口，
        # 全局退避，避免空转刷 429
        if r.maxlen is None or len(r) < r.maxlen or any(x == "ok" for x in r):
            return
        if self.total.global_pause_until > time.time():
            return
        if any(acct.inflight > 0 for acct in self.accounts):
            return
        self.total.global_backoff_n += 1
        pause = min(600, 30 * 2 ** (self.total.global_backoff_n - 1))
        self.total.global_pause_until = time.time() + pause
        self.total.recent.clear()
        self.log(f"所有 Key 都在限流且暂无在飞请求，全局退避 {pause:.0f}s", "WARN")

    # ---- 自动校准：烧够阈值后暂停，查实扣，算精确费率，继续 ----------------
    def _maybe_auto_calibrate(self) -> None:
        """烧够 --auto-calibrate 指定的 token 量后，暂停消耗并提示用户校准。"""
        if not self.args.auto_calibrate or self._calibrating:
            return
        burned_tokens = (self.total.tokens_in - self._calibrate_start_in
                         + self.total.tokens_out - self._calibrate_start_out)
        if burned_tokens < self.args.auto_calibrate * 1e6:
            return
        self._calibrating = True
        self.save_state()
        self.log("=" * 72, "WARN")
        self.log("自动校准：已烧够 "
                 f"{self.args.auto_calibrate:.0f}M token（本段 "
                 f"{htokens(self.total.tokens_in - self._calibrate_start_in)}入 "
                 f"{htokens(self.total.tokens_out - self._calibrate_start_out)}出），"
                 "消耗已暂停。", "WARN")
        self.log("请去商汤控制台「积分消耗明细」查本时段实扣积分，"
                 "然后运行：", "WARN")
        self.log("  python scripts/burn_sensenova.py --calibrate-actual <实扣积分>", "WARN")
        self.log("算出的费率写入账本后，重新启动消耗器即可继续（费率持久化，"
                 "重启不丢）。", "WARN")
        self.log("=" * 72, "WARN")

    def apply_calibrated_rates(self, r_in: float, r_out: float) -> None:
        """把校准后的费率写进 args 并持久化到账本。"""
        old_in, old_out = self.args.rate_in, self.args.rate_out
        self.args.rate_in = r_in
        self.args.rate_out = r_out
        # 费率精确了，安全系数可以提到 0.9（不再预留 2 倍不确定性）
        if not self.args.auto_calibrate_keep_margin:
            self.args.safety_margin = 0.9
            self.cap5h = self.args.window_credits * 0.9
            self.capweek = self.args.weekly_credits * 0.9
        # 把费率存到账本里，重启后 load_state 恢复
        self._save_rates_to_state(r_in, r_out)
        self.log(f"费率已校准：入 {old_in:.0f}→{r_in:.0f}，出 {old_out:.0f}→{r_out:.0f}"
                 f" 积分/百万token；安全系数 → {self.args.safety_margin}", "WARN")

    def _save_rates_to_state(self, r_in: float, r_out: float) -> None:
        """把校准后的费率写进账本文件（与 load_state/save_state 同文件）。"""
        import json
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data["rate_in"] = r_in
        data["rate_out"] = r_out
        data["safety_margin"] = self.args.safety_margin
        with contextlib.suppress(OSError):
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_file)

    def _load_rates_from_state(self) -> None:
        """从账本恢复上次校准的费率（如果存过）。"""
        import json
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if "rate_in" in data and "rate_out" in data:
            self.args.rate_in = float(data["rate_in"])
            self.args.rate_out = float(data["rate_out"])
            if "safety_margin" in data:
                self.args.safety_margin = float(data["safety_margin"])
                self.cap5h = self.args.window_credits * self.args.safety_margin
                self.capweek = self.args.weekly_credits * self.args.safety_margin
            self.log(f"已从账本恢复校准费率：入{self.args.rate_in:.0f}/出{self.args.rate_out:.0f}"
                     f" 积分/百万token，安全系数 {self.args.safety_margin}")

    # ---- 周期汇总 ----------------------------------------------------------
    async def periodic_summary(self) -> None:
        while not self.stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=self.args.summary_interval)
            if self.stop.is_set():
                break
            # 绝对上限全触顶 → 任务结束（没有恢复可能，空转没有意义）
            if self.cap_total > 0 and all(a.parked_until == float("inf")
                                          and a.credits_total >= self.cap_total
                                          for a in self.accounts):
                self.log("所有账号累计烧量都已达 --pool-total-credits 绝对上限，消耗器退出。"
                         "如商汤已发放新周期积分，清掉账本对应账号的 credits_total 再启动",
                         "ERROR")
                self.stop.set()
                break
            # AIMD 加性增：账号静默（60s 无 429）就 +1 并发，自动贴回供应商上限
            for acct in self.accounts:
                if acct.target < self.args.per_account_max \
                        and time.time() - acct.last_429 > 60:
                    acct.target += 1
            self.save_state()
            self._maybe_auto_calibrate()
            elapsed = time.time() - self.started
            rate_in = (self.total.tokens_in - self.base_in) / max(1e-9, elapsed)
            rate_out = (self.total.tokens_out - self.base_out) / max(1e-9, elapsed)
            now = time.time()
            parked = sum(1 for a in self.accounts if now < a.parked_until)
            inflight = sum(a.inflight for a in self.accounts)
            week_burned = sum(a.burned_since(a.week_start(now, self.week_anchor_ts))
                              for a in self.accounts)
            self.log(
                f"汇总 {elapsed / 3600:.2f}h | 累计 {htokens(self.total.tokens_in)}入"
                f" {htokens(self.total.tokens_out)}出 ≈{self.total.credits:.0f}积分"
                f" | 本周(全部账号)≈{week_burned:.0f}积分"
                f" | 本段速率 {htokens(rate_in)}/{htokens(rate_out)} tok/s"
                f" | 在飞 {inflight} | 成功 {self.total.ok} 失败 {self.total.fail}"
                f" 429 {self.total.rate_limited} | 账号停靠 {parked}/{len(self.accounts)}"
            )

    # ---- 工作协程 ----------------------------------------------------------
    async def _worker(self, client: httpx.AsyncClient) -> None:
        while not self.stop.is_set():
            # 自动校准暂停：烧够阈值后停发新请求，等用户校准
            if self._calibrating:
                await asyncio.sleep(5)
                continue
            if self.all_keys_dead():
                self.log("所有 Key 都已永久失效，任务结束", "ERROR")
                return
            ks = self.pick_key()
            if ks is None:
                # 账号停靠（预算/积分耗尽）或 Key 冷却中：等窗口滚动恢复后继续。
                # 常驻模式绝不因为全部停靠而退出，停靠只是暂时休眠。
                if self.args.once and all(k.attempts > 0 for k in self.keys) \
                        and all(k.inflight == 0 for k in self.keys):
                    return
                await asyncio.sleep(0.5)
                continue
            if time.time() < self.total.global_pause_until:
                await asyncio.sleep(1)
                continue
            est_cost = self._est_cost
            if not self.budget_allow(ks.account, est_cost):
                await asyncio.sleep(1)
                continue
            # 在飞成本先记账（完成时在 finally 里冲销，成功时已按实扣入账）
            ks.account.inflight += 1
            ks.account.inflight_cost += est_cost
            ks.inflight += 1
            try:
                await self.burn_once(client, ks)
            except Exception as exc:  # 兜底：不让单次异常打死 worker
                ks.fail += 1
                self.total.fail += 1
                ks.last_err = repr(exc)[:120]
                ks.cooldown_until = time.time() + 15
                self.log(f"[{ks.name}] 未预期异常，冷却 15s：{exc!r:.160}", "ERROR")
            finally:
                ks.inflight -= 1
                ks.account.inflight -= 1
                ks.account.inflight_cost -= est_cost

    # run() 里算一次，避免每条请求重复构建 messages
    _est_cost: float = 0.0

    async def run(self) -> None:
        timeout = httpx.Timeout(connect=self.args.connect_timeout,
                                read=self.args.read_timeout, write=60, pool=60)
        limits = httpx.Limits(max_connections=self.args.concurrency * 2 + 8)
        # trust_env=False：商汤是国内服务，直连即可；不走系统代理（Windows 注册表
        # 里的 127.0.0.1:10808），避免代理进程没开时整个消耗器跟着瘫痪
        async with httpx.AsyncClient(timeout=timeout, limits=limits,
                                     trust_env=False) as client:
            self._est_cost = self.request_cost(build_messages(self.args))
            workers = [asyncio.create_task(self._worker(client))
                       for _ in range(self.args.concurrency)]
            summary = asyncio.create_task(self.periodic_summary())
            timer = None
            if self.args.max_seconds > 0:
                async def _timer() -> None:
                    await asyncio.sleep(self.args.max_seconds)
                    self.log(f"已达 --max-seconds={self.args.max_seconds:.0f}，收尾中")
                    self.stop.set()
                timer = asyncio.create_task(_timer())

            # 等待任意结束条件：worker 自然结束（--once / 全部失效）/ 定时器
            stop_waiter = asyncio.create_task(self.stop.wait())
            all_tasks = workers + [summary] + ([timer] if timer else [])
            await asyncio.wait([*all_tasks, stop_waiter],
                               return_when=asyncio.FIRST_COMPLETED)
            self.stop.set()
            for t in [*all_tasks, stop_waiter]:
                t.cancel()
            await asyncio.gather(*all_tasks, stop_waiter, return_exceptions=True)
        self.final_summary()

    def final_summary(self) -> None:
        self.save_state()
        elapsed = time.time() - self.started
        self.log("=" * 72)
        self.log(
            f"结束。运行 {elapsed / 3600:.2f}h，"
            f"本段烧掉 输入 {htokens(self.total.tokens_in - self.base_in)}"
            f" + 输出 {htokens(self.total.tokens_out - self.base_out)}"
            f" = {htokens(self.total.tokens_in - self.base_in + self.total.tokens_out - self.base_out)} token"
        )
        self.log(
            f"历史累计（含之前的运行）：输入 {htokens(self.total.tokens_in)}"
            f" + 输出 {htokens(self.total.tokens_out)}"
            f" = {htokens(self.total.tokens_in + self.total.tokens_out)} token"
            f" ≈{self.total.credits:.0f}积分（按当前费率估算，偏保守）"
        )
        if elapsed > 0:
            total_rate = (self.total.tokens_in - self.base_in
                          + self.total.tokens_out - self.base_out) / elapsed
            self.log(f"本段平均速率 {htokens(total_rate)} tok/s（含限流/停靠等待）")
        now = time.time()
        for acct in self.accounts:
            ws = acct.week_start(now, self.week_anchor_ts)
            tag = ""
            if now < acct.parked_until:
                tag = f"停靠至 {time.strftime('%m-%d %H:%M', time.localtime(acct.parked_until))}"
                if acct.park_reason:
                    tag += f"（{acct.park_reason}）"
            if acct.anchor_ts:
                tag += f" 5h边界 {time.strftime('%m-%d %H:%M', time.localtime(acct.next_boundary(now)))}"
            if ws:
                tag += (f" 周边界 {time.strftime('%m-%d %H:%M', time.localtime(ws + WIN_WEEK))}"
                        f"{'(按账号)' if acct.week_anchor_ts else ''}")
            self.log(
                f"  账号 {acct.name:<28} 5h窗口≈{acct.burned(now, WIN_5H):.0f}积分"
                f" 本周(固定)≈{acct.burned_since(ws):.0f}积分 累计≈{acct.credits_total:.0f}积分"
                f" 并发目标 {acct.target:.0f} {tag}"
            )
        for ks in self.keys:
            status = "Key失效" if time.time() < ks.parked_until else "正常"
            self.log(
                f"  {ks.name:<22} ok={ks.ok:<4} 429={ks.rate_limited:<4} 失败={ks.fail:<3}"
                f" 入={htokens(ks.tokens_in):<10} 出={htokens(ks.tokens_out):<10} {status}"
            )
        self.log(f"明细日志：{self.log_path}；账本：{self.state_file}（重启不清零）")
        self.log("校准：控制台「积分消耗明细」选与账本同时段，把实扣积分填进 "
                 "--calibrate-actual 即可自动算出精确费率。")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # config/burner.yaml 的值作为默认值，命令行显式传入的优先——改配置不用
    # 记命令行，临时加参数也不受影响。用法里 --help 显示的是代码默认值，
    # 实际生效值以启动日志的「配置来源」为准。
    cfg = load_config()
    p = argparse.ArgumentParser(
        description="持续、多账号并行消耗商汤 sensenova-6.8-flash-lite 的专属池积分"
                    "（只烧专属池，预算熔断防止溢出扣到 kimi-k3 要用的通用池）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    def opt(flag: str, **kw):
        """add_argument 的薄包装：配置文件里有同名键就顶掉 default。"""
        dest = flag.lstrip("-").replace("-", "_")
        if dest in cfg:
            kw["default"] = cfg[dest]
        return p.add_argument(flag, **kw)

    p.set_defaults(_config_used=dict(cfg))

    opt("--model", default="sensenova-6.8-flash-lite", help="上游模型 ID")
    opt("--base-url",
                   default=os.environ.get("SENSENOVA_BASE_URL", "https://token.sensenova.cn/v1"),
                   help="商汤 OpenAI 兼容接口地址")
    opt("--concurrency", type=int, default=128,
                   help="全局 worker 数（并发上限；实际并发由每账号 AIMD 自适应决定）")
    opt("--per-account-max", type=int, default=24,
                   help="单账号最大并发（AIMD 的天花板；429 频繁就调小，从不 429 可调大）")
    opt("--per-account-start", type=int, default=8,
                   help="单账号自适应并发起点（起步高、靠 429 减半回落，收敛快）")
    opt("--max-tokens", type=int, default=16384,
                   help="单次请求输出上限（越大烧得越狠）")
    opt("--filler-chars", type=int, default=6000,
                   help="输入填充材料的字符数（烧输入 token，对耗时影响很小）")
    # ---- 积分池预算（防溢出烧到通用池） ----
    opt("--window-credits", type=float, default=60000,
                   help="每账号 Flash-lite 专属池 5h 窗口积分上限（官方 6 万）")
    opt("--weekly-credits", type=float, default=600000,
                   help="每账号 Flash-lite 专属池每周积分上限（官方 60 万；"
                        "若控制台显示的实际规模更小，请按控制台填）")
    opt("--safety-margin", type=float, default=0.45,
                   help="预算安全系数（熔断线 = 上限 × 该系数）。0.45 = 内建 2 倍"
                        "费率不确定性（实测区间 出333~720，估算取 360）——即使实际"
                        "费率是估算的 2 倍，实扣也不会超过官方上限")
    opt("--week-anchor", default="Mon 00:00",
                   help="周固定窗口的起点（星期几缩写 + HH:MM，本地时区），如 "
                        '"Mon 00:00"、"Wed 09:30"。自该时刻起累计周烧量，到线停靠'
                        "至下周同一时刻。可对齐控制台「专属池」的重置时刻")
    opt("--pool-total-credits", type=float, default=0,
                   help="每账号累计烧量绝对上限（按持久化账本口径，到线永久停靠，"
                        "防赠送池过期后继续空转烧通用池）。0 = 关闭")
    opt("--rate-in", type=float, default=830,
                   help="输入 token 积分费率（积分/百万token）。2026-09-24 用控制台"
                        "「本周剩余」两次读数差反推校准（旧默认 120 偏低约 7 倍，"
                        "是烧穿专属池事故的根因之一）。费率会持久化进账本，"
                        "重启后以账本为准；可用 --calibrate-actual 精校准")
    opt("--rate-out", type=float, default=2500,
                   help="输出 token 积分费率（积分/百万token）。同上，两次独立读数"
                        "反推 2516 / 2441（相差 3%），按 r_in=r_out/3 摊后取整。"
                        "单条请求（6000 字填充 + 16384 出）≈46 积分，5h 熔断线"
                        "27000 ≈ 587 条")
    opt("--quota-park-hours", type=float, default=12,
                   help="判定积分耗尽后账号停靠时长（小时）")
    opt("--account-groups", default="SENSENOVA_API_KEY,SENSENOVA_API_KEY_02",
                   help="同账号 Key 分组（分号分组、逗号分 Key）；组内共享一份预算与并发")
    opt("--week-anchors", default="",
                   help="每账号的周窗口重置时刻（控制台显示的「周刷新」，本地时间）。"
                        '格式：--week-anchors "2=Wed 18:10;10=Thu 09:36"（数字=Key '
                        "序号，同 --anchors；星期几缩写 + HH:MM）。各账号周刷新时刻"
                        "不同（实测=创建时刻+N×7天），全局 --week-anchor 单值必然错配；"
                        "不填的账号回落到全局 --week-anchor")
    opt("--anchors", default="",
                   help="每账号的专属池窗口重置时刻（控制台显示的「重置时间」，本地 HH:MM）。"
                        "格式：--anchors \"1=03:30;3=07:15;5=22:05\"（数字=Key 序号，"
                        "1 即 SENSENOVA_API_KEY，与 02 同账号共用）。不填=滚动窗口模型"
                        "（保守安全）；填了=固定窗口爆发（边界后满血烧干再停靠）")
    # ---- 冷却/超时 ----
    opt("--cooldown-base", type=float, default=60, help="429 首次冷却秒数（指数退避）")
    opt("--cooldown-max", type=float, default=900, help="429 冷却上限秒数")
    opt("--connect-timeout", type=float, default=15)
    opt("--read-timeout", type=float, default=180,
                   help="流式读超时（相邻 chunk 间隔上限）")
    opt("--summary-interval", type=float, default=60, help="汇总打印间隔秒数")
    opt("--max-seconds", type=float, default=0, help="最长运行秒数，0 = 一直跑")
    opt("--once", action="store_true", default=bool(cfg.get("once", False)),
        help="每把 Key 只发一次请求就汇总退出（自检用）")
    opt("--only", default="",
                   help="只用指定的 Key（逗号分隔 env 变量名），如 SENSENOVA_API_KEY_03")
    opt("--log-file", default=str(DEFAULT_LOG), help="日志文件路径")
    opt("--state-file", default=str(STATE_FILE),
                   help="账本持久化文件（重启不清零，累计口径与控制台连续）")
    opt("--calibrate-actual", type=float, default=0,
                   help="校准模式：传入控制台「积分消耗明细」里与账本同时段的实扣积分"
                        "（如 --calibrate-actual 7000），算出精确费率后退出，不烧积分")
    opt("--auto-calibrate", type=float, default=0, metavar="TOKENS",
                   help="自动校准：每烧够 N 百万 token 暂停一次，提示输入控制台实扣积分，"
                        "自动算出精确费率并继续烧。0 = 关闭。推荐 5（约 1~2 小时烧到）")
    opt("--auto-calibrate-keep-margin", action="store_true",
                   help="自动校准后保持原安全系数（默认校准后自动提到 0.9，因为费率已精确）")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    load_dotenv(ROOT / ".env")
    args = parse_args(argv)
    try:
        parse_week_anchor(args.week_anchor)
    except ValueError as exc:
        print(f"ERROR: --week-anchor {args.week_anchor!r}：{exc}", file=sys.stderr)
        return 2

    keys = load_keys()
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        keys = [k for k in keys if k.name in wanted]
    if not keys:
        print("ERROR: .env 里没有找到任何 SENSENOVA_API_KEY*，无法运行。", file=sys.stderr)
        return 2
    if not is_flash_lite(args.model):
        # 烧错模型 = 直接扣 kimi-k3 的通用池积分，且静默无信号。宁可拒绝启动。
        print(f"ERROR: --model {args.model!r} 不是 Flash-lite 模型。\n"
              f"       只有 Flash-lite 家族扣「专属池周期积分」（唯一能 1:1 折算回充成\n"
              f"       K3 可用积分的池）；其他模型只扣「通用池」——那是 kimi-k3 的口粮，\n"
              f"       烧了纯亏，且 API 不返回任何池信号，事后无从发现。\n"
              f"       请用 sensenova-6.8-flash-lite（或 6.7-flash-lite，限流桶独立，\n"
              f"       可另开实例并行烧）。", file=sys.stderr)
        return 2
    group_accounts(keys, args.account_groups)

    burner = Burner(args, keys)
    if args.anchors:
        n = apply_anchors(args.anchors, keys, burner.log)
        if n == 0:
            print("WARN: --anchors 没有生效到任何账号，将退回滚动窗口模型", file=sys.stderr)
    if args.week_anchors:
        n = apply_week_anchors(args.week_anchors, keys, burner.log)
        if n == 0:
            print("WARN: --week-anchors 没有生效到任何账号，"
                  "这些账号回落到全局 --week-anchor", file=sys.stderr)
    burner._load_rates_from_state()
    if burner.load_state():
        burner.base_in, burner.base_out = burner.total.tokens_in, burner.total.tokens_out
        burner.log(f"已恢复历史账本：{htokens(burner.total.tokens_in)}入"
                   f" {htokens(burner.total.tokens_out)}出 ≈{burner.total.credits:.0f}积分"
                   f"（累计口径连续，可与控制台直接对比）")
    # 自动校准基线：从恢复后的账本算起
    burner._calibrate_start_in = burner.total.tokens_in
    burner._calibrate_start_out = burner.total.tokens_out
    if args.calibrate_actual > 0:
        r_in, r_out = burner.suggest_rates(args.calibrate_actual)
        tin = sum(k.tokens_in for k in burner.keys)
        tout = sum(k.tokens_out for k in burner.keys)
        print(f"账本累计：入 {tin:,} + 出 {tout:,} token")
        print(f"你给的实扣：{args.calibrate_actual:,.0f} 积分")
        print(f"精确费率：入 {r_in:.0f} / 出 {r_out:.0f} 积分/百万token")
        if args.auto_calibrate:
            # 自动校准模式：写入账本并继续烧
            burner.apply_calibrated_rates(r_in, r_out)
            burner._calibrating = False
            burner._calibrate_start_in = burner.total.tokens_in
            burner._calibrate_start_out = burner.total.tokens_out
            burner.log("校准完成，费率已持久化到账本，继续烧。", "WARN")
        else:
            print(f"建议启动参数：--rate-in {r_in:.0f} --rate-out {r_out:.0f}")
            print("注意：实扣数必须与账本覆盖同一时段（控制台明细的时间范围要包住日志"
                  "第一次启动的时间），且期间网关没烧过 flash-lite（那也计同一池）。")
            return 0
    if burner.cap5h <= 0 or burner.capweek <= 0:
        burner.log("危险：积分预算已关闭（--window-credits/--weekly-credits ≤ 0），"
                   "专属池烧完后会静默扣通用池（kimi-k3 的积分）！", "ERROR")
    burner.log(
        f"启动：model={args.model} 全局并发≤{args.concurrency}"
        f" 单账号自适应并发 {args.per_account_start}→{args.per_account_max}"
        f"（429 减半、静默每分钟 +1，学到的目标重启不丢） max_tokens={args.max_tokens}"
        f" 填充={args.filler_chars}字"
        f" | 共 {len(keys)} 把 Key、{len(burner.accounts)} 个账号预算："
        f"{', '.join(a.name for a in burner.accounts)}"
    )
    now = time.time()
    next_week = burner.week_start(now) + WIN_WEEK
    anchor_str = time.strftime('%m-%d %H:%M', time.localtime(burner.week_anchor_ts))
    next_str = time.strftime('%m-%d %H:%M', time.localtime(next_week))
    burner.log(
        f"预算熔断线（每账号）：5h 滚动窗口 {burner.cap5h:.0f} 积分"
        f"（官方 6 万 × {args.safety_margin}）；"
        f"周固定窗口 {burner.capweek:.0f} 积分（官方 60 万 × {args.safety_margin}，"
        f"锚点 {args.week_anchor}，本窗口 {anchor_str} 起，下边界 {next_str}）"
    )
    if args.safety_margin > 0.6:
        burner.log("提示：安全系数 > 0.6 时，若实际费率处在实测区间上沿（≈估算 2 倍），"
                   "专属池仍可能被烧穿溢出——2026-09 事故的根因。保持默认 0.45 或更低", "WARN")
    if burner.cap_total > 0:
        burner.log(f"绝对上限：每账号累计 ≈{burner.cap_total:.0f} 积分，到线永久停靠")
    burner.log(
        f"费率估算 入{args.rate_in:.0f}/出{args.rate_out:.0f} 积分/百万token"
        f" → 单条请求 ≈{burner.request_cost(build_messages(args)):.0f} 积分"
        f"（预算按此口径记账，系数已含费率不确定性）"
    )
    if args.auto_calibrate:
        burner.log(f"自动校准：每 {args.auto_calibrate:.0f}M token 暂停一次提示校准"
                   f"（当前费率 入{args.rate_in:.0f}/出{args.rate_out:.0f}）")
    cfg_used = ", ".join(f"{k}={args._config_used[k]}" for k in sorted(args._config_used)) \
        if args._config_used else "无（全用命令行/默认）"
    burner.log(f"配置来源：config/burner.yaml → {cfg_used}")
    burner.log(f"目标池：Flash-Lite 专属池（model={args.model}；"
               f"只有它扣专属池、能 1:1 折算回充成 K3 可用积分）")
    burner.log(f"接口：{burner.url}")
    anchored = [a for a in burner.accounts if a.anchor_ts]
    if anchored:
        burner.log("5h 锚点模式（固定窗口，边界后满血爆发）：" + ", ".join(
            f"{a.name}→{time.strftime('%m-%d %H:%M', time.localtime(a.next_boundary(now)))}"
            for a in anchored))
    else:
        burner.log("5h 窗口模型：滚动（未配置 --anchors；按最近5h烧量记账，保守安全）")
    per_acct_week = [a for a in burner.accounts if a.week_anchor_ts]
    if per_acct_week:
        parts = []
        for a in per_acct_week:
            ws = a.week_start(now, burner.week_anchor_ts)
            parts.append(f"{a.name}→{time.strftime('%m-%d %H:%M', time.localtime(ws + WIN_WEEK))}")
        burner.log("周锚点（按账号，来自 --week-anchors）：" + ", ".join(parts))
    fallback_week = [a for a in burner.accounts if not a.week_anchor_ts]
    if fallback_week:
        burner.log(f"周锚点（回落全局 --week-anchor={args.week_anchor}）："
                   + ", ".join(a.name for a in fallback_week))

    # Windows 关闭控制台窗口会发 SIGBREAK（约 5s 宽限）：保存账本并留痕
    def _on_close(signum, frame) -> None:
        burner.log(f"收到关闭信号 {signum}，保存账本后退出", "WARN")
        burner.save_state()
        sys.exit(0)

    with contextlib.suppress(ValueError, OSError, AttributeError):
        import signal

        signal.signal(signal.SIGBREAK, _on_close)

    try:
        asyncio.run(burner.run())
    except KeyboardInterrupt:
        print("^C", flush=True)
        burner.final_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
