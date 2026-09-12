"""端口守卫 ``scripts/port_guard.py`` 的行为契约。

只测**纯逻辑与决策分支**；真正会杀进程 / 发请求的部分全部用替身替掉，
保证测试零副作用（绝不会真的结束任何进程）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# port_guard 是脚本不是包模块，手动加载
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "port_guard.py"
_spec = importlib.util.spec_from_file_location("port_guard", _SCRIPT)
port_guard = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
assert _spec is not None and _spec.loader is not None
sys.modules.setdefault("port_guard", port_guard)
_spec.loader.exec_module(port_guard)


# --------------------------------------------------------------------------- #
# _project_path_evidence：命令行特征判断
# --------------------------------------------------------------------------- #
class TestProjectPathEvidence:
    def test_absolute_project_path_is_ours(self) -> None:
        assert port_guard._project_path_evidence(
            rf"{port_guard.PROJECT_ROOT}\.venv\Scripts\python.exe -m uvicorn app.main:app"
        )

    def test_relative_uvicorn_app_main_is_ours(self) -> None:
        """从项目内用相对路径启动：命令行里没有完整项目路径，但有 app.main:app。"""
        assert port_guard._project_path_evidence(
            "python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
        )

    def test_unrelated_command_is_foreign(self) -> None:
        assert not port_guard._project_path_evidence(
            r"C:\other\service.exe --port 8000"
        )

    def test_empty_command_is_foreign(self) -> None:
        assert not port_guard._project_path_evidence("")

    def test_another_python_script_is_foreign(self) -> None:
        """同为 python 脚本但不是本网关（回归：曾误判 _fake_service.py 之外的场景）。"""
        assert not port_guard._project_path_evidence(
            r"python.exe C:\somewhere\else\server.py"
        )

    def test_project_name_without_uvicorn_is_not_enough(self) -> None:
        """路径里带 zk-ai 但不是 uvicorn（例如资源管理器开着的目录）不算。"""
        assert not port_guard._project_path_evidence(
            rf"{port_guard.PROJECT_ROOT}\docs\editor.exe"
        )


# --------------------------------------------------------------------------- #
# _is_our_process：命令行 + /health 探测的组合判定
# --------------------------------------------------------------------------- #
class TestIsOurProcess:
    def test_cmdline_match_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            port_guard, "_command_line", lambda pid: "python -m uvicorn app.main:app"
        )
        # /health 不应被调用
        monkeypatch.setattr(
            port_guard,
            "_pid_owns_port_via_http",
            lambda pid: pytest.fail("cmdline 命中时不应再探测 /health"),
        )
        ours, _desc = port_guard._is_our_process(123)
        assert ours

    def test_health_probe_rescues_unreadable_cmdline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """wmic/CIM 被安全策略拦掉时，靠 /health 兜底识别（实测回归场景）。"""
        monkeypatch.setattr(port_guard, "_command_line", lambda pid: "")
        monkeypatch.setattr(port_guard, "_pid_owns_port_via_http", lambda pid: True)
        ours, desc = port_guard._is_our_process(123)
        assert ours
        assert "health" in desc.lower() or "health" in desc

    def test_unreadable_and_no_probe_is_foreign(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """既读不到命令行又探测不到 /health —— 必须按外来进程处理，绝不误杀。"""
        monkeypatch.setattr(port_guard, "_command_line", lambda pid: "")
        monkeypatch.setattr(port_guard, "_pid_owns_port_via_http", lambda pid: False)
        ours, _desc = port_guard._is_our_process(123)
        assert not ours

    def test_foreign_cmdline_is_foreign_even_if_probe_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            port_guard, "_command_line", lambda pid: r"C:\elsewhere\app.exe"
        )
        monkeypatch.setattr(port_guard, "_pid_owns_port_via_http", lambda pid: False)
        ours, _desc = port_guard._is_our_process(123)
        assert not ours


# --------------------------------------------------------------------------- #
# main() 的退出码契约（监听列表全部替身化，零副作用）
# --------------------------------------------------------------------------- #
class TestMainExitCodes:
    def _patch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        listeners: list[int],
        verdicts: dict[int, bool],
        kill_ok: bool = True,
        killed: list[int] | None = None,
    ) -> None:
        record = killed if killed is not None else []

        def fake_kill(pid: int) -> bool:
            record.append(pid)
            return kill_ok

        monkeypatch.setattr(port_guard, "_listeners", lambda: listeners)
        monkeypatch.setattr(
            port_guard,
            "_is_our_process",
            lambda pid: (verdicts.get(pid, False), f"cmd-{pid}"),
        )
        monkeypatch.setattr(port_guard, "_kill", fake_kill)

    def test_port_free_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, listeners=[], verdicts={})
        assert port_guard.main() == 0

    def test_own_instance_is_killed_then_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        killed: list[int] = []
        self._patch(
            monkeypatch, listeners=[111], verdicts={111: True}, killed=killed
        )
        assert port_guard.main() == 0
        assert killed == [111]

    def test_foreign_process_returns_one_and_is_never_killed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        killed: list[int] = []
        self._patch(
            monkeypatch, listeners=[222], verdicts={222: False}, killed=killed
        )
        assert port_guard.main() == 1
        assert killed == []  # 关键契约：外来进程绝不动手
        out = capsys.readouterr().out
        assert "其他程序" in out
        assert "cmd-222" in out

    def test_kill_failure_returns_two(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        self._patch(
            monkeypatch, listeners=[333], verdicts={333: True}, kill_ok=False
        )
        assert port_guard.main() == 2
        out = capsys.readouterr().out
        assert "无法结束旧实例" in out

    def test_mixed_ownership_kills_only_ours(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        killed: list[int] = []
        self._patch(
            monkeypatch,
            listeners=[444, 555],
            verdicts={444: True, 555: False},
            killed=killed,
        )
        assert port_guard.main() == 1  # 有外来进程 → 拒绝启动
        assert killed == [444]  # 但自己的旧实例仍然清掉了
        out = capsys.readouterr().out
        assert "cmd-555" in out
