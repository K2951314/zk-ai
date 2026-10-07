"""Configuration loading: ``.env`` -> :class:`Settings`, YAML -> :class:`AppConfig`.

Resolution order for the three YAML files (first existing wins)::

    config/config.yaml   ||  config/config.example.yaml
    config/providers.yaml||  config/providers.example.yaml
    config/models.yaml   ||  config/models.example.yaml

Values may reference environment variables with ``${VAR}`` or ``${VAR:-default}``
anywhere in the YAML, which keeps real API keys out of the repository.

The package is split into four sub-modules:

* :mod:`.settings`   — :class:`Settings`, ``PROJECT_ROOT``, interpolation helpers;
* :mod:`.appconfig`   — :class:`AppConfig` and its cross-reference validation;
* :mod:`.env_health`  — ``.env`` loading + shadowed/malformed-name diagnostics;
* :mod:`.loader`      — YAML parsing, Settings<->config.yaml bridging, the cache.

This ``__init__`` re-exports everything that used to live in the single
``config.py`` so existing callers — ``from app.core.config import AppConfig`` —
keep working unchanged.
"""

from __future__ import annotations

from app.core.config.appconfig import AppConfig
from app.core.config.env_health import (
    credential_env_gaps,
    env_file_names,
    load_dotenv_file,
    malformed_env_names,
    shadowed_env_names,
)
from app.core.config.loader import (
    _SETTINGS_SECTIONS,
    apply_db_overrides,
    apply_settings_from_yaml,
    get_app_config,
    load_app_config,
    reset_config_cache,
)
from app.core.config.settings import (
    PROJECT_ROOT,
    Settings,
    deep_merge,
    interpolate_env,
    logger,
)

__all__ = [
    "PROJECT_ROOT",
    "_SETTINGS_SECTIONS",
    "AppConfig",
    "Settings",
    "apply_db_overrides",
    "apply_settings_from_yaml",
    "credential_env_gaps",
    "deep_merge",
    "env_file_names",
    "get_app_config",
    "interpolate_env",
    "load_app_config",
    "load_dotenv_file",
    "logger",
    "malformed_env_names",
    "reset_config_cache",
    "shadowed_env_names",
]
