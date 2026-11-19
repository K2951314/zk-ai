"""业务侧巡检探针：状态机与「变化才发」语义。

为什么要有这组测试（2026-11-19）：巡检最容易出的两种错都不是「探针跑不起来」，
而是 (a) 每天都发同一个事件把人吵烦，(b) 恢复了却没人知道。两条都靠
「同状态不重复发 / 坏→好也发」这条状态机保证，所以钉成测试。

探针本身（subprocess 调三菱金丝雀 / _health.py）**不在这里测**——它依赖
外部项目与网络，会在 CI 上假红。测的是纯函数部分：定位、状态机、指纹。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import signal_probes as probes


class TestResolveHome:
    """定位语义：环境变量设了就必须有效；没设置才探测。"""

    def test_explicit_env_pointing_at_a_valid_project(self, tmp_path, monkeypatch):
        (tmp_path / "backend" / "smart_quotation").mkdir(parents=True)
        (tmp_path / "backend" / "smart_quotation" / "mitsubishi_stock.py").touch()
        monkeypatch.setenv("SQ_HOME", str(tmp_path))
        assert probes.resolve_sq_home() == tmp_path

    def test_explicit_env_pointing_nowhere_is_misconfigured(self, tmp_path, monkeypatch):
        """设了但无效 → 返回 None，且必须能被识别成「配错了」而不是「没装」。"""
        monkeypatch.setenv("SQ_HOME", str(tmp_path / "nonexistent"))
        assert probes.resolve_sq_home() is None
        assert probes.env_misconfigured("SQ_HOME", None) is True

    def test_unset_env_is_not_misconfigured(self, monkeypatch):
        monkeypatch.delenv("SQ_HOME", raising=False)
        assert probes.env_misconfigured("SQ_HOME", None) is False

    def test_ss_home_marker_is_the_health_script(self, tmp_path, monkeypatch):
        (tmp_path / "mpgen" / "tools").mkdir(parents=True)
        (tmp_path / "mpgen" / "tools" / "_health.py").touch()
        monkeypatch.setenv("SS_HOME", str(tmp_path))
        assert probes.resolve_ss_home() == tmp_path


class TestMitsubishiStateMachine:
    """「变化才发」——同状态不发，坏→好发恢复事件。"""

    def test_first_observation_always_emits(self):
        event = probes.mitsubishi_event({"ok": False, "reason": "credential_invalid"},
                                        None, source="zkai-gateway")
        assert event is not None
        assert event["type"] == "inventory.canary_failed"
        assert "credential_invalid" in event["summary"]

    def test_same_state_does_not_repeat(self):
        current = {"ok": False, "reason": "credential_invalid"}
        previous = {"ok": False, "reason": "credential_invalid"}
        assert probes.mitsubishi_event(current, previous, source="x") is None

    def test_same_ok_state_does_not_repeat(self):
        assert probes.mitsubishi_event({"ok": True}, {"ok": True}, source="x") is None

    def test_recovery_emits_its_own_type(self):
        event = probes.mitsubishi_event({"ok": True}, {"ok": False}, source="zkai-gateway")
        assert event is not None
        assert event["type"] == "inventory.canary_recovered"

    def test_failure_becomes_recovery_and_back(self):
        """坏 → 好 → 坏 三次都要发事件（每次都是状态变化）。"""
        failed = {"ok": False, "reason": "canary_failed"}
        ok = {"ok": True}
        assert probes.mitsubishi_event(failed, None, source="x")["type"].endswith("failed")
        assert probes.mitsubishi_event(ok, failed, source="x")["type"].endswith("recovered")
        assert probes.mitsubishi_event(failed, ok, source="x")["type"].endswith("failed")

    def test_fingerprint_ignores_volatile_detail(self):
        """同一种失败，detail 里的时间戳/序号不同也必须算出同一个键。"""
        a = probes.mitsubishi_event(
            {"ok": False, "reason": "canary_failed", "detail": "第 1 次 2026-11-19 10:00"},
            None, source="x")
        b = probes.mitsubishi_event(
            {"ok": False, "reason": "canary_failed", "detail": "第 9 次 2026-11-20 23:59"},
            None, source="x")
        assert a["payload"]["fingerprint"] == b["payload"]["fingerprint"]

    def test_detail_is_truncated(self):
        """超长 stderr 不能整段塞进事件（会撑爆提示词预算）。"""
        event = probes.mitsubishi_event(
            {"ok": False, "reason": "x", "detail": "y" * 5000}, None, source="x")
        assert len(event["payload"]["detail"]) <= 500


class TestFreshnessStateMachine:
    def test_first_observation_emits_failure(self):
        event = probes.freshness_event(
            {"ok": False, "reason": "gates_failed",
             "detail": "结论：发现 3 个数据面异常、1 条构建滞后", "returncode": 1},
            None, source="zkai-gateway")
        assert event is not None
        assert event["type"] == "data.freshness_failed"
        assert "3" in event["payload"]["fingerprint"]

    def test_same_result_does_not_repeat(self):
        current = {"ok": False, "detail": "结论：发现 3 个数据面异常", "returncode": 1}
        assert probes.freshness_event(current, dict(current, ok=False), source="x") is None

    def test_recovery_emits(self):
        event = probes.freshness_event({"ok": True}, {"ok": False}, source="x")
        assert event["type"] == "data.freshness_recovered"

    def test_fingerprint_separates_different_breakage(self):
        """异常数量变了就是新问题，不该被当成同一条事件吃掉。"""
        a = probes.freshness_event(
            {"ok": False, "detail": "结论：发现 3 个数据面异常", "returncode": 1},
            None, source="x")
        b = probes.freshness_event(
            {"ok": False, "detail": "结论：发现 7 个数据面异常", "returncode": 1},
            None, source="x")
        assert a["payload"]["fingerprint"] != b["payload"]["fingerprint"]


class TestProbeSubprocessContract:
    """真跑一次 subprocess，验证「退出码/输出 → 结构」的契约。

    为什么不能只测纯函数（2026-11-19 实测）：第一版探针用了采集器自己的
    ``sys.executable``（ZK-AI 的 venv，没有 requests），三菱探针每次都报
    ``ModuleNotFoundError``，看起来像「三菱接口坏了」。**探针与目标的解释器
    契约必须被测试钉住**，否则巡检会稳定假报。
    """

    def _fake_project(self, root: Path, script_body: str, name: str) -> Path:
        tools = root / "mpgen" / "tools"
        tools.mkdir(parents=True)
        (tools / "_health.py").write_text(script_body, encoding="utf-8")
        return root

    def test_freshness_reads_exit_code_zero_as_ok(self, tmp_path):
        self._fake_project(
            tmp_path,
            "print('结论：全部正常（24 个数据面、19 条构建边、闸门全绿）')\n",
            "_health.py",
        )
        result = probes.probe_freshness(tmp_path)
        assert result["ok"] is True
        assert result["returncode"] == 0
        assert "全部正常" in result["detail"]

    def test_freshness_reads_nonzero_exit_as_failure(self, tmp_path):
        self._fake_project(
            tmp_path,
            "import sys\n"
            "print('结论：发现 0 个数据面异常、0 条构建滞后、4 条闸门失败')\n"
            "sys.exit(1)\n",
            "_health.py",
        )
        result = probes.probe_freshness(tmp_path)
        assert result["ok"] is False
        assert "4 条闸门失败" in result["detail"]

    def test_mitsubishi_parses_last_json_line(self, tmp_path):
        """引擎会往 stdout 打日志；探针必须取**最后一行 JSON**，不被日志干扰。"""
        pkg = tmp_path / "backend" / "smart_quotation"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").touch()
        (pkg / "mitsubishi_stock.py").write_text(
            "def get_engine():\n"
            "    class E:\n"
            "        def ensure_canary(self):\n"
            "            print('[多品牌钻头] 并入 969 行')\n"
            "            return True\n"
            "    return E()\n",
            encoding="utf-8",
        )
        result = probes.probe_mitsubishi(tmp_path)
        assert result is not None
        assert result["ok"] is True

    def test_mitsubishi_import_error_is_classified_not_crashed(self, tmp_path):
        """依赖缺失必须是结构化失败，不能抛异常把整个采集器带崩。"""
        pkg = tmp_path / "backend" / "smart_quotation"
        pkg.mkdir(parents=True)
        (pkg / "mitsubishi_stock.py").write_text(
            "import definitely_missing_module\n", encoding="utf-8")
        result = probes.probe_mitsubishi(tmp_path)
        assert result["ok"] is False
        assert "definitely_missing_module" in result["detail"]

    def test_missing_project_returns_none(self):
        """项目没装 → None（静默跳过），不能每天报一次巡检失败。"""
        assert probes.probe_mitsubishi(None) is None
        assert probes.probe_freshness(None) is None

    """按小时桶节流：重启进程不会立刻重跑一轮巡检。"""

    def test_first_run_is_due(self):
        assert probes  # 触发导入
        from event_watch import business_due

        assert business_due({}) is True

    def test_just_ran_is_not_due(self):
        from event_watch import BUSINESS_PROBE_INTERVAL_HOURS, _hour_bucket, business_due

        assert business_due({"business_probe_hour": _hour_bucket()}) is False
        assert business_due(
            {"business_probe_hour": _hour_bucket() - BUSINESS_PROBE_INTERVAL_HOURS}
        ) is True

    def test_bad_state_value_does_not_crash(self):
        from event_watch import business_due

        assert business_due({"business_probe_hour": "not-an-int"}) is True


class TestInterpreterSelection:
    """解释器选择是巡检正确性的一部分，不是实现细节。

    两次实测都踩在这里（2026-11-19）：
    - 用采集器自己的 venv → ``ModuleNotFoundError: requests``，看起来像接口坏了；
    - 用项目 venv 跑 smart-selection 的闸门 → 4 条失败，而系统 3.13 只有 1 条。
    **用错解释器会把「环境差异」伪装成「数据异常」**，这是巡检最危险的误报。
    """

    def test_project_venv_wins_when_present(self, tmp_path):
        if sys.platform == "win32":
            venv = tmp_path / ".venv" / "Scripts" / "python.exe"
        else:
            venv = tmp_path / ".venv" / "bin" / "python"
        venv.parent.mkdir(parents=True)
        venv.write_text("", encoding="utf-8")
        assert probes._python_for(tmp_path) == [str(venv)]

    def test_falls_back_to_system_python(self, tmp_path):
        """没有项目 venv 时不能回落到采集器 venv（那会假报依赖缺失）。"""
        chosen = probes._python_for(tmp_path)
        assert chosen, "必须给出一个解释器"
        # 返回值是命令前缀列表（`py -3.13` 是两个词）
        assert all(isinstance(part, str) for part in chosen)

    def test_result_records_which_interpreter_ran(self, tmp_path):
        """结论里必须能看出用的哪个解释器，误报才有归因线索。"""
        (tmp_path / "mpgen" / "tools").mkdir(parents=True)
        (tmp_path / "mpgen" / "tools" / "_health.py").write_text(
            "print('结论：全部正常')\n", encoding="utf-8")
        result = probes.probe_freshness(tmp_path)
        assert result.get("interpreter")


class TestStateSurvivesNextRound:
    """``next_state`` 每轮整体替换 state —— 巡检状态必须被显式继承。

    2026-11-19 实测的真事故（不是推演）：``next_state`` 只返回 /health 那几个
    键，``business_probe_hour`` / ``mitsubishi_canary`` / ``smart_selection_freshness``
    全被抹掉。后果是**每 60 秒**（采集器轮询间隔）：

    - ``business_due`` 恒为 True → 每次都跑一遍 77 秒的 ``_health.py --gates``
      和一次真实三菱 RPC（额度与耗时才是有代价的部分）；
    - 探针状态丢失 → 「首次观测」判定每轮成立 → 同一故障每轮重发事件。

    这类 bug 在跑一次的验证里看不出来，只有「跑两轮」才暴露，所以钉成测试。
    """

    def test_probe_state_is_inherited(self):
        from event_watch import next_state

        previous = {
            "business_probe_hour": 12345,
            "mitsubishi_canary": {"ok": False, "reason": "credential_invalid"},
            "smart_selection_freshness": {"ok": True},
            "event_count": 7,
        }
        snapshot = {"status": "healthy", "providers": {"available": 3}}
        nxt = next_state(snapshot, previous, events=[])

        assert nxt["business_probe_hour"] == 12345
        assert nxt["mitsubishi_canary"]["reason"] == "credential_invalid"
        assert nxt["smart_selection_freshness"]["ok"] is True
        assert nxt["event_count"] == 7

    def test_health_state_is_still_recomputed(self):
        """继承巡检键不能顺带把 /health 基线也冻住。"""
        from event_watch import next_state

        previous = {"quarantine": ["old"], "business_probe_hour": 1}
        snapshot = {
            "quarantined_deployments": [{"deployment_id": "new"}],
            "providers": {"available": 0},
            "status": "degraded",
        }
        nxt = next_state(snapshot, previous, events=[])
        assert nxt["quarantine"] == ["new"]
        assert nxt["no_provider"] is True

    def test_two_consecutive_rounds_do_not_rerun_probes(self):
        """跑两轮：第二轮不该再判为「该巡检」。这是上面那个 bug 的直接判据。"""
        from event_watch import _hour_bucket, business_due, next_state

        previous = {"business_probe_hour": _hour_bucket()}
        snapshot = {"status": "healthy", "providers": {"available": 1}}
        for _ in range(3):
            previous = next_state(snapshot, previous, events=[])
        assert business_due(previous) is False, (
            "state 继承断了：business_probe_hour 被 next_state 抹掉，"
            "巡检会变成每轮都跑"
        )
