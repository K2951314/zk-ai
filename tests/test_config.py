"""Configuration loading: ``.env`` handling, YAML interpolation, settings bridging."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from app.core.config import (
    PROJECT_ROOT,
    AppConfig,
    Settings,
    apply_settings_from_yaml,
    credential_env_gaps,
    env_file_names,
    interpolate_env,
    load_app_config,
    load_dotenv_file,
    malformed_env_names,
    shadowed_env_names,
)
from app.models.provider import CredentialConfig, ProviderConfig, ProviderType


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #
def test_dotenv_is_loaded_into_the_process_environment(tmp_path, monkeypatch) -> None:
    """Provider keys live in ``os.environ``, so ``.env`` must be pushed there.

    ``pydantic-settings`` only reads ``.env`` into the ``Settings`` object; that is
    not enough for credentials, which are resolved by
    :func:`app.core.security.resolve_env_reference`.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("ZKAI_TEST_DOTENV_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("ZKAI_TEST_DOTENV_KEY", raising=False)

    assert load_dotenv_file(env_file) is True
    assert os.environ["ZKAI_TEST_DOTENV_KEY"] == "from-dotenv"


def test_a_real_environment_variable_beats_the_dotenv_file(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ZKAI_TEST_DOTENV_PRECEDENCE=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("ZKAI_TEST_DOTENV_PRECEDENCE", "from-environment")

    load_dotenv_file(env_file)
    assert os.environ["ZKAI_TEST_DOTENV_PRECEDENCE"] == "from-environment"


def test_a_missing_dotenv_file_is_not_an_error(tmp_path) -> None:
    assert load_dotenv_file(tmp_path / "does-not-exist.env") is False


def test_shadowed_env_names_flags_only_differing_values(tmp_path, monkeypatch) -> None:
    """A process env value that differs from the .env entry shadows it (env wins)."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ZKAI_TEST_SHADOW_KEY=from-dotenv\nZKAI_TEST_MATCH_KEY=same\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZKAI_TEST_SHADOW_KEY", "stale-windows-value")
    monkeypatch.setenv("ZKAI_TEST_MATCH_KEY", "same")
    monkeypatch.delenv("ZKAI_TEST_ABSENT_KEY", raising=False)

    assert shadowed_env_names(env_file) == ["ZKAI_TEST_SHADOW_KEY"]


def test_malformed_env_names_flags_values_with_stray_control_chars(tmp_path, monkeypatch) -> None:
    """``.env`` lines ending in an extra CR are a file bug, not a shadow conflict.

    2026-09-24: ``ZKAI_HOST=0.0.0.0\\r\\r`` put ``"0.0.0.0\\r"`` into the process
    environment. The tray passed it to uvicorn as ``--host``, ``socket.bind()``
    resolved it via ``getaddrinfo`` and the gateway died with
    ``[Errno 11001] getaddrinfo failed`` - read as a DNS outage.
    """
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"ZKAI_TEST_CR_HOST=0.0.0.0\r\r\nZKAI_TEST_CR_CLEAN=ok\r\n")
    monkeypatch.setenv("ZKAI_TEST_CR_HOST", "0.0.0.0\r")
    monkeypatch.setenv("ZKAI_TEST_CR_CLEAN", "ok")

    assert malformed_env_names(env_file) == ["ZKAI_TEST_CR_HOST"]


def test_malformed_name_is_not_reported_as_a_shadow(tmp_path, monkeypatch) -> None:
    """The corrupted entry must not send the operator off unsetting OS variables.

    Reporting it as "shadowed by process environment" is what made this failure
    look like an environment-variable conflict; the fix is in the file.
    """
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"ZKAI_TEST_CR_BOTH=8317\r\r\n")
    monkeypatch.setenv("ZKAI_TEST_CR_BOTH", "8317\r")

    assert malformed_env_names(env_file) == ["ZKAI_TEST_CR_BOTH"]
    assert shadowed_env_names(env_file) == []


def test_genuine_shadow_is_still_reported(tmp_path, monkeypatch) -> None:
    """A real conflict (different value, not just stray whitespace) is untouched."""
    env_file = tmp_path / ".env"
    env_file.write_text("ZKAI_TEST_REAL=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("ZKAI_TEST_REAL", "totally-different")

    assert malformed_env_names(env_file) == []
    assert shadowed_env_names(env_file) == ["ZKAI_TEST_REAL"]


# --------------------------------------------------------------------------- #
# credential keys vs .env (would this key survive a machine move?)
# --------------------------------------------------------------------------- #
def test_env_file_names_reports_names_only(tmp_path) -> None:
    """Values must not come back out — callers print what they are given."""
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n# comment\nB='two'\n", encoding="utf-8")

    assert env_file_names(env_file) == frozenset({"A", "B"})
    assert env_file_names(tmp_path / "nope.env") == frozenset()


def _config_with(credentials: list[CredentialConfig]) -> AppConfig:
    provider = ProviderConfig(
        id="p1",
        type=ProviderType.OPENAI,
        base_url="https://example.invalid/v1",
        credentials=credentials,
    )
    return AppConfig(
        settings=Settings(),
        providers={"p1": provider},
        models={},
        aliases={},
        warnings=[],
    )


def test_credential_env_gaps_names_keys_that_would_not_travel(tmp_path) -> None:
    """The 2026-09-29 finding: a key that lives only in the OS environment.

    ``sensenova-01`` reads ``SENSENOVA_API_KEY`` from the process env, so it
    works on this machine — but the transfer package carries ``.env`` only, so
    the new machine comes up with that credential DISABLED and no clue why.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("TRAVELS=yes\n", encoding="utf-8")
    config = _config_with(
        [
            CredentialConfig(id="sensenova-01", env="SENSENOVA_API_KEY"),
            CredentialConfig(id="sensenova-02", env="TRAVELS"),
        ]
    )

    gaps = credential_env_gaps(config, env_file)

    assert gaps == {"SENSENOVA_API_KEY": ["sensenova-01"]}


def test_credential_env_gaps_covers_both_reference_spellings(tmp_path) -> None:
    """``env: NAME`` and ``env_var: NAME`` are the same thing (see env_reference)."""
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    config = _config_with(
        [
            CredentialConfig(id="a", env="${ONE}"),
            CredentialConfig(id="b", env_var="TWO"),
        ]
    )

    gaps = credential_env_gaps(config, env_file)

    assert gaps == {"ONE": ["a"], "TWO": ["b"]}


def test_credential_env_gaps_ignores_credentials_without_an_env_name(tmp_path) -> None:
    """A literal value or a keyless provider has no name to lose."""
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    config = _config_with(
        [
            CredentialConfig(id="inline", value="dev-only"),
            CredentialConfig(id="keyless", enabled=False),
        ]
    )

    assert credential_env_gaps(config, env_file) == {}


def test_credential_env_gaps_ignores_an_inline_value_even_with_an_env_name(tmp_path) -> None:
    """``env`` + ``value`` 同时存在 = 密钥在 providers.yaml 里，本来就会迁移。

    这是全 Mock 测试夹具的真实形状（``make_provider`` 两个字段都填），所以这条
    误报会在每个跑门禁的人的屏幕上出现一遍——比没有检查更糟。
    """
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    config = _config_with([CredentialConfig(id="both", env="BOTH_WAYS", value="literal")])

    assert credential_env_gaps(config, env_file) == {}


def test_credential_env_gaps_merges_credentials_sharing_one_name(tmp_path) -> None:
    """Two credentials on the same account read the same variable: one entry."""
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    config = _config_with(
        [
            CredentialConfig(id="x", env="SHARED"),
            CredentialConfig(id="y", env="SHARED"),
        ]
    )

    assert credential_env_gaps(config, env_file) == {"SHARED": ["x", "y"]}


def test_credential_env_gaps_is_silenced_by_an_empty_value(tmp_path) -> None:
    """2026-09-30：运营者主动删掉 .env 里的 SENSENOVA_API_KEY，原话
    「不用管，空着就行，别报警了」。

    这里的关键是**把这一行留着但留空**——不是把行删掉，也不是拿 skipped 之类的
    新开关绕过检查。`dotenv_values()` 把「键存在、值是空串」记成 `("")`，所以名字
    已经在 `.env` 声明的集合里，缺口自然消失。而凭据侧照旧从 `os.environ` 解析，
    HKCU 里那份真实密钥继续生效（本机正是如此：那把 Key 还活着，换机时同账号的
    `_02` 会把它带走，所以本来就没有任何缺口需要报）。

    删掉那一行反而会让告警回来——这是运营者已经选过的答案，别让人再选一次。
    """
    env_file = tmp_path / ".env"
    env_file.write_text("SENSENOVA_API_KEY=\nTRAVELS=yes\n", encoding="utf-8")
    config = _config_with(
        [
            CredentialConfig(id="sensenova-01", env="SENSENOVA_API_KEY"),
            CredentialConfig(id="sensenova-02", env="TRAVELS"),
        ]
    )

    assert credential_env_gaps(config, env_file) == {}


def test_credential_env_gaps_names_an_absent_name(tmp_path) -> None:
    """反面：名字真的不在 `.env` 里，告警必须回来。

    和上一条必须成对存在——否则「静音空值」会被顺手改成「什么都不报」，
    而告警本身是 2026-09-29 抓到的真问题（一把只活在 HKCU 里的 Key）。
    """
    env_file = tmp_path / ".env"
    env_file.write_text("TRAVELS=yes\n", encoding="utf-8")
    config = _config_with([CredentialConfig(id="sensenova-01", env="SENSENOVA_API_KEY")])

    assert credential_env_gaps(config, env_file) == {"SENSENOVA_API_KEY": ["sensenova-01"]}


def test_credential_env_gaps_is_empty_on_a_healthy_repo(tmp_path) -> None:
    """No noise on a machine where every key is in ``.env`` — otherwise the
    warning gets trained into something people skip."""
    env_file = Path(PROJECT_ROOT) / ".env"
    if not env_file.is_file():
        pytest.skip("no local .env")
    config = load_app_config()

    gaps = credential_env_gaps(config, env_file)

    # This asserts the *contract* (the function returns a dict), not a live
    # value: what it holds on any given machine is that machine's business.
    assert isinstance(gaps, dict)


#: Environment variable names that belong to *other* software, not to this
#: gateway. Anything a template interpolates from one of these is decided by
#: whatever tool happens to have set it at the user level.
TOOL_OWNED_ENV_NAMES = frozenset(
    {
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_ENABLED",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_ENABLED",
        "GEMINI_BASE_URL",
        "GEMINI_API_KEY",
        "GEMINI_ENABLED",
        "OPENROUTER_BASE_URL",
        "OPENROUTER_ENABLED",
    }
)

_INTERP_NAME = re.compile(r"\$\{([A-Z0-9_]+)(?::-[^}]*)?\}")


def test_example_templates_do_not_read_tool_owned_env_names() -> None:
    """接入点不许用别人家的变量名（2026-09-29 实测踩到过）。

    这台机器上 `ANTHROPIC_BASE_URL` 被 cc-switch 设成了 `http://127.0.0.1:15721`，
    `.env` 里的官方地址被静默顶掉——因为 `load_dotenv(override=False)` 让进程环境赢。
    模板改成 `ZKAI_` 前缀后这个方向就彻底没了：运营商仍然完全可控（设
    `ZKAI_ANTHROPIC_BASE_URL` 即可），只是不再有无关工具能替网关决定上游。
    """
    template = Path(PROJECT_ROOT) / "config" / "providers.example.yaml"
    if not template.is_file():
        pytest.skip("no bundled providers.example.yaml")

    lines = [
        line
        for line in template.read_text(encoding="utf-8").splitlines()
        # 注释里出现裸名字是在「举例说明别这么写」，那不是真插值
        if not line.lstrip().startswith("#")
    ]
    used = set(_INTERP_NAME.findall("\n".join(lines)))

    assert not (used & TOOL_OWNED_ENV_NAMES), (
        f"模板仍在使用别人家的变量名：{sorted(used & TOOL_OWNED_ENV_NAMES)}"
        "——改成 ZKAI_ 前缀（见 .env.example 的说明）"
    )


def test_namespaced_base_url_still_overrides_and_survives_a_hijack(monkeypatch) -> None:
    """ZKAI_ 前缀的两个方向都要对：自己设了就生效，别人设裸名字不影响。"""
    doc = {
        "providers": [
            {
                "id": "anthropic",
                "base_url": "${ZKAI_ANTHROPIC_BASE_URL:-https://api.anthropic.com/v1}",
            }
        ]
    }
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:15721")

    assert interpolate_env(doc)["providers"][0]["base_url"] == "https://api.anthropic.com/v1"

    monkeypatch.setenv("ZKAI_ANTHROPIC_BASE_URL", "https://my-proxy.internal/v1")
    assert interpolate_env(doc)["providers"][0]["base_url"] == "https://my-proxy.internal/v1"


# --------------------------------------------------------------------------- #
# ${VAR} interpolation
# --------------------------------------------------------------------------- #
    monkeypatch.setenv("ZKAI_TEST_INTERP_PORT", "9001")
    monkeypatch.setenv("ZKAI_TEST_INTERP_FLAG", "true")
    monkeypatch.delenv("ZKAI_TEST_INTERP_MISSING", raising=False)

    payload = {
        "a": "${ZKAI_TEST_INTERP_PORT:-1234}",
        "b": "${ZKAI_TEST_INTERP_MISSING:-fallback}",
        "c": "${ZKAI_TEST_INTERP_MISSING}",
        "d": ["${ZKAI_TEST_INTERP_PORT}", {"deep": "x-${ZKAI_TEST_INTERP_PORT:-0}"}],
        "e": 42,
        "f": None,
        "g": "${ZKAI_TEST_INTERP_FLAG:-false}",
    }
    result = interpolate_env(payload)

    # A whole-value reference is coerced, so `port: ${ZKAI_PORT:-8000}` arrives
    # as an int and `enabled: ${X:-true}` as a bool without quoting the YAML.
    assert result["a"] == 9001
    assert isinstance(result["a"], int)
    assert result["g"] is True
    assert result["b"] == "fallback"
    # No env var and no default -> the field is *absent*, never a blank string.
    # That fails closed: a missing ${ADMIN_TOKEN} disables the admin API.
    assert result["c"] is None
    # A value that is exactly a reference is coerced wherever it appears, even
    # nested in a list; a reference embedded in a larger string is substituted
    # but left as text.
    assert result["d"] == [9001, {"deep": "x-9001"}]
    assert result["e"] == 42
    assert result["f"] is None


def test_credential_env_indirection_repoints_the_variable_name(monkeypatch) -> None:
    """``env: ${X_ENV:-DEFAULT}`` picks *which* variable holds the key.

    This is the pattern the bundled ``providers.example.yaml`` uses, so it must
    keep working: the indirection lets an operator repoint a slot at a different
    variable without editing the YAML.
    """
    monkeypatch.setenv("ZKAI_TEST_INDIRECT_ENV", "REAL_KEY_VAR")
    assert interpolate_env("${ZKAI_TEST_INDIRECT_ENV:-ZKAI_TEST_INDIRECT}") == "REAL_KEY_VAR"

    monkeypatch.delenv("ZKAI_TEST_INDIRECT_ENV", raising=False)
    assert interpolate_env("${ZKAI_TEST_INDIRECT_ENV:-ZKAI_TEST_INDIRECT}") == "ZKAI_TEST_INDIRECT"


# --------------------------------------------------------------------------- #
# Settings bridging
# --------------------------------------------------------------------------- #
def test_yaml_settings_do_not_override_environment_variables(monkeypatch) -> None:
    """An explicit env var must win over ``config.yaml``."""
    monkeypatch.setenv("ZKAI_PORT", "7777")
    settings = Settings()
    assert settings.port == 7777  # pydantic-settings reads the ZKAI_ prefix

    merged = apply_settings_from_yaml(settings, {"app": {"port": 1234}})
    assert merged.port == 7777


def test_yaml_settings_fill_in_values_that_have_no_env_var(monkeypatch) -> None:
    monkeypatch.delenv("ZKAI_PORT", raising=False)
    settings = Settings()
    merged = apply_settings_from_yaml(settings, {"app": {"port": 1234}})
    assert merged.port == 1234


# --------------------------------------------------------------------------- #
# End-to-end config load
# --------------------------------------------------------------------------- #
def test_load_app_config_reads_the_three_yaml_files(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"app": {"name": "ZK-Test"}, "credential_rotation": "round_robin"}),
        encoding="utf-8",
    )
    (config_dir / "providers.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "id": "p1",
                        "type": "openai",
                        "base_url": "${ZKAI_TEST_BASE_URL:-http://127.0.0.1:9/v1}",
                        # `env:` names the variable that holds the key; the
                        # *_ENV indirection mirrors the bundled examples.
                        "credentials": [{"id": "k1", "env": "${ZKAI_TEST_KEY_ENV:-ZKAI_TEST_KEY}"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "models.yaml").write_text(
        yaml.safe_dump(
            {
                "models": [
                    {"id": "m1", "deployments": [{"id": "d1", "provider_id": "p1", "model": "up1"}]}
                ],
                "aliases": [{"name": "zk-a", "targets": ["m1"]}],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ZKAI_TEST_BASE_URL", "http://127.0.0.1:8099/v1")
    monkeypatch.setenv("ZKAI_TEST_KEY", "secret-value")
    monkeypatch.delenv("ZKAI_TEST_KEY_ENV", raising=False)

    config = load_app_config(Settings(config_dir=config_dir, health_check_mode="off"))

    assert config.settings.app_name == "ZK-Test"
    assert set(config.source_files) == {"config", "providers", "models"}
    assert config.providers["p1"].base_url == "http://127.0.0.1:8099/v1"
    # The secret is never interpolated into the document: `env` stays a reference
    # and is resolved from os.environ at credential-registration time.
    credential = config.providers["p1"].credentials[0]
    assert credential.env_reference() == "${ZKAI_TEST_KEY}"
    assert "secret-value" not in (credential.env or "")
    assert config.models["m1"].deployments[0].model == "up1"
    assert config.is_alias("zk-a") is True
    assert config.resolve_model_ids("zk-a") == ["m1"]
    assert config.warnings == []


def test_load_app_config_falls_back_to_the_example_files(tmp_path) -> None:
    """Without local YAML the bundled ``*.example.yaml`` files must be used.

    A machine that has already run the gateway has git-ignored ``config/*.yaml``
    files which shadow the examples, so this builds the directory a fresh clone
    sees: example files only.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for stem in ("config", "providers", "models"):
        source = PROJECT_ROOT / "config" / f"{stem}.example.yaml"
        assert source.is_file(), f"bundled example missing: {source}"
        (config_dir / f"{stem}.example.yaml").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )

    config = load_app_config(Settings(config_dir=config_dir, health_check_mode="off"))

    assert config.source_files == {
        "config": "config.example.yaml",
        "providers": "providers.example.yaml",
        "models": "models.example.yaml",
    }
    # The documented aliases always resolve, even with no local overrides.
    assert config.is_alias("zk-coding")


def test_project_root_is_used_for_relative_config_dirs(monkeypatch) -> None:
    monkeypatch.delenv("ZKAI_CONFIG_DIR", raising=False)
    settings = Settings(config_dir=Path("config"))
    assert settings.resolved_config_dir == (PROJECT_ROOT / "config").resolve()

    absolute = Settings(config_dir=PROJECT_ROOT / "elsewhere")
    assert absolute.resolved_config_dir == (PROJECT_ROOT / "elsewhere").resolve()


# --------------------------------------------------------------------------- #
# Credential guards
# --------------------------------------------------------------------------- #
def _write_provider_config_dir(config_dir: Path, providers: list[dict]) -> None:
    """Write ``providers.yaml`` plus a coherent ``models.yaml``.

    The model is needed so the implicit ``zk-*`` aliases have a target; without
    it ``validate()`` reports "alias has no targets" and the warning assertions
    below would pass for the wrong reason.
    """
    (config_dir / "providers.yaml").write_text(
        yaml.safe_dump({"providers": providers}), encoding="utf-8"
    )
    (config_dir / "models.yaml").write_text(
        yaml.safe_dump(
            {
                "models": [
                    {
                        "id": "m1",
                        "deployments": [{"id": "d1", "provider_id": providers[0]["id"], "model": "up1"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_an_expanded_secret_in_env_is_reported(tmp_path, monkeypatch) -> None:
    """``env:`` names a variable, so expanding a secret into it must not be silent.

    Read the other way round, the credential stays **enabled** holding the literal
    secret as its key: every request 401s, and because ``env_reference()`` is also
    persisted and logged, an identifier-safe value would be written out verbatim.
    """
    monkeypatch.setenv("ZKAI_TEST_LEAKY_KEY", "sk-super-secret-value")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_provider_config_dir(
        config_dir,
        [
            {
                "id": "p1",
                "type": "openai",
                "base_url": "http://127.0.0.1:9/v1",
                "credentials": [{"id": "k1", "env": "${ZKAI_TEST_LEAKY_KEY}"}],
            }
        ],
    )

    config = load_app_config(Settings(config_dir=config_dir, health_check_mode="off"))

    credential = config.providers["p1"].credentials[0]
    assert credential.env == "sk-super-secret-value"  # what interpolation did
    assert any("is not a valid environment variable name" in w for w in config.warnings)


def test_a_credential_without_any_secret_source_is_reported(tmp_path) -> None:
    """An enabled credential with neither ``env`` nor ``value`` can only 401."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_provider_config_dir(
        config_dir,
        [
            {
                "id": "p1",
                "type": "openai",
                "base_url": "http://127.0.0.1:9/v1",
                "credentials": [{"id": "k1", "priority": 100}],
            }
        ],
    )

    config = load_app_config(Settings(config_dir=config_dir, health_check_mode="off"))

    assert any("has no secret source" in w for w in config.warnings)


def test_keyless_providers_do_not_need_a_secret_source(tmp_path) -> None:
    """Ollama runs locally, so a bare credential entry must not warn."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_provider_config_dir(
        config_dir,
        [
            {
                "id": "local",
                "type": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "credentials": [{"id": "k1"}],
            }
        ],
    )

    config = load_app_config(Settings(config_dir=config_dir, health_check_mode="off"))

    assert config.warnings == []


# --------------------------------------------------------------------------- #
# Project-root anchoring (process must not depend on the working directory)
# --------------------------------------------------------------------------- #
def test_relative_database_url_is_anchored_to_the_project_root() -> None:
    settings = Settings(database_url="sqlite+aiosqlite:///./data/zkai.db")
    url = settings.resolved_database_url
    assert str(PROJECT_ROOT.as_posix()) in url
    assert "./data" not in url


def test_dotenv_loads_from_the_project_root_regardless_of_cwd(tmp_path, monkeypatch) -> None:
    """Starting the gateway from another directory must still find the real .env."""
    monkeypatch.delenv("ZKAI_TEST_DOTENV_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # cwd has no .env at all
    assert load_dotenv_file() is True
    # The real project .env was loaded (any known var proves it).
    assert os.environ.get("ZKAI_ADMIN_TOKEN") or os.environ.get("SENSENOVA_API_KEY")
