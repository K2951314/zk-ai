"""systemctl 执行层：报错解读 + 自重启分离 + 网关/消耗器分流。

三条不变量：
1. 报错必须翻译成人话——NoNewPrivileges / polkit 未放行都指向 50-zkai.rules。
2. 网关重启自己走 ``--no-block`` + 延迟分离，不许同步跑（会把自己 SIGTERM 在响应里）。
3. 不再尝试 ``sudo -n``——在 NoNewPrivileges 下必然失败，还把真报错盖掉。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.services import gateway_restart, systemd_ctl
from app.services.burner_service import restart as burner_restart


class TestErrorInterpretation:
    def test_no_new_privileges_points_to_polkit_rule(self) -> None:
        msg = systemd_ctl._interpret_failure(
            'sudo: The "no new privileges" flag is set, which prevents sudo from running as root.',
            "zkai", "restart",
        )
        assert "50-zkai.rules" in msg
        assert "NoNewPrivileges" in msg or "polkit" in msg

    def test_polkit_denial_points_to_polkit_rule(self) -> None:
        msg = systemd_ctl._interpret_failure(
            "Failed to restart zkai.service: Interactive authentication required.",
            "zkai", "restart",
        )
        assert "50-zkai.rules" in msg
        assert "polkit" in msg

    def test_generic_failure_keeps_raw_stderr(self) -> None:
        msg = systemd_ctl._interpret_failure(
            "Unit zkai.service not found.", "zkai", "restart"
        )
        assert "not found" in msg


class TestRunUnitAction:
    def test_rejects_unknown_verb(self) -> None:
        r = systemd_ctl.run_unit_action("zkai", "explode")
        assert r["ok"] is False
        assert "不支持" in r["message"]

    def test_detached_mode_returns_ok_immediately(self, monkeypatch) -> None:
        """自重启：Popen 分离进程后立即 ok，不等 systemctl 返回。"""
        popped: list[list[str]] = []

        def fake_popen(cmd, **kw):
            popped.append(cmd)
            return MagicMock()

        monkeypatch.setattr(systemd_ctl.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(systemd_ctl, "find_systemctl", lambda: "/usr/bin/systemctl")
        r = systemd_ctl.run_unit_action("zkai", "restart", detached_delay=1.0)
        assert r["ok"] is True
        assert r["mode"] == "systemd"
        assert popped, "必须用 Popen 分离进程"
        assert "--no-block" in popped[0][2]
        assert "restart" in popped[0][2] and "zkai" in popped[0][2]

    def test_sync_mode_reports_success(self, monkeypatch) -> None:
        proc = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(systemd_ctl.subprocess, "run", lambda *a, **k: proc)
        monkeypatch.setattr(systemd_ctl, "find_systemctl", lambda: "/usr/bin/systemctl")
        r = systemd_ctl.run_unit_action("zkai-burner", "restart")
        assert r["ok"] is True
        assert "zkai-burner" in r["message"]

    def test_sync_mode_translates_failure(self, monkeypatch) -> None:
        proc = MagicMock(
            returncode=1, stdout="",
            stderr='sudo: The "no new privileges" flag is set',
        )
        monkeypatch.setattr(systemd_ctl.subprocess, "run", lambda *a, **k: proc)
        monkeypatch.setattr(systemd_ctl, "find_systemctl", lambda: "/usr/bin/systemctl")
        r = systemd_ctl.run_unit_action("zkai-burner", "restart")
        assert r["ok"] is False
        assert "50-zkai.rules" in r["message"]


class TestGatewayRestartSelf:
    def test_gateway_self_restart_uses_detached(self, monkeypatch) -> None:
        """网关重启自己必须分离 + 延迟，不能同步跑。"""
        captured: dict = {}

        def fake_run_unit_action(unit, verb, *, detached_delay=0.0):
            captured.update(unit=unit, verb=verb, detached_delay=detached_delay)
            return {"mode": "systemd", "ok": True, "message": "ok"}

        monkeypatch.setattr(gateway_restart, "detect_mode", lambda: "systemd")
        monkeypatch.setattr(gateway_restart, "run_unit_action", fake_run_unit_action)
        r = gateway_restart.restart_gateway(Path("data"))
        assert r["ok"] is True
        assert captured["unit"] == "zkai"
        assert captured["detached_delay"] > 0, "自重启必须延迟分离"

    def test_tray_mode_still_writes_request_file(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(gateway_restart, "detect_mode", lambda: "tray")
        r = gateway_restart.restart_gateway(tmp_path)
        assert r["ok"] is True
        assert (tmp_path / "gateway_restart.request").exists()


class TestBurnerRestartRouting:
    def test_burner_restart_is_sync_not_detached(self, monkeypatch) -> None:
        """重启消耗器是别的进程，同步跑即可，不需要延迟分离。"""
        captured: dict = {}

        def fake_run_unit_action(unit, verb, *, detached_delay=0.0):
            captured.update(unit=unit, verb=verb, detached_delay=detached_delay)
            return {"mode": "systemd", "ok": True, "message": "ok"}

        monkeypatch.setattr(burner_restart, "detect_restart_mode", lambda _d: "systemd")
        monkeypatch.setattr(burner_restart, "run_unit_action", fake_run_unit_action)
        r = burner_restart.request_restart_ex(Path("data"))
        assert r["ok"] is True
        assert captured["unit"] == "zkai-burner"
        assert captured["detached_delay"] == 0

    def test_no_sudo_in_action_command(self) -> None:
        """执行层不该拼 sudo——那条路在 NoNewPrivileges 下必死。"""
        import inspect

        src = inspect.getsource(systemd_ctl.run_unit_action)
        assert '"sudo"' not in src and "'sudo'" not in src
