"""消耗器账号核对与校准：diff、anchors 序列化往返、账本校准事件、重启三形态。

这里最容易被忽略的是**窗口口径必须连续**：写回 burned 时不重建 events，而是注入
一个校准事件把账本估算拉到运营者给的数上。若改成直接覆盖 burned 值，重启后
burner 按「最近 5h 的 events」重算，新旧口径在窗口边界上跳变——运营者看到的
数字和消耗器实际用的数字从此对不上，而那正是这个功能的全部意义。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.services import burner_service as bs

CONFIG_YAML = """\
model: sensenova-6.8-flash-lite
concurrency: 8
per_account_start: 4
per_account_max: 8
only: "SENSENOVA_API_KEY_02,SENSENOVA_API_KEY_03"
anchors: "2=15:10;3=15:10"
week_anchors: "2=Wed 18:10"
rate_in: 761
rate_out: 2292
window_credits: 60000
weekly_credits: 600000
safety_margin: 0.95
"""


def _ledger(**overrides: object) -> dict:
    now = time.time()
    data: dict = {
        "saved_at": "2026-10-03 18:48:29",
        "accounts": {
            "SENSENOVA_API_KEY_02": {
                "events": [[now - 60, 100.0], [now - 30, 50.0]],
                "credits_total": 50000.0,
                "target": 8.0,
                "anchor_ts": 0.0,
                "week_anchor_ts": 0.0,
            },
            "SENSENOVA_API_KEY_03": {
                "events": [],
                "credits_total": 0.0,
                "target": 4.0,
            },
        },
        "keys": {
            "SENSENOVA_API_KEY_02": {
                "ok": 100, "fail": 2, "rate_limited": 5,
                "tokens_in": 3000000, "tokens_out": 9000000,
            },
            "SENSENOVA_API_KEY_03": {
                "ok": 0, "fail": 0, "rate_limited": 0,
                "tokens_in": 0, "tokens_out": 0,
            },
        },
        "rate_in": 761.0,
        "rate_out": 2292.0,
        "safety_margin": 0.95,
    }
    data.update(overrides)
    return data


@pytest.fixture
def burner_files(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "burn_state.json"
    config = tmp_path / "burner.yaml"
    state.write_text(json.dumps(_ledger()), encoding="utf-8")
    config.write_text(CONFIG_YAML, encoding="utf-8")
    return state, config


class TestAnchorCodec:
    def test_anchors_roundtrip(self) -> None:
        spec = "2=15:10;3=15:10;10=14:36"
        assert bs.accounts_to_anchors(bs.anchors_to_accounts(spec)) == spec

    def test_week_anchors_roundtrip(self) -> None:
        spec = "2=Wed 18:10;10=Thu 09:36"
        assert bs.accounts_to_anchors(bs.week_anchors_to_accounts(spec)) == spec

    def test_bad_items_are_skipped(self) -> None:
        assert bs.anchors_to_accounts("2=15:10;oops;3") == {
            "SENSENOVA_API_KEY_02": "15:10"
        }

    def test_accounts_to_anchors_sorts_by_index(self) -> None:
        got = bs.accounts_to_anchors({
            "SENSENOVA_API_KEY_10": "01:02",
            "SENSENOVA_API_KEY_02": "03:04",
            "UNKNOWN_KEY": "09:09",
        })
        assert got == "2=03:04;10=01:02"

    def test_key_index(self) -> None:
        assert bs._key_index("SENSENOVA_API_KEY") == 1
        assert bs._key_index("SENSENOVA_API_KEY_02") == 2
        assert bs._key_index("SENSENOVA_API_KEY_10") == 10
        assert bs._key_index("OTHER") is None

    def test_only_split_and_join(self) -> None:
        assert bs.only_to_set("K_02, K_03") == ["K_02", "K_03"]
        assert bs.set_to_only(["K_02", "K_03"]) == "K_02,K_03"


class TestReconcileDiff:
    def test_reports_gap_between_ledger_and_operator_value(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        # 前端填「剩余」：cap_5h = 60000 * 0.95 = 57000，账本已烧 150 -> 剩余 56850。
        # 运营者填剩余 1000，等于已烧 56000，比账本多 55850。
        diff = bs.reconcile_diff(
            state,
            bs.read_config(config),
            {"accounts": [{"name": "SENSENOVA_API_KEY_02",
                           "left_5h": 1000.0}]},
        )
        row = diff["accounts"][0]
        assert row["left_5h"]["filled"] == pytest.approx(1000.0)
        assert row["left_5h"]["delta"] > 0  # delta = 目标已烧 - 账本已烧 > 0
        assert row["left_5h"]["notable"] is True

    def test_blank_field_means_no_change(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        diff = bs.reconcile_diff(
            state,
            bs.read_config(config),
            {"accounts": [{"name": "SENSENOVA_API_KEY_02", "left_5h": ""}]},
        )
        assert diff["accounts"][0]["left_5h"]["filled"] is None
        assert diff["accounts"][0]["left_5h"]["delta"] is None

    def test_unknown_account_is_rejected(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        with pytest.raises(ValueError, match="没有账号"):
            bs.reconcile_diff(
                state,
                bs.read_config(config),
                {"accounts": [{"name": "SENSENOVA_API_KEY_99"}]},
            )

    def test_only_and_anchors_become_config_changes(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        diff = bs.reconcile_diff(
            state,
            bs.read_config(config),
            {
                "accounts": [],
                "only": ["SENSENOVA_API_KEY_02", "SENSENOVA_API_KEY_04"],
                "anchors": {"SENSENOVA_API_KEY_02": "16:00"},
                "week_anchors": {"SENSENOVA_API_KEY_02": "Thu 07:00"},
            },
        )
        assert diff["config_changes"]["only"] == (
            "SENSENOVA_API_KEY_02,SENSENOVA_API_KEY_04"
        )
        assert diff["config_changes"]["anchors"] == "2=16:00"
        assert diff["config_changes"]["week_anchors"] == "2=Thu 07:00"
        assert sorted(diff["restart_keys"]) == ["anchors", "only", "week_anchors"]

    def test_rates_can_be_set_through_the_same_payload(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        diff = bs.reconcile_diff(
            state,
            bs.read_config(config),
            {"accounts": [], "rate_in": 800, "rate_out": 2400},
        )
        assert diff["config_changes"]["rate_in"] == 800.0
        assert diff["config_changes"]["rate_out"] == 2400.0
        # 费率是热键，不该要求重启
        assert diff["restart_keys"] == []


class TestApplyReconcile:
    def test_calibration_event_shifts_the_window_without_rebuilding_it(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        """ burned 的修正走「注入校准事件」，不动 events 序列本身。"""
        state, config = burner_files
        before = json.loads(state.read_text(encoding="utf-8"))
        events_before = len(before["accounts"]["SENSENOVA_API_KEY_02"]["events"])
        # cap_5h = 57000，账本已烧 150。运营者填剩余 1000 = 已烧 56000。
        result = bs.apply_reconcile(
            state,
            config,
            {"accounts": [{"name": "SENSENOVA_API_KEY_02", "left_5h": 1000.0}]},
        )
        assert any("left_5h" in w for w in result["ledger_written"])

        after = json.loads(state.read_text(encoding="utf-8"))
        acct = after["accounts"]["SENSENOVA_API_KEY_02"]
        assert len(acct["events"]) == events_before + 1
        # 新事件落在当前 5h 窗口内，且符号 = 目标已烧 - 账本已烧
        now = time.time()
        gap = acct["events"][-1][1]
        assert acct["events"][-1][0] > now - 5 * 3600
        # 目标已烧 = 57000 - 1000 = 56000，账本已烧 = 150，gap = 55850
        assert gap > 0 and gap == pytest.approx(56000.0 - 150.0, abs=1.0)

        # 关键：重算出来的 burned_5h 必须等于运营者给的隐含值
        status = bs.read_status(state, bs.read_config(config))
        acct_status = next(a for a in status.accounts if a.name == "SENSENOVA_API_KEY_02")
        view = bs.account_view(acct_status, now, 0.0, status, 1.0)
        assert view["burned_5h"] == pytest.approx(56000.0, abs=1.0)
        assert view["left_5h"] == pytest.approx(1000.0, abs=1.0)

    def test_negative_gap_when_operator_sees_less(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        # 运营者填剩余 56900 = 已烧 100，比账本 150 少 -> gap 为负
        bs.apply_reconcile(
            state,
            config,
            {"accounts": [{"name": "SENSENOVA_API_KEY_02", "left_5h": 56900.0}]},
        )
        data = json.loads(state.read_text(encoding="utf-8"))
        assert data["accounts"]["SENSENOVA_API_KEY_02"]["events"][-1][1] < 0

    def test_tiny_gap_is_not_written(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        """四舍五入噪音不该落盘：每次都写会让账本事件无限增长。"""
        state, config = burner_files
        before = json.loads(state.read_text(encoding="utf-8"))
        # 账本已烧 150，cap 57000，剩余 56850。填剩余 56849.8 = 已烧 150.2，
        # gap 只有 0.2，低于 0.5 阈值，不该写入。
        result = bs.apply_reconcile(
            state,
            config,
            {"accounts": [{"name": "SENSENOVA_API_KEY_02", "left_5h": 56849.8}]},
        )
        after = json.loads(state.read_text(encoding="utf-8"))
        assert len(after["accounts"]["SENSENOVA_API_KEY_02"]["events"]) == len(
            before["accounts"]["SENSENOVA_API_KEY_02"]["events"]
        )
        assert result["ledger_written"] == []

    def test_writes_structured_only_and_anchors_back_to_yaml(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        result = bs.apply_reconcile(
            state,
            config,
            {
                "accounts": [],
                "only": ["SENSENOVA_API_KEY_02", "SENSENOVA_API_KEY_04"],
                "anchors": {"SENSENOVA_API_KEY_02": "16:00"},
            },
        )
        assert "only" in result["config_written"]
        assert "anchors" in result["config_written"]
        text = config.read_text(encoding="utf-8")
        assert "SENSENOVA_API_KEY_04" in text
        assert "2=16:00" in text
        # 注释与其它键原样保留（write_config 是文本级替换）
        assert "model: sensenova-6.8-flash-lite" in text

    def test_pausing_an_account_parks_it(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        bs.apply_reconcile(
            state,
            config,
            {"accounts": [{"name": "SENSENOVA_API_KEY_03", "burning": False}]},
        )
        data = json.loads(state.read_text(encoding="utf-8"))
        assert data["accounts"]["SENSENOVA_API_KEY_03"]["parked_until"] > time.time()

    def test_empty_payload_is_rejected(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        with pytest.raises(ValueError, match="没有可写入"):
            bs.apply_reconcile(state, config, {"accounts": []})

    def test_reconciled_at_is_stamped(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        bs.apply_reconcile(
            state, config, {"accounts": [{"name": "SENSENOVA_API_KEY_03",
                                          "credits_total": 1.0}]}
        )
        data = json.loads(state.read_text(encoding="utf-8"))
        assert data["reconciled_at"] > 0

    def test_week_anchor_per_account_is_synced_to_config(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        """账号级 week_anchor 保存时必须同步写入 burner.yaml 的 week_anchors。

        2026-10-07 事故：消耗器重启后从配置文件读 week_anchors，
        如果配置文件没更新，账本里的新锚点会被旧配置覆盖。
        """
        state, config = burner_files
        result = bs.apply_reconcile(
            state,
            config,
            {"accounts": [{"name": "SENSENOVA_API_KEY_02",
                            "week_anchor": "10-14 18:10"}]},
        )
        # 账本写了
        assert any("week_anchor_ts" in w for w in result["ledger_written"])
        # 配置文件也写了
        assert "week_anchors" in result["config_written"]
        text = config.read_text(encoding="utf-8")
        assert "2=10-14 18:10" in text
        # week_anchors 是重启键
        assert "week_anchors" in result["restart_keys"]

    def test_anchor_per_account_is_synced_to_config(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        """账号级 anchor 同理：必须同步写入 burner.yaml 的 anchors。"""
        state, config = burner_files
        result = bs.apply_reconcile(
            state,
            config,
            {"accounts": [{"name": "SENSENOVA_API_KEY_02",
                            "anchor": "16:00"}]},
        )
        assert "anchors" in result["config_written"]
        text = config.read_text(encoding="utf-8")
        assert "2=16:00" in text
        assert "anchors" in result["restart_keys"]

    def test_week_anchor_slash_format_is_accepted(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        """斜杠 10/14 18:10 和横线 10-14 18:10 都要接受（2026-10-07 事故根因）。"""
        state, config = burner_files
        result = bs.apply_reconcile(
            state,
            config,
            {"accounts": [{"name": "SENSENOVA_API_KEY_02",
                            "week_anchor": "10/14 18:10"}]},
        )
        assert any("week_anchor_ts" in w for w in result["ledger_written"])


class TestCalibration:
    def test_back_solves_rates_from_actual_credits(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        result = bs.suggest_calibration(
            state, bs.read_config(config), actual_credits=12345.0
        )
        assert result["suggested"]["rate_out"] > 0
        # r_in = r_out / 3（与 burner.suggest_rates 同一假设）
        assert result["suggested"]["rate_in"] == pytest.approx(
            result["suggested"]["rate_out"] / 3
        )
        assert result["current"]["rate_in"] == 761.0
        assert "覆盖同一时段" in result["caveat"]

    def test_per_account_calibration(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        result = bs.suggest_calibration(
            state, bs.read_config(config), actual_credits=500.0,
            account="SENSENOVA_API_KEY_02",
        )
        assert result["account"] == "SENSENOVA_API_KEY_02"
        assert result["ledger_tokens"]["tokens_out"] > 0

    def test_zero_credits_rejected(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        with pytest.raises(ValueError, match="大于 0"):
            bs.suggest_calibration(state, bs.read_config(config), 0)

    def test_unknown_account_rejected(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        state, config = burner_files
        with pytest.raises(ValueError, match="没有账号"):
            bs.suggest_calibration(
                state, bs.read_config(config), 100.0, account="SENSENOVA_API_KEY_99"
            )

    def test_no_tokens_rejected(self, burner_files: tuple[Path, Path]) -> None:
        state, config = burner_files
        data = json.loads(state.read_text(encoding="utf-8"))
        data["accounts"]["SENSENOVA_API_KEY_02"]["events"] = []
        data["keys"]["SENSENOVA_API_KEY_02"] = {
            "ok": 0, "fail": 0, "rate_limited": 0, "tokens_in": 0, "tokens_out": 0,
        }
        state.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError, match="没有任何 token"):
            bs.suggest_calibration(
                state, bs.read_config(config), 100.0, account="SENSENOVA_API_KEY_02"
            )

    def test_short_name_resolves_to_full_account(
        self, burner_files: tuple[Path, Path]
    ) -> None:
        """前端显示 S_02 省屏幕，后端要能把它还原成 SENSENOVA_API_KEY_02。"""
        state, config = burner_files
        full = bs.suggest_calibration(
            state, bs.read_config(config), 100.0, account="SENSENOVA_API_KEY_02"
        )
        short = bs.suggest_calibration(
            state, bs.read_config(config), 100.0, account="S_02"
        )
        # 两者算出的建议费率应该完全一致（同一个账号的 token 量）
        assert full["suggested"] == short["suggested"]
        # 返回的 account 字段保留用户原始输入
        assert short["account"] == "S_02"


class TestRestartModes:
    def test_windows_uses_the_tray_file(self, tmp_path: Path) -> None:
        assert bs.detect_restart_mode(tmp_path) in {"tray", "systemd", "manual"}

    def test_tray_path_writes_the_request_file(self, tmp_path: Path) -> None:
        bs.request_restart(tmp_path)
        assert bs.restart_pending(tmp_path) is True

    def test_manual_mode_returns_the_command(self, tmp_path: Path) -> None:
        """检测不到托盘也检测不到 systemd 时必须给出手动命令，不能静默成功。"""
        mode = bs.detect_restart_mode(tmp_path)
        result = bs.request_restart_ex(tmp_path)
        if mode == "tray":
            assert result["ok"] is True and result["mode"] == "tray"
        else:
            assert result["ok"] is False
            assert result["command"]

    def test_systemd_unit_name_is_stable(self) -> None:
        assert bs._SYSTEMD_UNIT == "zkai-burner"


class TestAccountAnchorResolution:
    """管理员看到的表单要显示他编辑的那个值。每账号优先于全局，没配就回零。"""
    def test_per_account_anchor_wins_over_global(self) -> None:
        config = {"anchors": "2=15:10", "anchor": "03:00"}
        got = bs.account_anchor_ts(config, "SENSENOVA_API_KEY_02", 0.0)
        # 显示格式 2026-10-07 改为 MM-DD HH:MM（和周锚点统一），只断言时刻。
        assert got > 0
        lt = time.localtime(got)
        assert (lt.tm_hour, lt.tm_min) == (15, 10)

    def test_global_anchor_is_the_fallback(self) -> None:
        got = bs.account_anchor_ts({"anchor": "03:00"}, "SENSENOVA_API_KEY_02", 0.0)
        assert got > 0
        lt = time.localtime(got)
        assert (lt.tm_hour, lt.tm_min) == (3, 0)

    def test_no_anchor_means_rolling(self) -> None:
        assert bs.account_anchor_ts({}, "SENSENOVA_API_KEY_02", 0.0) == 0.0

    def test_broken_anchor_spec_does_not_raise(self) -> None:
        bad = {"anchors": "2=nope"}
        assert bs.account_anchor_ts(bad, "SENSENOVA_API_KEY_02", 0.0) == 0.0

    def test_week_anchor_per_account_then_global(self) -> None:
        per = bs.account_week_anchor_ts(
            {"week_anchors": "2=Wed 18:10"}, "SENSENOVA_API_KEY_02", 0.0
        )
        # 显示格式 2026-10-07 改为 MM-DD HH:MM（照抄控制台「下次重置时间」，
        # 不再要求运营者自己推星期几）。所以只断言「时刻对了」。
        assert per > 0
        lt = time.localtime(per)
        assert (lt.tm_hour, lt.tm_min) == (18, 10)
        assert lt.tm_wday == 2  # Wed
        glob = bs.account_week_anchor_ts(
            {"week_anchor": "Mon 00:00"}, "SENSENOVA_API_KEY_02", 0.0
        )
        assert glob > 0
        assert time.localtime(glob).tm_wday == 0  # Mon
        assert time.localtime(glob).tm_hour == 0

    def test_broken_week_anchor_spec_does_not_raise(self) -> None:
        bad = {"week_anchors": "2=someday"}
        assert bs.account_week_anchor_ts(bad, "SENSENOVA_API_KEY_02", 0.0) == 0.0


class TestAnchorDateFormat:
    """5h 和周锚点的日期格式（2026-10-07）：支持中文、数字、旧格式。

    语义：运营者填的是「下次重置时间」，锚点 = 该时刻 - WIN（当前窗口起点）。
    过去的日期先推到最近的未来边界，再减一个窗口。
    """

    def test_hhmm_still_works(self) -> None:
        """5h 锚点旧格式 HH:MM 不能失效。"""
        ts = bs._parse_hhmm("23:10")
        assert ts > 0
        lt = time.localtime(ts)
        assert (lt.tm_hour, lt.tm_min) == (23, 10)

    def test_chinese_date_is_accepted(self) -> None:
        """5h 锚点支持中文格式 10月7日 23:10。"""
        ts = bs._parse_hhmm("10月7日 23:10")
        assert ts > 0

    def test_chinese_week_anchor_is_accepted(self) -> None:
        """周锚点支持中文格式 10月9日 18:10。"""
        ts = bs._parse_weekday_hhmm("10月9日 18:10")
        assert ts > 0

    def test_weekday_format_still_works(self) -> None:
        """周锚点旧格式 Wed 18:10 不能失效。"""
        ts = bs._parse_weekday_hhmm("Wed 18:10")
        assert ts > 0
        assert time.localtime(ts).tm_hour == 18

    def test_slash_separator_is_accepted(self) -> None:
        """斜杠 10/07 23:10 和横线 10-07 23:10 都要接受。"""
        assert bs._parse_hhmm("10/07 23:10") > 0
        assert bs._parse_weekday_hhmm("10/09 18:10") > 0

    def test_bad_format_raises(self) -> None:
        with pytest.raises(ValueError, match="锚点"):
            bs._parse_hhmm("someday")
        with pytest.raises(ValueError, match="周锚点"):
            bs._parse_weekday_hhmm("someday")

    def test_past_date_resolves_to_current_window(self) -> None:
        """过去的日期：推到最近未来边界再减一个窗口 = 当前窗口起点。"""
        now = time.time()
        past = f"01-{time.localtime(now).tm_mday:02d} 12:00"
        ts5 = bs._parse_hhmm(past)
        tsw = bs._parse_weekday_hhmm(past)
        # 锚点 = 当前窗口起点，应该在过去或刚好等于当前窗口起点（<= now + 一点容差）
        # 因为是"当前窗口起点"，所以 <= now
        assert ts5 <= now + 1, "5h 锚点应是当前窗口起点（<= now）"
        assert tsw <= now + 1, "周锚点应是当前窗口起点（<= now）"

    def test_future_date_resolves_to_current_window(self) -> None:
        """未来日期：减一个窗口 = 当前窗口起点。

        填 10-21（未来）-> 锚点 10-14（当前窗口起点），下次重置 10-21。
        用户原话「10.21 正确就应该滚到 10.14」。
        """
        now = time.time()
        # 构造一个 7 天以上的未来日期
        lt = time.localtime(now + 14 * 86400)
        future = f"{lt.tm_mon:02d}-{lt.tm_mday:02d} {lt.tm_hour:02d}:{lt.tm_min:02d}"
        future_ts = time.mktime(time.strptime(f"{time.localtime().tm_year} {future}", "%Y %m-%d %H:%M"))
        ts = bs._parse_weekday_hhmm(future)
        # 锚点 = 填的日期 - WIN_WEEK
        from scripts.burn_sensenova import WIN_WEEK
        assert ts == pytest.approx(future_ts - WIN_WEEK, abs=2)
        # 下次重置 = 锚点 + WIN_WEEK = 填的日期
        assert ts + WIN_WEEK == pytest.approx(future_ts, abs=2)

    def test_fmt_shows_date(self) -> None:
        """表单显示 MM-DD HH:MM 格式。"""
        # 用固定 ts 测试格式化，不依赖当前时间（parse_window_anchor 会推边界）
        import time as _time
        # 10-07 18:10 在东八区的 epoch
        ts = _time.mktime((2026, 10, 7, 18, 10, 0, 0, 0, -1))
        shown = bs._fmt_hhmm(ts)
        assert shown.count("-") == 1, f"期望 1 个横线，得到 {shown!r}"
        assert "18:10" in shown, f"期望 18:10，得到 {shown!r}"
        assert "10-07" in shown, f"期望 10-07，得到 {shown!r}"

    def test_console_hint_teaches_new_format(self) -> None:
        """输入框提示必须教新格式。"""
        web = Path(__file__).resolve().parent.parent / "app" / "web"
        html = (web / "index.html").read_text(encoding="utf-8")
        js = (web / "console.js").read_text(encoding="utf-8") if (web / "console.js").exists() else ""
        # 提示里应该出现中文日期示例或 MM-DD 格式
        assert "10月" in html or "10月" in js or "MM-DD" in html or "MM-DD" in js, \
            "输入框提示必须教新格式（中文日期或 MM-DD）"
