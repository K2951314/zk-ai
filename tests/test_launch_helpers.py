"""First-run bootstrap and console-open helpers (scripts/).

These two scripts remove the manual steps a fresh machine / a restart used to
need: materialising ``.env`` and the live config, and pasting the admin token
into the console by hand.
"""

from __future__ import annotations

from pathlib import Path

from scripts import first_run, open_console


# --------------------------------------------------------------------------- #
# First-run bootstrap
# --------------------------------------------------------------------------- #
def test_first_run_materialises_templates(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env.example").write_text("ZKAI_ADMIN_TOKEN=\nNVIDIA_API_KEY=\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "providers.example.yaml").write_text("providers: []\n", encoding="utf-8")
    monkeypatch.setattr(first_run, "_ROOT", tmp_path)

    assert first_run.ensure_env() == ".env"
    assert first_run.ensure_config_templates() == ["config/providers.yaml"]
    assert (tmp_path / ".env").exists()
    assert (tmp_path / "config" / "providers.yaml").exists()


def test_first_run_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "ZKAI_ADMIN_TOKEN=tok\nNVIDIA_API_KEY=nv-real\nSENSENOVA_API_KEY=s-real\n"
        "MODELSCOPE_API_KEY=m-real\nMOONSHOT_API_KEY=k-real\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(first_run, "_ROOT", tmp_path)
    assert first_run.ensure_env() is None
    assert first_run.ensure_config_templates() == []
    assert first_run.needs_keys() == []  # a filled-in .env has nothing to demand


def test_needs_keys_reports_empty_placeholders(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "NVIDIA_API_KEY=\nSENSENOVA_API_KEY=sk-real\nZKAI_ADMIN_TOKEN=\n", encoding="utf-8"
    )
    monkeypatch.setattr(first_run, "_ROOT", tmp_path)
    assert "NVIDIA_API_KEY" in first_run.needs_keys()
    assert "SENSENOVA_API_KEY" not in first_run.needs_keys()


def test_first_run_exit_code_asks_for_then_lets_go(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / ".env.example").write_text("ZKAI_ADMIN_TOKEN=\nNVIDIA_API_KEY=\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.example.yaml").write_text("admin:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(first_run, "_ROOT", tmp_path)

    created = first_run.main([])
    assert created == 1  # stop the launcher: the operator must fill the blanks
    assert "ZKAI_ADMIN_TOKEN" in capsys.readouterr().out

    # Second run with the values filled in: nothing left to ask, exit 0.
    (tmp_path / ".env").write_text("ZKAI_ADMIN_TOKEN=tok\nNVIDIA_API_KEY=k\n", encoding="utf-8")
    assert first_run.main([]) == 0


# --------------------------------------------------------------------------- #
# Console URL / token carrying
# --------------------------------------------------------------------------- #
def test_env_from_file_reads_plain_and_quoted(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        '# comment\nZKAI_PORT=8317\nexport ZKAI_ADMIN_TOKEN="abc-123"\n', encoding="utf-8"
    )
    monkeypatch.setattr(open_console, "_ROOT", tmp_path)
    assert open_console.env_from_file("ZKAI_PORT") == "8317"
    assert open_console.env_from_file("ZKAI_ADMIN_TOKEN") == "abc-123"
    assert open_console.env_from_file("MISSING", "default") == "default"
    assert open_console.env_from_file("ZKAI_PORT", "999") == "8317"  # a real value wins over the default


def test_console_url_puts_token_in_query_only_when_known() -> None:
    assert open_console.console_url(port=9000) == "http://127.0.0.1:9000/ui"
    assert open_console.console_url(path="ui/agent", port=9000) == "http://127.0.0.1:9000/ui/agent"
    with_token = open_console.console_url(port=9000, token="t0p-secret")
    assert with_token == "http://127.0.0.1:9000/ui?token=t0p-secret"


def test_wait_for_port_fails_fast_when_nothing_listens() -> None:
    # Port 1 is privileged and never used by our tests: no wait storm.
    assert open_console.wait_for_port(1, timeout=0.2) is False


def test_open_console_reads_port_and_token_from_env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("ZKAI_PORT=8399\nZKAI_ADMIN_TOKEN=tok-8399\n", encoding="utf-8")
    monkeypatch.setattr(open_console, "_ROOT", tmp_path)
    opened: list[str] = []
    monkeypatch.setattr(open_console.webbrowser, "open", opened.append)

    assert open_console.open_console(wait=0.3) is False  # nothing is listening
    assert opened == []  # and nothing was opened either
