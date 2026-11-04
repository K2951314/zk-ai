"""信号采集器：把「系统里已经有的异常数据」变成事件，投给事件中台。

为什么需要它：
中台只负责「事件 -> 规则 -> 派单」。但异常信号不会自己变成事件 ——
``/health`` 里明明有 ``quarantined_deployments``、``config_watch.last_error``、
``config.stale_files``，以前全靠人打开控制台看。这个脚本就是那根管子。

它做三件事：
1. 轮询 ``/health``，把「新出现的」异常转成事件（**变化才发**，不是每次都发）
2. 可选检查 git：本地 HEAD 与 origin/main 的差距
3. 把上次看到的状态存盘，重启后不会把同一件事再发一遍

用法::

    uv run python scripts/event_watch.py                       # 本机盯网关
    uv run python scripts/event_watch.py --once                # 只采一轮
    uv run python scripts/event_watch.py --source server \\
        --gateway http://127.0.0.1:8318 --git-repo /opt/zkai   # 在服务器上跑

在服务器上跑时记得 ``--source server``，规则里按 source 区分。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="事件信号采集：轮询网关健康状态，把变化投成事件")
    parser.add_argument("--gateway", default="http://127.0.0.1:8317", help="网关地址（中台也在这里）")
    parser.add_argument(
        "--token",
        default=os.environ.get("ZKAI_ADMIN_TOKEN", ""),
        help="管理令牌（默认读环境变量 ZKAI_ADMIN_TOKEN）",
    )
    parser.add_argument("--source", default="zkai-gateway", help="事件来源标识，规则按它匹配")
    parser.add_argument("--interval", type=float, default=60.0, help="轮询间隔秒数")
    parser.add_argument("--state", default="", help="状态文件路径（默认 data/event_watch_state.json）")
    parser.add_argument("--git-repo", default="", help="顺带检查这个仓库落后 origin/main 多少提交")
    parser.add_argument(
        "--db",
        default="",
        help="网关数据库路径（默认 data/zkai.db，用来算最近 15 分钟的失败率）",
    )
    parser.add_argument("--once", action="store_true", help="只采一轮就退出")
    return parser


# --------------------------------------------------------------------------- #
# 纯函数：快照 -> 事件（好测）
# --------------------------------------------------------------------------- #
def collect(snapshot: dict[str, Any], previous: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
    """把这次快照和上次比，只产出**新出现**的异常事件。"""
    events: list[dict[str, Any]] = []

    quarantined = snapshot.get("quarantined_deployments") or []
    current_ids = sorted(str(item.get("deployment_id") or item.get("id") or item) for item in quarantined)
    if current_ids and current_ids != sorted(previous.get("quarantine") or []):
        events.append(
            {
                "source": source,
                "type": "health.quarantined",
                "object": ",".join(current_ids),
                "summary": f"{len(current_ids)} 个部署被自动隔离：{', '.join(current_ids)}",
                "link": "",
                "payload": {
                    "deployments": quarantined,
                    "fingerprint": "|".join(current_ids),
                },
            }
        )

    config_watch = snapshot.get("config_watch") or {}
    watch_error = str(config_watch.get("last_error") or "").strip()
    if watch_error and watch_error != str(previous.get("config_error") or ""):
        events.append(
            {
                "source": source,
                "type": "config.drift",
                "object": "config_watch",
                "summary": "配置文件改了但重新加载失败，当前仍在用上一份配置",
                "link": "",
                "payload": {"last_error": watch_error, "fingerprint": watch_error[:200]},
            }
        )

    stale = list((snapshot.get("config") or {}).get("stale_files") or [])
    if stale and sorted(stale) != sorted(previous.get("stale_files") or []):
        events.append(
            {
                "source": source,
                "type": "config.drift",
                "object": ",".join(stale),
                "summary": f"有配置文件被外部改过但网关还没加载：{', '.join(stale)}",
                "link": "",
                "payload": {"stale_files": stale, "fingerprint": "|".join(sorted(stale))},
            }
        )

    providers = snapshot.get("providers") or {}
    unavailable = snapshot.get("status") == "degraded" and int(providers.get("available") or 0) == 0
    if unavailable and not previous.get("no_provider"):
        events.append(
            {
                "source": source,
                "type": "health.degraded",
                "object": "providers",
                "summary": "没有任何供应商可用，网关无法提供服务",
                "link": "",
                "payload": {"providers": providers},
            }
        )
    return events


# --------------------------------------------------------------------------- #
# 失败率突发（rate_limit.burst）
# --------------------------------------------------------------------------- #
#: 判定「突发」的默认阈值。取这几个数不是拍脑袋：
#: 2026-11-03 只读查库，11,189 条请求里 5,499 条失败（49%）——
#: 4,063 条 no_available_credential + 1,436 条 rate_limit_error。
#: 而「正常」时段的失败率远低于这个数，所以 30% 已经能抓住真实故障。
BURST_FAILURE_RATE = 0.30
#: 窗口内至少要有这么多请求才判——3 条里错 1 条不是突发，是噪音
BURST_MIN_REQUESTS = 10
#: 统计窗口（分钟）
BURST_WINDOW_MINUTES = 15

#: 视为「容量/限流类」的错误。跟 AGENTS.md 里那几条教训对齐：
#: 429 不等于渠道坏，多半是 tpm/rpm 打满或整池 Key 见底。
_THROTTLE_ERRORS = frozenset(
    {
        "rate_limit_error",
        "no_available_credential",
        "no_available_deployment",
    }
)


def burst_probe(db_path: Path, *, minutes: int = BURST_WINDOW_MINUTES) -> dict[str, Any] | None:
    """查最近 N 分钟的失败率，超阈值就返回一份摘要。

    为什么要这个探针（2026-11-03）：``/health`` 只看得到「被隔离的部署」，
    而真正的痛点在 ``requests`` 表里——11,189 条请求有 5,499 条失败，
    一次都没进过事件中台。而 ``events.yaml`` 里那条 ``rate_limit.burst``
    规则从写下那天起就没有任何产出方，等于一条永不触发的死规则。

    这里**只读**数据库（``mode=ro``），不写、不锁、不影响网关。
    拿不到库就返回 None——采集器的职责是锦上添花，不是把网关拖下水。
    """
    import sqlite3

    if not db_path.is_file():
        return None
    cutoff = f"-{max(1, int(minutes))} minutes"
    try:
        # 只读 URI：绝不给网关的库加写锁
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "select count(*), "
            "sum(case when error_type is not null then 1 else 0 end), "
            "sum(case when error_type in (?,?,?) then 1 else 0 end) "
            "from requests where started_at > datetime('now', ?)",
            (*sorted(_THROTTLE_ERRORS), cutoff),
        ).fetchone()
        if not row:
            return None
        total, failed, throttled = (int(v or 0) for v in row)
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    if total < BURST_MIN_REQUESTS:
        return None
    rate = failed / total
    if rate < BURST_FAILURE_RATE:
        return None
    return {
        "window_minutes": minutes,
        "requests": total,
        "failed": failed,
        "throttled": throttled,
        "failure_rate": round(rate, 4),
    }


def burst_event(summary: dict[str, Any], *, source: str) -> dict[str, Any]:
    """把 burst_probe 的摘要翻成一条事件。

    指纹里带**窗口分桶**（分钟粒度对齐），这样同一场故障在连续几轮轮询里
    算出同一个键、被中台去重合并成一条，而不是每 60 秒开一个新任务。
    """
    window = int(summary["window_minutes"])
    bucket = _minute_bucket(window)
    pct = summary["failure_rate"] * 100
    return {
        "source": source,
        "type": "rate_limit.burst",
        "object": "requests",
        "summary": (
            f"最近 {window} 分钟失败率 {pct:.0f}%"
            f"（{summary['failed']}/{summary['requests']}，其中限流类 {summary['throttled']}）"
        ),
        "link": "",
        "payload": {**summary, "fingerprint": f"burst|{window}|{bucket}"},
    }


def _minute_bucket(window_minutes: int) -> int:
    """当前时间的「窗口分桶」，同名故障落在同一桶里便于去重。"""
    import time

    span = max(1, window_minutes) * 60
    return int(time.time() // span)


# --------------------------------------------------------------------------- #
# 到点的复查（回执 later）
# --------------------------------------------------------------------------- #
async def drain_due_laters(client: httpx.AsyncClient) -> list[int]:
    """让中台取走到点的复查，返回它派单出来的事件编号。

    **不在采集器里读库**（2026-11-04）：复查到期要「查到 → 标 fired → 建事件」
    这三步必须在一个事务里，否则采集器查到之后崩溃，那几条复查就永远
    卡在 pending 没人发。采集器只是心跳，消费交给中台。
    拿不到（网关还没上这个接口 / 网络抖）就当作没有到点的，下轮再来。
    """
    try:
        response = await client.post("/zkadmin/events/later/drain", timeout=15)
    except httpx.HTTPError as exc:
        print(f"取到点复查失败（{exc}），下轮再试", flush=True)
        return []
    if response.status_code == 404:
        return []
    if response.status_code >= 400:
        print(f"取到点复查失败 HTTP {response.status_code}：{response.text[:200]}", flush=True)
        return []
    try:
        body = response.json()
    except ValueError:
        return []
    created: list[int] = body.get("created") or []
    if created:
        print(f"到点复查 {len(created)} 条已转成事件：{created}", flush=True)
    return created


def next_state(
    snapshot: dict[str, Any], previous: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    """算出下一轮的比较基线。"""
    quarantined = snapshot.get("quarantined_deployments") or []
    providers = snapshot.get("providers") or {}
    config_block = snapshot.get("config") or {}
    degraded = snapshot.get("status") == "degraded"
    return {
        "quarantine": sorted(
            str(item.get("deployment_id") or item.get("id") or item) for item in quarantined
        ),
        "config_error": str((snapshot.get("config_watch") or {}).get("last_error") or ""),
        "stale_files": sorted(config_block.get("stale_files") or []),
        "no_provider": degraded and int(providers.get("available") or 0) == 0,
        "checked_at": snapshot.get("uptime_seconds"),
        "event_count": int(previous.get("event_count") or 0) + len(events),
    }


def git_behind(repo: str) -> int | None:
    """本地 HEAD 落后 origin/main 多少提交；拿不到就返回 None（不是错误）。"""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - 固定参数，无用户输入拼接
            [git, "-C", repo, "rev-list", "--count", "HEAD..origin/main"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def send(client: httpx.AsyncClient, event: dict[str, Any]) -> bool:
    try:
        response = await client.post("/zkadmin/events", json=event)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"事件投递失败（{event.get('type')}）：{exc}", flush=True)
        return False
    body = response.json()
    if body.get("duplicate"):
        print(f"事件重复已合并：{event.get('type')} {event.get('object')}", flush=True)
    else:
        rule = body.get("rule") or "（无规则）"
        print(f"事件已投递：{event.get('type')} {event.get('object')} -> {rule}", flush=True)
    return True


async def main_async(args: argparse.Namespace) -> int:
    state_path = Path(args.state) if args.state else PROJECT_ROOT / "data" / "event_watch_state.json"
    db_path = Path(args.db) if args.db else PROJECT_ROOT / "data" / "zkai.db"
    # /health 不需要令牌，但**投事件需要**。而 .env 里的 ZKAI_ADMIN_TOKEN
    # 只有在 load_app_config 之后才会进进程环境 —— 原来这里直接读
    # os.environ，令牌恒为空，于是每条事件都被 401 拒掉，
    # 而且失败信息只是被打到 stdout，看起来像「这一轮没发现异常」。
    # （2026-11-03 实跑时发现）
    if not args.token:
        try:
            from app.core.config import Settings, load_app_config

            load_app_config(Settings())
        except Exception as exc:  # 配置读不出来不该拦住采集
            print(f"读取 .env 失败（仍继续，只是可能投不进去）：{exc}", flush=True)
    token = args.token or os.environ.get("ZKAI_ADMIN_TOKEN", "")
    headers = {"X-Admin-Token": token} if token else {}
    if not token:
        print(
            "⚠ 没有管理令牌：事件会被中台以 401 拒掉。"
            "在 .env 里配 ZKAI_ADMIN_TOKEN，或用 --token 传。",
            flush=True,
        )
    previous = load_state(state_path)
    print(
        f"开始采集：网关 {args.gateway}，来源 {args.source}，每 {args.interval:.0f}s 一轮",
        flush=True,
    )
    print(f"失败率探针：{db_path}（{BURST_WINDOW_MINUTES} 分钟窗口，阈值 {BURST_FAILURE_RATE:.0%}）"
          + ("" if db_path.is_file() else " —— 库不存在，这一路会静默跳过"), flush=True)
    async with httpx.AsyncClient(base_url=args.gateway.rstrip("/"), headers=headers, timeout=30) as client:
        while True:
            try:
                response = await client.get("/health")
                response.raise_for_status()
                snapshot = response.json()
            except httpx.HTTPError as exc:
                print(f"读 /health 失败（{exc}），跳过这一轮", flush=True)
                snapshot = {}
            except ValueError:
                print("/health 返回的不是 JSON，跳过这一轮", flush=True)
                snapshot = {}

            events: list[dict[str, Any]] = []
            if snapshot:
                events = collect(snapshot, previous, source=args.source)
                for event in events:
                    await send(client, event)
                if args.git_repo:
                    behind = git_behind(args.git_repo)
                    if behind:
                        await send(
                            client,
                            {
                                "source": "server",
                                "type": "git.behind",
                                "object": args.git_repo,
                                "summary": f"代码落后 origin/main {behind} 个提交",
                                "link": "",
                                "payload": {"behind": behind, "repo": args.git_repo},
                            },
                        )
                previous = next_state(snapshot, previous, events)
                save_state(state_path, previous)
            # 失败率探针与 /health 无关：网关接口全挂时它照样能看见问题
            burst = burst_probe(db_path)
            if burst is not None:
                await send(client, burst_event(burst, source=args.source))
            # 到点的复查（回执 later）：采集器就是那个心跳，
            # 没有它，Agent 约的「明天早上再看一眼」永远不会醒。
            await drain_due_laters(client)
            if args.once:
                return 0
            await asyncio.sleep(args.interval)


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("已停止采集", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
