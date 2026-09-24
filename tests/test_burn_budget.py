"""消耗器预算数学回归（scripts/burn_sensenova.py 的纯计算部分）。

背景：2026-09 的「滚动周窗口 + 默认费率偏下沿」组合导致熔断线物理上永远
触不到，专属池被烧穿后静默溢出扣通用池。这里钉死修复后的行为：
固定周窗口记账、安全系数默认 0.45、绝对上限停靠。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from scripts import burn_sensenova
from scripts.burn_sensenova import (
    WIN_5H,
    WIN_WEEK,
    AccountState,
    Burner,
    KeyState,
    apply_week_anchors,
    is_flash_lite,
    load_config,
    parse_args,
    parse_week_anchor,
    parse_week_anchors,
)


@pytest.fixture(autouse=True)
def _isolate_burner_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never append to the live log or rewrite the live ledger.

    ``parse_args`` resolves both defaults from these module constants at call
    time, so redirecting them here is enough - and it also covers a future test
    that forgets to pass ``--log-file``. Found the hard way: every run of this
    file was appending real-looking "预算触顶 / 永久停靠" lines for a fixture
    account (K1) into ``data/burn_sensenova.log``, which is the file the tray's
    heartbeat reads to decide green vs blue.
    """
    monkeypatch.setattr(burn_sensenova, "DEFAULT_LOG", tmp_path / "burn_sensenova.log")
    monkeypatch.setattr(burn_sensenova, "STATE_FILE", tmp_path / "burn_state.json")

# ---------------------------------------------------------------------------
# parse_week_anchor
# ---------------------------------------------------------------------------


def test_week_anchor_resolves_to_most_recent_weekday() -> None:
    ts = parse_week_anchor("Mon 00:00")
    lt = time.localtime(ts)
    assert lt.tm_wday == 0
    assert (lt.tm_hour, lt.tm_min) == (0, 0)
    assert ts <= time.time() < ts + WIN_WEEK


def test_week_anchor_same_day_future_time_rolls_back_a_week() -> None:
    now = time.time()
    lt = time.localtime(now)
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    # 今天是周几就传周几的 23:59（若当前时刻已过则不回滚；两种情况都必须 <= now+7d）
    ts = parse_week_anchor(f"{names[lt.tm_wday]} 23:59")
    assert now - WIN_WEEK <= ts
    # 恒等式：锚点必然落在 [now-7d, now] 内
    assert ts <= now


def test_week_anchor_rejects_garbage() -> None:
    for bad in ("", "Monday 00:00", "Mon", "Mon 25:00", "Xyz 10:00", "Mon 00:00x"):
        try:
            parse_week_anchor(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")


# ---------------------------------------------------------------------------
# 固定周窗口记账
# ---------------------------------------------------------------------------


def _acct_with(events: list[tuple[float, float]]) -> AccountState:
    acct = AccountState(name="t")
    acct.events = list(events)
    return acct


def test_burned_since_counts_only_events_in_fixed_window() -> None:
    now = time.time()
    acct = _acct_with([(now - 6 * 86400, 100.0), (now - 3600, 50.0), (now - 60, 10.0)])
    ws = now - 2 * 86400
    assert acct.burned_since(ws) == 60.0
    # 滚动 7 天口径仍包含 6 天前那笔（两者差异正是旧版溢出的根因）
    assert acct.burned(now, WIN_WEEK) == 160.0


def test_available_uses_fixed_week_window() -> None:
    now = time.time()
    acct = _acct_with([(now - 6 * 86400, 100.0)])
    ws = now - 86400
    # 固定周窗口内没烧过 → 周余量满；滚动口径则只剩 capweek-100
    assert acct.available(now, cap5h=1000.0, capweek=200.0, week_start=ws) == 200.0
    assert acct.available(now, cap5h=1000.0, capweek=200.0, week_start=0.0) == 100.0
    # 在飞成本要扣掉
    acct.inflight_cost = 30.0
    assert acct.available(now, 1000.0, 200.0, ws) == 170.0


def test_available_respects_5h_floor() -> None:
    now = time.time()
    acct = _acct_with([(now - 600, 400.0)])
    # 5h 余量 600 < 周余量 → 取小
    assert acct.available(now, cap5h=1000.0, capweek=10_000.0, week_start=now) == 600.0


# ---------------------------------------------------------------------------
# budget_allow / resume_time（经 Burner 走全链路）
# ---------------------------------------------------------------------------


def _burner(**overrides: object) -> Burner:
    argv = ["--concurrency", "1", "--summary-interval", "1"]
    for key, value in overrides.items():
        flag = f"--{key.replace('_', '-')}"
        if value is True:
            argv.append(flag)
        else:
            argv.extend([flag, str(value)])
    args = parse_args(argv)
    ks = KeyState(name="K1", key="sk-test", account=AccountState(name="K1"))
    return Burner(args, [ks])


def test_budget_trips_and_parks_until_next_week_boundary() -> None:
    burner = _burner(weekly_credits=1000, safety_margin=0.5, window_credits=10**9)
    acct = burner.keys[0].account
    now = time.time()
    ws = burner.week_start(now)
    acct.events = [(now - 60, 490.0)]  # 已烧 490 / 熔断线 500
    cost = 20.0  # 需要 20 > 余 10 → 触发
    assert burner.budget_allow(acct, cost) is False
    assert acct.parked_until >= ws + WIN_WEEK  # 停靠到下周锚点之后
    assert burner.budget_allow(acct, cost) is False  # 停靠期间继续拒绝


def test_budget_allows_when_within_fixed_week_cap() -> None:
    burner = _burner(weekly_credits=1000, safety_margin=0.5)
    acct = burner.keys[0].account
    now = time.time()
    acct.events = [(now - 60, 100.0)]
    old_park = acct.parked_until
    assert burner.budget_allow(acct, 100.0) is True
    assert acct.parked_until == old_park


def test_inflight_cost_subtracted_from_headroom() -> None:
    burner = _burner(weekly_credits=1000, safety_margin=0.5)
    acct = burner.keys[0].account
    acct.inflight_cost = 460.0  # 在飞已预订 460，余量只剩 40
    assert burner.budget_allow(acct, 50.0) is False


def test_absolute_total_cap_parks_account_forever() -> None:
    burner = _burner(pool_total_credits=500)
    acct = burner.keys[0].account
    acct.credits_total = 500.0
    assert burner.budget_allow(acct, 1.0) is False
    assert acct.parked_until == float("inf")
    assert "绝对上限" in acct.park_reason


def test_default_margin_is_conservative() -> None:
    args = parse_args([])
    # 0.45 = 官方上限的一半：费率即使处在实测区间上沿（≈估算 2 倍）也不烧穿
    assert args.safety_margin == 0.45
    assert args.week_anchor == "Mon 00:00"
    assert args.pool_total_credits == 0


def test_week_start_advances_across_boundaries() -> None:
    burner = _burner()
    now = time.time()
    ws = burner.week_start(now)
    assert ws <= now < ws + WIN_WEEK
    assert burner.week_start(ws + WIN_WEEK + 1) == ws + WIN_WEEK


def test_5h_rolling_still_applies() -> None:
    burner = _burner(window_credits=100, safety_margin=0.5)
    acct = burner.keys[0].account
    now = time.time()
    acct.events = [(now - 60, 60.0), (now - 6 * 3600, 1000.0)]  # 6h 前的不算 5h 窗口
    # 5h 内烧 60 / 熔断线 50 → 拒
    assert burner.budget_allow(acct, 1.0) is False
    # 停靠时刻应按 5h 滚动事件计算（>= 最老窗口内事件 + 5h）
    assert acct.parked_until >= now + WIN_5H - 120


# ---------------------------------------------------------------------------
# 2026-09-24 校准与按账号周锚点（控制台真值驱动）
# ---------------------------------------------------------------------------


def test_per_account_week_anchor_overrides_the_global_one() -> None:
    """各账号周刷新时刻不同（实测=创建时刻+N×7天），必须按账号记周账。"""
    burner = _burner(weekly_credits=1000, safety_margin=0.5)
    acct = burner.keys[0].account
    now = time.time()
    # 账号自己的周锚点：故意设成 2 天前 → 本周已烧很多
    acct.week_anchor_ts = now - 2 * 86400
    acct.events = [(now - 3600, 400.0)]
    # 全局锚点是默认 Mon 00:00，若误用全局，这条事件落在窗口外 → 会放行
    ws = acct.week_start(now, burner.week_anchor_ts)
    assert ws == acct.week_anchor_ts
    assert acct.available(now, cap5h=10**9, capweek=500.0, week_start=ws) == 100.0


def test_week_start_falls_back_to_the_global_anchor() -> None:
    burner = _burner()
    acct = burner.keys[0].account
    now = time.time()
    assert acct.week_anchor_ts == 0.0
    assert acct.week_start(now, burner.week_anchor_ts) == burner.week_start(now)


def test_week_start_returns_zero_when_both_anchors_are_unset() -> None:
    burner = _burner()
    acct = burner.keys[0].account
    burner.week_anchor_ts = 0.0          # 全局锚点显式置空 = 滚动 7 天逃生口
    assert acct.week_anchor_ts == 0.0
    assert acct.week_start(time.time(), 0.0) == 0.0


def test_parse_week_anchors_parses_key_index_to_epoch() -> None:
    got = parse_week_anchors("2=Wed 18:10;10=Thu 09:36")
    assert set(got) == {2, 10}
    for ts in got.values():
        lt = time.localtime(ts)
        assert ts <= time.time()
        assert (lt.tm_hour, lt.tm_min) in ((18, 10), (9, 36))


def test_parse_week_anchors_rejects_garbage_without_raising() -> None:
    assert parse_week_anchors("nope") == {}
    assert parse_week_anchors("2=not-a-time") == {}


def test_apply_week_anchors_maps_key_index_to_account() -> None:
    keys = [KeyState(name="SENSENOVA_API_KEY_02", key="sk-a",
                     account=AccountState(name="K2")),
            KeyState(name="SENSENOVA_API_KEY_10", key="sk-b",
                     account=AccountState(name="K10"))]
    logs: list[tuple[str, str]] = []
    n = apply_week_anchors("2=Wed 18:10;99=Fri 01:00",
                           keys, lambda msg, level="INFO": logs.append((msg, level)))
    assert n == 1
    assert keys[0].account.week_anchor_ts > 0
    assert keys[1].account.week_anchor_ts == 0.0
    assert any("99" in msg for msg, _ in logs)


def test_budget_uses_the_account_week_anchor_not_the_global_one() -> None:
    """全局单值会让早刷新的账号被少算 → 烧穿。这里钉死按账号那条路。"""
    burner = _burner(weekly_credits=1000, safety_margin=0.5, window_credits=10**9)
    acct = burner.keys[0].account
    now = time.time()
    acct.week_anchor_ts = now - 2 * 86400     # 账号周窗口 2 天前才开始
    acct.events = [(now - 3600, 480.0)]       # 全落在这个窗口内
    assert burner.budget_allow(acct, 30.0) is False
    ws = acct.week_start(now, burner.week_anchor_ts)
    assert acct.parked_until >= ws + WIN_WEEK


def test_save_state_persists_the_calibrated_rates(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """费率必须落盘：_save_rates_to_state 先写、save_state 后写，早先的整体
    覆盖把 rate_in/rate_out/safety_margin 冲掉 → 重启后校准丢失、退回默认
    费率（2026-09-24 实测：真实费率比旧默认高近 7 倍都没能记住）。"""
    import json

    burner = _burner(rate_in=830, rate_out=2500, safety_margin=0.45)
    burner.state_file = tmp_path / "state.json"
    burner.args.rate_in, burner.args.rate_out = 830.0, 2500.0
    burner.save_state()
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert data["rate_in"] == 830.0
    assert data["rate_out"] == 2500.0
    assert data["safety_margin"] == 0.45


def test_state_round_trip_keeps_both_anchor_kinds(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    burner = _burner()
    burner.state_file = tmp_path / "state.json"
    now = time.time()
    acct = burner.keys[0].account
    acct.anchor_ts = now - 3600
    acct.week_anchor_ts = now - 86400
    acct.credits_total = 123.0
    burner.save_state()

    again = _burner()
    again.state_file = tmp_path / "state.json"
    assert again.load_state() is True
    got = again.keys[0].account
    assert got.anchor_ts == acct.anchor_ts
    assert got.week_anchor_ts == acct.week_anchor_ts
    assert got.credits_total == 123.0


# ---------------------------------------------------------------------------
# config/burner.yaml 配置入口（2026-09-24）
# ---------------------------------------------------------------------------


def test_load_config_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_config(tmp_path / "nope.yaml") == {}


def test_load_config_reads_known_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("rate_out: 2500\nsafety_margin: 0.45\nanchors: \"2=15:10\"\n",
                   encoding="utf-8")
    got = load_config(cfg)
    assert got == {"rate_out": 2500.0, "safety_margin": 0.45, "anchors": "2=15:10"}


def test_load_config_accepts_int_where_float_expected(tmp_path: Path) -> None:
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("window_credits: 60000\n", encoding="utf-8")
    assert load_config(cfg)["window_credits"] == 60000.0


def test_load_config_ignores_unknown_keys(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("rate_out: 1\nnot_a_flag: 2\n", encoding="utf-8")
    got = load_config(cfg)
    assert got == {"rate_out": 1.0}
    assert "not_a_flag" in capsys.readouterr().err


def test_load_config_rejects_wrong_type(tmp_path: Path) -> None:
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("rate_out: \"fast\"\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_config(cfg)


def test_load_config_rejects_bool_for_number(tmp_path: Path) -> None:
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("rate_out: true\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_config(cfg)


def test_load_config_rejects_non_mapping(tmp_path: Path) -> None:
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_config(cfg)


def test_config_file_values_become_defaults(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """配置入口的核心契约：文件值成为默认值，命令行显式传入仍然优先。"""
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("rate_out: 2500\nconcurrency: 64\nmax_seconds: 900\n", encoding="utf-8")
    monkeypatch.setattr(burn_sensenova, "CONFIG_FILE", cfg)

    args = parse_args([])
    assert args.rate_out == 2500.0
    assert args.concurrency == 64
    assert args.max_seconds == 900.0
    assert args._config_used["rate_out"] == 2500.0

    overridden = parse_args(["--rate-out", "999", "--concurrency", "16"])
    assert overridden.rate_out == 999.0
    assert overridden.concurrency == 16
    assert overridden.max_seconds == 900.0


def test_config_file_hyphen_keys_are_normalised(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("per-account-max: 32\nweek-anchors: \"2=Wed 18:10\"\n", encoding="utf-8")
    monkeypatch.setattr(burn_sensenova, "CONFIG_FILE", cfg)
    args = parse_args([])
    assert args.per_account_max == 32
    assert args.week_anchors == "2=Wed 18:10"


def test_committed_template_covers_every_config_key() -> None:
    """模板必须列出全部可配置键，否则用户不知道有什么可调。"""
    text = (Path(__file__).resolve().parent.parent
            / "config" / "burner.example.yaml").read_text(encoding="utf-8")
    missing = sorted(k for k in burn_sensenova._CONFIG_KEYS if f"{k}:" not in text)
    assert not missing, f"模板缺少这些键：{missing}"


# ---------------------------------------------------------------------------
# Flash-Lite 专属池保护（烧错模型 = 直接吃 kimi-k3 的通用池积分）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,ok", [
    ("sensenova-6.8-flash-lite", True),
    ("sensenova-6.7-flash-lite", True),
    ("SENSENOVA-6.8-FLASH-LITE", True),
    ("kimi-k3", False),
    ("glm-5.2", False),
    ("deepseek-v4-flash", False),
    ("sensenova-u1-fast", False),
])
def test_is_flash_lite_only_accepts_the_flash_lite_family(model: str, ok: bool) -> None:
    assert is_flash_lite(model) is ok


def test_main_refuses_a_non_flash_lite_model(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """烧错模型 = 扣 kimi-k3 的通用池积分，且静默无信号。必须拒绝启动。"""
    monkeypatch.setattr(burn_sensenova, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(burn_sensenova, "CONFIG_FILE", tmp_path / "no-such.yaml")
    monkeypatch.setenv("SENSENOVA_API_KEY_03", "sk-test")
    rc = burn_sensenova.main(["--model", "kimi-k3", "--log-file", str(tmp_path / "b.log")])
    assert rc == 2


def test_committed_template_pins_the_flash_lite_model() -> None:
    """模板与现役配置都必须烧 Flash-lite——这是专属池保护的入口。"""
    root = Path(__file__).resolve().parent.parent
    for name in ("burner.example.yaml", "burner.yaml"):
        text = (root / "config" / name).read_text(encoding="utf-8")
        assert "model: sensenova-6.8-flash-lite" in text, name
