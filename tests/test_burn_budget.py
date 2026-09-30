"""消耗器预算数学回归（scripts/burn_sensenova.py 的纯计算部分）。

背景：2026-09 的「滚动周窗口 + 默认费率偏下沿」组合导致熔断线物理上永远
触不到，专属池被烧穿后静默溢出扣通用池。这里钉死修复后的行为：
固定周窗口记账、安全系数默认 0.45、绝对上限停靠。
"""

from __future__ import annotations

import json
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
    is_quota_exhausted,
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


# ---------------------------------------------------------------------------
# 安全系数提示不得在默认值上自相矛盾（2026-09-30 修）
# ---------------------------------------------------------------------------


def test_default_margin_does_not_trip_its_own_warning() -> None:
    """默认 0.95 不许触发「已高于默认」提示——否则每次启动都刷一句废话。

    旧版判据写 ``> 0.6``，而默认值就是 0.9，于是自相矛盾：日志说
    「安全系数 0.95 已高于默认 0.95」。更早一版建议「保持默认 0.45 或更低」，
    而默认值早就改成 0.9 了。**自相矛盾的提示等于没有提示**——运营者学会
    忽略它，真正该警醒的那次就被淹了。
    """
    assert burn_sensenova.DEFAULT_SAFETY_MARGIN == 0.95
    assert burn_sensenova.is_above_default_margin(
        burn_sensenova.DEFAULT_SAFETY_MARGIN) is False


def test_over_default_margin_is_flagged() -> None:
    """真调到默认值以上要报警：缓冲带更薄，烧穿风险实打实升高。"""
    for margin in (0.96, 0.98, 1.0):
        assert burn_sensenova.is_above_default_margin(margin) is True


def test_under_default_margin_stays_quiet() -> None:
    """比默认更保守不该被唠叨（0.45 只是少烧，不是风险）。"""
    for margin in (0.45, 0.9, 0.95):
        assert burn_sensenova.is_above_default_margin(margin) is False


def test_the_note_names_the_numbers_the_operator_needs() -> None:
    """提示必须显示实际值和默认值，不能只说「太高了」。"""
    note = burn_sensenova.above_default_margin_note(0.96)
    assert "0.96" in note
    assert "0.95" in note
    assert "缓冲带" in note


def test_console_fallback_margin_matches_the_burner() -> None:
    """控制台的兜底系数必须和消耗器默认同一个数。

    否则「YAML 与账本都没提系数」时，控制台按自己的数画熔断线、消耗器按自己
    的数跑——同一块屏幕上两个数，运营者无从判断该信哪个。
    """
    from app.services import burner_service

    burner_default = burn_sensenova.DEFAULT_SAFETY_MARGIN
    console_default = burner_service.BurnerStatus().safety_margin
    assert console_default == burner_default, (
        f"控制台兜底 {console_default} != 消耗器默认 {burner_default}"
    )


# ---------------------------------------------------------------------------
# 配置热加载：控制台改完/burner.yaml 被手改，不用重启就生效（2026-09-30 加）
# ---------------------------------------------------------------------------


def _burner_with_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                      cfg_text: str, **overrides: object) -> Burner:
    """构造一个盯着 tmp 配置文件的 Burner（绝不碰真实 config/burner.yaml）。"""
    cfg = tmp_path / "burner.yaml"
    cfg.write_text(cfg_text, encoding="utf-8")
    argv = ["--concurrency", "1", "--summary-interval", "1",
            "--config-file", str(cfg)]
    for key, value in overrides.items():
        flag = f"--{key.replace('_', '-')}"
        if value is True:
            argv.append(flag)
        else:
            argv.extend([flag, str(value)])
    args = parse_args(argv)
    ks = KeyState(name="K1", key="sk-test", account=AccountState(name="K1"))
    return Burner(args, [ks])


def test_config_change_takes_effect_without_a_restart(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """改 safety_margin 后不用重启：熔断线立刻按新系数重算。

    这条是运营者原话「后续在控制台修改后立即生效」。之前改 burner.yaml 必须重启
    消耗器，托盘还要等一次心跳（约 3 秒），期间烧的仍是旧参数。
    """
    cfg = "safety_margin: 0.5\n"
    # safety_margin 也走命令行：优先于同键的 YAML（命令行 > burner.yaml），
    # 这样「YAML 改了要不要覆盖命令行」的行为被测到。
    burner = _burner_with_cfg(tmp_path, monkeypatch, cfg, weekly_credits=1000,
                               window_credits=10000, safety_margin=0.5)
    burner._cfg_sig = burner._config_signature()
    assert burner.args.safety_margin == 0.5
    assert burner.capweek == 500.0

    # 运营者在文件里（或经控制台）把系数调大
    Path(burner.config_file).write_text(cfg.replace("0.5", "0.95"), encoding="utf-8")
    changed = burner.reload_config()

    assert "safety_margin" in changed
    assert burner.args.safety_margin == 0.95
    assert burner.capweek == 950.0, "熔断线没跟着系数重算——改了等于没改"


def test_reload_is_a_noop_when_nothing_changed(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """文件没动就不做事：不能每个汇总周期都刷一条热更新日志。"""
    burner = _burner_with_cfg(tmp_path, monkeypatch, "safety_margin: 0.5\n")
    burner._cfg_sig = burner._config_signature()
    assert burner.reload_config() == []


def test_a_broken_config_file_never_stops_the_burner(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """写了一半/语法坏掉的 YAML 不能把消耗器打停。

    保留上一份配置并告警，等运营者改完下一个周期自然恢复——他此刻正在编辑这个文件。
    """
    burner = _burner_with_cfg(tmp_path, monkeypatch, "safety_margin: 0.5\n",
                               weekly_credits=1000)
    burner._cfg_sig = burner._config_signature()
    before = burner.args.safety_margin

    Path(burner.config_file).write_text("safety_margin: [unclosed\n", encoding="utf-8")
    changed = burner.reload_config()

    assert changed == []
    assert burner.args.safety_margin == before, "坏文件把好配置冲掉了"


def test_restart_only_keys_are_flagged_not_silently_applied(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """改 Key 集合/窗口结构的键只提示要重启，不偷偷改内存。

    因为它们决定账本结构：在飞请求按旧窗口记账、新请求按新窗口记账，账本就和
    控制台对不上了。
    """
    burner = _burner_with_cfg(tmp_path, monkeypatch, "safety_margin: 0.5\n")
    burner._cfg_sig = burner._config_signature()
    original = burner.args.safety_margin

    Path(burner.config_file).write_text(
        "model: kimi-k3\n" + "only: SENSENOVA_API_KEY_01",
        encoding="utf-8")
    burner.reload_config()

    assert burner.args.safety_margin == original, "顺手改了不该热改的键"
    assert burner.args.model != "kimi-k3", "model 是重启键，不该被热改"
def test_default_margin_is_conservative() -> None:
    args = parse_args([])
    # 2026-09-24 从 0.45 提到 0.9。0.45 是费率不准时代的折扣（估算可能低估 2 倍），
    # 但费率已按控制台「本周剩余」两次读数差校准到 ±3%，继续折半只会让每个账号
    # 每周白丢约 33 万回赠积分。0.95 = 熔断线 5.7万/57万，仍留 5% 缓冲带。
    assert args.safety_margin == 0.95
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

# ---------------------------------------------------------------------------
# 429 二义性：限流 vs 权益用尽（2026-09-24 实测，商汤两者都返回 429）
# ---------------------------------------------------------------------------

#: 商汤真实返回体（2026-09-24 逐把 Key 单发抓到，keep verbatim）
_TPM_BODY = '{"error":{"message":"tpm exhausted","type":"quota_exceeded_error","code":"8"}}'
_RPM_BODY = '{"error":{"message":"rpm exhausted","type":"quota_exceeded_error","code":"8"}}'
_RPS_BODY = '{"error":{"message":"rps exhausted","type":"quota_exceeded_error","code":"8"}}'
_ENTITLEMENT_BODY = (
    '{"error":{"message":"token plan entitlement exhausted",'
    '"type":"quota_exceeded_error","code":"8"}}'
)
_BALANCE_BODY = (
    '{"error":{"message":"plan balance exhausted",'
    '"type":"quota_exceeded_error","code":"8"}}'
)


@pytest.mark.parametrize("body", [_TPM_BODY, _RPM_BODY, _RPS_BODY,
                                  "Requests per minute exceeded",
                                  "rate limit reached, please retry later"])
def test_rate_limited_bodies_are_not_mistaken_for_exhaustion(body: str) -> None:
    """限流要短冷却重试；误判成「用尽」会让账号白停靠几小时不烧。"""
    assert is_quota_exhausted(body) is False


@pytest.mark.parametrize("body", [_ENTITLEMENT_BODY, _BALANCE_BODY,
                                  "insufficient credits for this request",
                                  "您的权益已用尽，请下周再来"])
def test_entitlement_exhausted_bodies_are_recognised(body: str) -> None:
    """权益用尽要长停靠到周期刷新；误判成限流会每 60s 空转刷一次 429。"""
    assert is_quota_exhausted(body) is True


def test_quota_branch_runs_before_the_429_branch() -> None:
    """回归：早先 `if status == 429` 挡在前面，「权益用尽」分支是死代码，
    于是 KEY_07（权益已用尽）只被冷却 60s 后反复重试，只能靠人在
    config/burner.yaml 里手工排除它。现在程序必须自己认出并长停靠。"""
    burner = _burner(quota_park_hours=12)
    acct = burner.keys[0].account
    ks = burner.keys[0]

    burner.on_error(ks, 429, _ENTITLEMENT_BODY)

    # 长停靠（12h），不是 60s 短冷却
    assert acct.park_reason == "疑似额度/积分耗尽"
    assert acct.parked_until > time.time() + 11 * 3600
    # 停靠期间不许再放行预算
    assert burner.budget_allow(acct, 1.0) is False


def test_tpm_exhaustion_still_cools_down_instead_of_parking() -> None:
    """对照组：tpm exhausted 只该短冷却 + AIMD 降档，绝不长停靠。"""
    burner = _burner()
    acct = burner.keys[0].account
    ks = burner.keys[0]
    start_target = acct.target

    burner.on_error(ks, 429, _TPM_BODY)

    # 没被长停靠（这正是要守住的：别把限流当成权益用尽）
    assert acct.park_reason == ""
    assert acct.parked_until == 0.0
    assert ks.rate_limited == 1
    # AIMD 乘性减：撞一次 429 目标减半，而不是钉死或长停靠
    assert acct.target == start_target / 2
    assert acct.target > 1.0


# --------------------------------------------------------------------------- #
# 饥饿救济：别让一个账号独吃 TPM、其余饿死（2026-09-28 实测五个账号 0 成功）
# --------------------------------------------------------------------------- #


def test_starved_account_gets_a_borrowed_slot() -> None:
    """饿久的账号要能借到并发，否则它永远挤不进来。

    病灶：AIMD 的加性增只看「自己 60s 没撞 429」，完全不看同实例其他账号占了
    多少。抢占到 TPM 的账号一路涨到 per_account_max，被压住的撞 429 减半后锁在
    1。商汤的 tpm 是账号间共享的桶，所以独占者不掉，其余永远挤不进来。
    实测：KEY_05 稳定在并发 16（43 次采样全是 16、从不回落），另外五个账号
    连续 77 次采样 0 成功——全站在 19,714 次 429 对 80,345 次成功。
    """
    burner = _burner(per_account_max=4, starve_after=300)
    acct = burner.keys[0].account
    acct.target = 1.0        # 被压到底
    acct.last_ok = 0.0       # 从未成功过 → 立刻算饿
    # last_429 设为 now：否则「60s 没撞 429」的 AIMD 加性增会先 +1，
    # 就把救济的效果混进来了——这里要单独测救济，得把那条支路按下去。
    acct.last_429 = time.time()
    # 跳过启动宽限期：刚启动那会儿哪怕真的饿也不救济（2026-09-28 实测
    # 七账号同时借出后三种 429 齐炸），跑够 120s 才谈救济。
    burner.started = time.time() - 200

    burner._rebalance_concurrency()

    assert acct.starve_boost == 1, "该借出 1 级"
    assert acct.target == 2.0, "借出要把并发抬起来，否则还是挤不进去"


def test_no_relief_during_the_start_grace_window() -> None:
    """启动宽限期内不救济：否则重启会让所有账号同时算「从未成功」。

    2026-09-28 实测：旧账本没有 last_ok 字段，重启后一秒内七个账号同时借出，
    随后 tpm 35 / rpm 20 / rps 19 三种 429 齐炸——救济反而把桶挤爆了。
    """
    burner = _burner(per_account_max=4, starve_after=300, starve_grace=120)
    acct = burner.keys[0].account
    acct.target = 1.0
    acct.last_ok = 0.0
    acct.last_429 = time.time()
    burner.started = time.time() - 60        # 才跑 60s，没到 120s 宽限期

    burner._rebalance_concurrency()

    assert acct.starve_boost == 0, "宽限期内不许借出"


def test_freq_limited_account_is_not_given_more_concurrency() -> None:
    """频率类限流（rpm/rps）时加并发有害——只会让它撞得更狠。

    2026-09-28 实测：12:21 那一分钟 rpm 20 次 + rps 19 次，借出后依旧全 429。
    频率卡的是「发得多频」不是「吃得多快」，救济在这里方向相反。
    """
    burner = _burner(per_account_max=4, starve_after=300)
    acct = burner.keys[0].account
    acct.target = 1.0
    acct.last_ok = 0.0
    acct.last_429 = time.time()
    acct.last_limit_body = '{"error":{"message":"rpm exhausted"}}'
    burner.started = time.time() - 200

    burner._rebalance_concurrency()

    assert acct.starve_boost == 0, "rpm 限流时不该借出并发"
    assert acct.target == 1.0, "并发也不该被抬高"


def test_throughput_limited_account_still_gets_relief() -> None:
    """对照：tpm（吞吐）限流才是救济该救的场景。"""
    burner = _burner(per_account_max=4, starve_after=300)
    acct = burner.keys[0].account
    acct.target = 1.0
    acct.last_ok = 0.0
    acct.last_429 = time.time()
    acct.last_limit_body = '{"error":{"message":"tpm exhausted"}}'
    burner.started = time.time() - 200

    burner._rebalance_concurrency()

    assert acct.starve_boost == 1, "tpm 限流该借出"


def test_borrowed_slot_is_not_renewed_after_a_success() -> None:
    """刚成功过的账号不该再借——救济只给真正饿的。"""
    burner = _burner(per_account_max=4, starve_after=300)
    acct = burner.keys[0].account
    acct.last_ok = time.time()          # 刚刚成功
    acct.target = 1.0

    burner._rebalance_concurrency()

    assert acct.starve_boost == 0, "不饿就不借"
    # target 仍可能因原本的 AIMD 加性增而 +1（那条逻辑保留），这里只守住不借出


def test_borrow_is_capped_so_it_cannot_run_away() -> None:
    """借出必须有硬顶，否则饿账号会无限涨并发、把桶再次吃光。"""
    burner = _burner(per_account_max=4, starve_after=300)
    acct = burner.keys[0].account
    acct.target = 3.0
    acct.last_ok = 0.0                   # 一直饿
    acct.last_429 = time.time()          # 压掉 AIMD 加性增，只测救济的硬顶
    burner.started = time.time() - 200   # 跳过启动宽限期

    for _ in range(10):                  # 反复调用
        burner._rebalance_concurrency()

    assert acct.starve_boost <= 2, "最多借 2 级"
    assert acct.target <= 4.0, "不许越过 per_account_max"


def test_parked_account_is_never_relieved() -> None:
    """停靠中的账号不参与救济——它的额度没有意义，借了也是白借。"""
    burner = _burner(per_account_max=4, starve_after=300)
    acct = burner.keys[0].account
    acct.target = 1.0
    acct.last_ok = 0.0
    acct.parked_until = time.time() + 3600     # 停靠 1 小时

    burner._rebalance_concurrency()

    assert acct.starve_boost == 0, "停靠中不该借出"
    assert acct.target == 1.0, "并发也不该动"


# ---------------------------------------------------------------------------
# 校准口径（2026-09-28）：单账号读数必须按单账号 token 反推
# ---------------------------------------------------------------------------

def _burner_with_two_accounts(tmp_path: Path) -> Burner:
    """两个账号、token 量差 9 倍——正是「拿单账号读数却按全部账号反推」的陷阱。"""
    args = parse_args(["--once"])
    a1 = AccountState(name="K1")
    a2 = AccountState(name="K2")
    k1 = KeyState(name="K1", key="sk-test", account=a1)
    k2 = KeyState(name="K2", key="sk-test2", account=a2)
    burner = Burner(args, [k1, k2])
    # K1 是唯一在烧的账号，K2 完全没烧（真实场景：Ingulf 在烧，其余躺平）
    k1.tokens_in = 119_440_582
    k1.tokens_out = 525_797_796
    k2.tokens_in = 0
    k2.tokens_out = 0
    return burner


def test_single_account_calibration_uses_only_that_account(tmp_path: Path) -> None:
    """单账号读数 + 单账号 token → 费率反映该账号，不被躺平账号稀释 9 倍。"""
    burner = _burner_with_two_accounts(tmp_path)
    r_in, r_out = burner.suggest_rates(495_085.0, account="K1")
    # 与直接用该账号 token 手算一致
    denom = 525_797_796 + 119_440_582 / 3
    assert r_out == pytest.approx(495_085.0 * 1e6 / denom, rel=1e-9)
    assert r_in == pytest.approx(r_out / 3, rel=1e-9)


def test_scopeless_calibration_still_uses_every_account(tmp_path: Path) -> None:
    """不指定账号 = 实扣覆盖全部账号（旧契约，行为不变）。"""
    burner = _burner_with_two_accounts(tmp_path)
    _r_in, r_out = burner.suggest_rates(990_170.0)
    denom = 525_797_796 + 119_440_582 / 3      # K2 为 0，总量等于 K1
    assert r_out == pytest.approx(990_170.0 * 1e6 / denom, rel=1e-9)
    # 同样读数在两种口径下差 2 倍：这就是不配 account 时的静默错误来源
    _alone_in, alone_out = burner.suggest_rates(495_085.0, account="K1")
    assert r_out == pytest.approx(alone_out * 2, rel=1e-9)


def test_unknown_account_is_rejected_not_silently_ignored(tmp_path: Path) -> None:
    """账号名写错必须报错——静默按全部账号算是 9 倍误差，会烧穿专属池。"""
    burner = _burner_with_two_accounts(tmp_path)
    with pytest.raises(ValueError, match="没有账号"):
        burner.suggest_rates(495_085.0, account="NOPE")


# ---------------------------------------------------------------------------
# 外部校准不得被长命进程抹掉（2026-09-29 事故）
# ---------------------------------------------------------------------------

def test_external_rate_calibration_survives_a_running_process(tmp_path: Path) -> None:
    """「手工改 state 文件」必须能被尊重。

    事故还原：加载顺序是 args(burner.yaml) → state 覆盖 args → 进内存，而
    save_state() 又把内存值写回 state。这个环让手工校准不可能生效——进程活着，
    下一次保存就抹掉。实测：运营者按控制台读数把费率改成 761/2292，消耗器未重启
    （改 burner.yaml 需重启才生效），它以旧费率记账并在 14.5 小时后写回旧值，
    运营者看到「校准生效过又没了」，而账号仍被误判成额度耗尽在停靠。
    """
    state = tmp_path / "burn_state.json"

    burner = Burner(
        parse_args(["--rate-in", "830", "--rate-out", "2500", "--once"]),
        [KeyState(name="K1", key="sk", account=AccountState(name="K1"))],
    )
    burner.state_file = state
    burner.load_state()      # 文件不存在 → 全新启动
    burner.save_state()
    burner.load_state()      # 进程「见过」830/2500
    assert burner._loaded_rate_in == 830.0

    # 外部手工校准（模拟运营者按控制台读数改文件）
    blob = json.loads(state.read_text(encoding="utf-8"))
    blob["rate_in"], blob["rate_out"] = 761.0, 2292.0
    state.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")

    # 长命进程不重启、直接再保存：必须让位
    burner.save_state()
    after = json.loads(state.read_text(encoding="utf-8"))
    assert (after["rate_in"], after["rate_out"]) == (761.0, 2292.0), (
        "外部校准被进程抹掉了——这正是 09-29 那次『看起来生效又没了』"
    )


def test_rates_are_still_written_when_nobody_touched_them(tmp_path: Path) -> None:
    """没人外部改过时，行为与之前完全一致（无回归）。"""
    state = tmp_path / "burn_state.json"
    burner = Burner(
        parse_args(["--rate-in", "900", "--rate-out", "2700", "--once"]),
        [KeyState(name="K1", key="sk", account=AccountState(name="K1"))],
    )
    burner.state_file = state
    burner.load_state()
    burner.save_state()
    blob = json.loads(state.read_text(encoding="utf-8"))
    burner.load_state()          # 刷新 _loaded_*，文件里已是 900/2700
    burner.save_state()
    after = json.loads(state.read_text(encoding="utf-8"))
    assert (after["rate_in"], after["rate_out"]) == (900.0, 2700.0)
    assert (blob["rate_in"], blob["rate_out"]) == (900.0, 2700.0)


# ---------------------------------------------------------------------------
# 费率优先级：命令行 > burner.yaml > 账本（2026-09-30，K_02 提前停的根因）
#
# 实测事故：burner.yaml 写着校准值 761/2292，账本里是旧值 830/2500，而
# _load_rates_from_state 无条件用账本覆盖 args。日志实锤
# 「已从账本恢复校准费率：入830/出2500」——yaml 里写的新值从没生效过。
# 费率偏高 9.1% 让 K_02 按虚高记账提前撞上 54 万熔断线：账本估 539,964
# （停），真实只烧 495,085（控制台剩 104,488）。用户看到的正是
# 「明明剩 10 万，消耗器却在限流停靠」。
# ---------------------------------------------------------------------------
def test_yaml_rates_win_over_a_stale_ledger(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """burner.yaml 显式给了费率，账本里的旧值不许覆盖它。"""
    import json

    state = tmp_path / "burn_state.json"
    state.write_text(json.dumps({"rate_in": 830.0, "rate_out": 2500.0,
                                 "safety_margin": 0.9}), encoding="utf-8")
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("rate_in: 761\nrate_out: 2292\n", encoding="utf-8")
    monkeypatch.setattr(burn_sensenova, "CONFIG_FILE", cfg)

    burner = Burner(
        parse_args(["--once"]),
        [KeyState(name="K1", key="sk", account=AccountState(name="K1"))],
    )
    burner.state_file = state
    assert (burner.args.rate_in, burner.args.rate_out) == (761.0, 2292.0)
    burner._load_rates_from_state()
    assert (burner.args.rate_in, burner.args.rate_out) == (761.0, 2292.0), (
        "账本里的旧费率盖掉了 burner.yaml 的显式配置——K_02 少烧 10 万的老路"
    )
    # 生效值必须同步回账本，否则下次重启旧值又被当「上次校准」恢复出来
    after = json.loads(state.read_text(encoding="utf-8"))
    assert (after["rate_in"], after["rate_out"]) == (761.0, 2292.0)


def test_ledger_rates_still_used_when_yaml_is_silent(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """yaml 没提费率时，账本仍是兜底来源（无回归）。"""
    import json

    state = tmp_path / "burn_state.json"
    state.write_text(json.dumps({"rate_in": 830.0, "rate_out": 2500.0,
                                 "safety_margin": 0.9}), encoding="utf-8")
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("concurrency: 16\n", encoding="utf-8")
    monkeypatch.setattr(burn_sensenova, "CONFIG_FILE", cfg)

    burner = Burner(
        parse_args(["--once"]),
        [KeyState(name="K1", key="sk", account=AccountState(name="K1"))],
    )
    burner.state_file = state
    burner._load_rates_from_state()
    assert (burner.args.rate_in, burner.args.rate_out) == (830.0, 2500.0)
    assert burner.args.safety_margin == 0.9


def test_command_line_beats_both_yaml_and_ledger(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """命令行显式传的优先级最高（临时调参的语义不能被任何来源夺走）。"""
    import json

    state = tmp_path / "burn_state.json"
    state.write_text(json.dumps({"rate_in": 830.0, "rate_out": 2500.0}), encoding="utf-8")
    cfg = tmp_path / "burner.yaml"
    cfg.write_text("rate_in: 761\n", encoding="utf-8")
    monkeypatch.setattr(burn_sensenova, "CONFIG_FILE", cfg)

    burner = Burner(
        parse_args(["--rate-in", "500", "--rate-out", "1500", "--once"]),
        [KeyState(name="K1", key="sk", account=AccountState(name="K1"))],
    )
    burner.state_file = state
    burner._load_rates_from_state()
    assert (burner.args.rate_in, burner.args.rate_out) == (500.0, 1500.0)


def test_fresh_start_writes_memory_rates_not_zero(tmp_path: Path) -> None:
    """_loaded_rate_in == 0（从未加载账本）不得被当成「外部改成了 0」。"""
    state = tmp_path / "burn_state.json"
    burner = Burner(
        parse_args(["--rate-in", "761", "--rate-out", "2292", "--once"]),
        [KeyState(name="K1", key="sk", account=AccountState(name="K1"))],
    )
    burner.state_file = state
    burner.save_state()      # 未 load_state：_loaded_* 仍是 0
    after = json.loads(state.read_text(encoding="utf-8"))
    assert (after["rate_in"], after["rate_out"]) == (761.0, 2292.0)


# ---------------------------------------------------------------------------
# 外部校准不得被长命进程抹掉（2026-09-29 事故）

# ---------------------------------------------------------------------------
# 限流停靠（2026-09-30，运营者原话「达到上限自动停止且不再尝试」）
#
# 上一版只有预算熔断一种停靠，而它的语义是「积分不够了」。真实场景里账号
# 积分还剩 10 万但挤不进共享的 tpm/rpm 桶，于是每个冷却周期都去撞一次——
# 实测 19,714 次 429 对 80,345 次成功，19.7% 的请求是纯空转。
# ---------------------------------------------------------------------------


def test_rate_park_stops_an_account_that_cannot_get_into_the_bucket() -> None:
    """连续撞满阈值就停手，且停手期间 pick_key 不再挑它。"""
    burner = _burner(rate_park_after=3, rate_park_seconds=600, cooldown_base=1)
    ks = burner.keys[0]
    acct = ks.account

    # 撞满阈值之前：照常给冷却，不停靠
    for _ in range(2):
        burner.on_error(ks, 429, '{"error":{"message":"tpm exhausted"}}')
    assert acct.rate_park_until <= time.time()
    ks.cooldown_until = 0.0  # 冷却期过了才会被挑中；这里只验「没被停靠挡住」
    assert burner.pick_key() is ks

    # 第 N 次：停手
    burner.on_error(ks, 429, '{"error":{"message":"tpm exhausted"}}')
    assert acct.rate_streak == 3
    assert acct.rate_park_until > time.time()

    # 停手期间不再被选中——这条是「不再尝试」的底线。
    # 冷却也清掉：否则返回 None 分不清是冷却挡的还是停靠挡的。
    ks.cooldown_until = 0.0
    assert burner.pick_key() is None


def test_a_single_success_clears_the_rate_streak() -> None:
    """「偶尔挤一下」和「根本挤不进去」必须分开：成功一次就不能再累积。

    否则按时窗口抖动的账号会被误判成死锁、白白停靠 10 分钟。
    """
    burner = _burner(rate_park_after=3, rate_park_seconds=600)
    ks = burner.keys[0]
    acct = ks.account
    for _ in range(2):
        burner.on_error(ks, 429, '{"error":{"message":"tpm exhausted"}}')
    assert acct.rate_streak == 2

    acct.rate_streak = 0  # burn_once 成功路径上就是这一行
    burner.on_error(ks, 429, '{"error":{"message":"tpm exhausted"}}')
    assert acct.rate_streak == 1
    assert acct.rate_park_until <= time.time()


def test_rate_park_state_survives_a_restart(tmp_path: Path) -> None:
    """停靠记账必须落盘：不恢复的话每次重启 rate_streak 归零，又得从头撞满阈值。"""
    state = tmp_path / "burn_state.json"
    burner = _burner(rate_park_after=2, rate_park_seconds=600)
    ks = burner.keys[0]
    acct = ks.account
    for _ in range(2):
        burner.on_error(ks, 429, '{"error":{"message":"tpm exhausted"}}')
    parked = acct.rate_park_until
    assert parked > time.time()

    burner.state_file = state
    burner.save_state()

    # 新进程：同样的 Key、空的 AccountState，只从账本恢复
    fresh = Burner(parse_args(["--rate-park-after", "2", "--once"]),
                   [KeyState(name="K1", key="sk-test",
                             account=AccountState(name="K1"))])
    fresh.state_file = state
    fresh.load_state()
    assert fresh.keys[0].account.rate_streak == 2
    assert fresh.keys[0].account.rate_park_until == parked


def test_budget_park_and_rate_park_are_independent() -> None:
    """预算熔断与限流停靠是两套判据，不能互相干扰。

    预算够但挤不进桶 → rate_park；桶空了但积分烧完 → parked_until。
    混在一起会同时误伤：预算停了它的账号其实只是被限流，等窗口就好。
    """
    burner = _burner(weekly_credits=1000, window_credits=1000, safety_margin=0.5,
                     rate_park_after=2, rate_park_seconds=600)
    ks = burner.keys[0]
    acct = ks.account

    for _ in range(2):
        burner.on_error(ks, 429, '{"error":{"message":"tpm exhausted"}}')
    assert acct.rate_park_until > time.time()   # 被限流停住
    assert acct.parked_until == 0.0             # 预算没动

    # 预算触顶时也照样拒绝，且不碰 rate_park
    burner2 = _burner(weekly_credits=100, window_credits=100, safety_margin=0.5)
    acct2 = burner2.keys[0].account
    acct2.events = [(time.time(), 90.0)]
    assert burner2.budget_allow(acct2, 30.0) is False
    assert acct2.parked_until > time.time()
    assert acct2.rate_park_until == 0.0
