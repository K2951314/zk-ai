""".env file health checks: load, shadowed-name, malformed-value, gap detection.

These run at startup (``load_dotenv_file``) and are also called by the health
endpoint and the transfer/import tooling. Kept separate from the YAML loader
so the loader does not need to import dotenv internals.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from app.core.config.appconfig import AppConfig
from app.core.config.settings import PROJECT_ROOT, logger


def load_dotenv_file(path: Path | str | None = None) -> bool:
    """Load a ``.env`` file into the process environment.

    ``pydantic-settings`` reads ``.env`` into the :class:`Settings` object, but
    provider *credentials* are resolved straight from ``os.environ`` by
    :func:`app.core.security.resolve_env_reference` - so without this call a key
    placed in ``.env`` would silently never be found.

    Real environment variables always win (``override=False``), and a missing
    file is not an error. Returns ``True`` when a file was actually loaded.

    Because "env wins" is a silent precedence rule, a stale variable exported at
    the OS level (e.g. a Windows user variable set long ago) shadows the .env
    value forever - two credentials then resolve to the same key and the operator
    has no idea. That happened in practice, so every shadowed name that also has
    a *different* value in .env is logged as a warning at startup and surfaced in
    ``AppConfig.warnings``.
    """
    if path is not None:
        # Explicit path: respect it exactly (tests pass fixtures here).
        target = Path(path)
        if not target.is_file():
            return False
    else:
        target = PROJECT_ROOT / ".env"
        if not target.is_file():
            # Unusual layout (e.g. Docker mounts): fall back to the CWD.
            target = Path(".env")
            if not target.is_file():
                return False
    malformed = malformed_env_names(target)
    if malformed:
        # Reported first and separately: unlike a genuine shadow, the fix is to
        # repair the file, and the symptom shows up far away (a bind failure
        # logged as a DNS error), so the operator needs the pointer.
        logger.error(
            "%d variable(s) in %s carry stray whitespace/control characters in "
            "their value (e.g. a line ending in \\r\\r): %s - processes reading "
            "them straight from the environment get a value like '0.0.0.0\\r', "
            "which fails deep in the stack (socket bind reports it as "
            "'getaddrinfo failed'). Repair the file: rewrite those lines with "
            "plain CRLF endings",
            len(malformed), target, ", ".join(sorted(malformed)[:20]),
        )
    shadowed = shadowed_env_names(target)
    if shadowed:
        logger.warning(
            "%d variable(s) in %s are shadowed by pre-existing process "
            "environment variables (env wins; the .env values are ignored): %s "
            "- if these are stale, unset them in the OS environment",
            len(shadowed), target, ", ".join(sorted(shadowed)[:20]),
        )
    loaded = load_dotenv(target, override=False)
    if loaded:
        logger.info("loaded environment variables from %s", target)
    return bool(loaded)


def env_file_names(path: Path | str = ".env") -> frozenset[str]:
    """Names declared in ``.env`` — values are deliberately not returned.

    The transfer package (一键换机 Skill 引擎) carries ``.env`` and nothing
    else, so "is this name in ``.env``" is the same question as "does this key
    survive a machine move". Reporting names only keeps callers from having to
    handle secrets.
    """
    target = Path(path)
    if not target.is_file():
        return frozenset()
    try:
        return frozenset(dotenv_values(target))
    except Exception:  # pragma: no cover - malformed .env
        return frozenset()


def credential_env_gaps(
    config: AppConfig, path: Path | str = ".env"
) -> dict[str, list[str]]:
    """{环境变量名: 读它的凭据 id} 名单里**不在** ``.env`` 中的那些。

    Third check on the same file, after :func:`shadowed_env_names` and
    :func:`malformed_env_names`. Those two ask "is the file's value being
    honoured"; this one asks the question that only matters at the worst
    possible moment — **would this key survive a machine move?** A credential
    resolves its name from the *process* environment, so a variable that only
    exists at the user/machine level works fine today and is invisible to the
    package that carries ``.env``. Found for real on 2026-09-29:
    ``SENSENOVA_API_KEY`` (``sensenova-01``, priority 100) lives only in HKCU.

    Not an error by itself: a variable may legitimately come from somewhere
    else (a secret manager, a container). It is a **warning** because the
    operator has to be the one who decides that, and right now nobody could
    even see it.
    """
    declared = env_file_names(path)
    gaps: dict[str, list[str]] = {}
    for provider in config.providers.values():
        for credential in provider.credentials:
            reference = credential.env_reference()
            if not reference:
                continue  # inline value or keyless - no env name to lose
            if credential.value:
                # Inline secret: it lives in providers.yaml, which *does* travel
                # with the package. Flagging it would be a false alarm that
                # trains the operator to ignore the real ones.
                continue
            name = reference.removeprefix("${").removesuffix("}")
            if name and name not in declared:
                gaps.setdefault(name, [])
                if credential.id not in gaps[name]:
                    gaps[name].append(credential.id)
    return gaps


def shadowed_env_names(path: Path | str = ".env") -> list[str]:
    """``.env`` names whose file value is being ignored (process env already has
    a *different* value - with ``override=False`` the process value silently wins).

    A value that differs from the file only by *surrounding whitespace or control
    characters* is not a real conflict - it is a corrupted ``.env``. Those lines
    end up here because ``dotenv_values()`` strips them from the file value while
    ``os.environ`` keeps them, and the fallout is severe: a line written as
    ``ZKAI_HOST=0.0.0.0\\r\\r`` puts ``"0.0.0.0\\r"`` into the process (and, via
    the tray, into uvicorn's argv), where ``socket.bind()`` resolves the host
    through ``getaddrinfo`` and dies with ``[Errno 11001] getaddrinfo failed`` -
    a message that reads as a DNS outage. So the two cases are reported
    separately: :func:`malformed_env_names` names the fixable ones.
    """
    target = Path(path)
    if not target.is_file():
        return []
    try:
        file_values = dotenv_values(target)
    except Exception:  # pragma: no cover - malformed .env
        return []
    return [
        name
        for name, value in file_values.items()
        if value and name in os.environ and os.environ[name] != value
        and name not in malformed_env_names(target)
    ]


def malformed_env_names(path: Path | str = ".env") -> list[str]:
    """``.env`` names whose *process* value differs only by stripped characters.

    These are not shadowing conflicts to resolve by unsetting an OS variable -
    the file's own bytes carry the junk, so the fix is to repair the file.
    """
    target = Path(path)
    if not target.is_file():
        return []
    try:
        file_values = dotenv_values(target)
    except Exception:  # pragma: no cover - malformed .env
        return []
    names: list[str] = []
    for name, value in file_values.items():
        current = os.environ.get(name)
        if current and value and current != value and current.strip() == value:
            names.append(name)
    return names
