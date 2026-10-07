"""AppConfig: the fully merged runtime configuration object.

Holds providers / models / aliases / retry policy / raw YAML and provides
cross-reference validation plus lookups used by the router and console.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config.settings import _ENV_VAR_NAME, Settings
from app.models.provider import (
    ModelAliasConfig,
    ModelConfig,
    ProviderConfig,
)
from app.retry.policy import RetryPolicy


@dataclass
class AppConfig:
    """Fully merged runtime configuration."""

    settings: Settings
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    models: dict[str, ModelConfig] = field(default_factory=dict)
    aliases: dict[str, ModelAliasConfig] = field(default_factory=dict)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    raw: dict[str, Any] = field(default_factory=dict)
    source_files: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        return self.providers.get(provider_id)

    def get_model(self, model_id: str) -> ModelConfig | None:
        return self.models.get(model_id)

    def get_alias(self, name: str) -> ModelAliasConfig | None:
        return self.aliases.get(name)

    def is_alias(self, name: str) -> bool:
        return name in self.aliases

    def resolve_model_ids(self, name: str) -> list[str]:
        """Expand an alias (or a plain model id) into concrete model ids."""
        alias = self.aliases.get(name)
        if alias is None:
            return [name] if name in self.models else []
        targets: list[str] = []
        for target in alias.targets:
            nested = self.aliases.get(target)
            if nested is not None:
                targets.extend(self.resolve_model_ids(target))
            elif target in self.models:
                targets.append(target)
        # de-duplicate, keep order
        seen: dict[str, None] = {}
        for target in targets:
            seen.setdefault(target, None)
        return list(seen)

    def deployments_for_model(self, model_id: str) -> list[Any]:
        model = self.models.get(model_id)
        return list(model.deployments) if model else []

    def enabled_models(self) -> list[ModelConfig]:
        return [m for m in self.models.values() if m.enabled]

    def public_model_ids(self) -> list[str]:
        return sorted(self.models.keys())

    def enabled_aliases(self) -> list[ModelAliasConfig]:
        return [a for a in self.aliases.values() if a.enabled]

    def credentials_for_provider(self, provider_id: str) -> list[Any]:
        provider = self.providers.get(provider_id)
        return list(provider.credentials) if provider else []

    def describe(self) -> dict[str, Any]:
        """Summary used by ``/admin/health`` and startup logging."""
        return {
            "environment": self.settings.environment,
            "providers": {
                pid: {
                    "type": provider.type.value,
                    "enabled": provider.enabled,
                    "credentials": len(provider.credentials),
                }
                for pid, provider in self.providers.items()
            },
            "models": len(self.models),
            "aliases": sorted(self.aliases.keys()),
            "source_files": self.source_files,
            "warnings": self.warnings,
        }

    def validate(self) -> list[str]:
        """Cross-reference checks; returns a list of human readable problems."""
        problems: list[str] = []
        for provider in self.providers.values():
            problems.extend(self._validate_credentials(provider))
        for model in self.models.values():
            if not model.deployments:
                problems.append(f"model '{model.id}' has no deployments")
            for deployment in model.deployments:
                if deployment.provider_id not in self.providers:
                    problems.append(
                        f"model '{model.id}' deployment '{deployment.id}' references "
                        f"unknown provider '{deployment.provider_id}'"
                    )
        for alias in self.aliases.values():
            if not alias.targets:
                problems.append(f"alias '{alias.name}' has no targets")
            for target in alias.targets:
                if target not in self.models and target not in self.aliases:
                    problems.append(f"alias '{alias.name}' target '{target}' is unknown")
        return problems

    @staticmethod
    def _validate_credentials(provider: ProviderConfig) -> list[str]:
        """Catch the two ways a credential ends up with no usable secret.

        ``env``/``env_var`` hold the *name* of an environment variable, so a
        document such as ``env: ${OPENAI_KEY}`` is expanded by
        :func:`interpolate_env` into the secret **value** - which then names a
        variable that does not exist. The failure is nasty because it is not
        reported as a missing key: the credential stays enabled holding the
        literal string as its secret (every request 401s), and if the expanded
        value happens to be identifier-safe the pool logs it verbatim.
        """
        problems: list[str] = []
        for credential in provider.credentials:
            name = credential.env or credential.env_var
            if name is None:
                if credential.enabled and credential.value is None and provider.requires_credential:
                    problems.append(
                        f"credential '{credential.id}' has no secret source: "
                        "set env/env_var to an environment variable name, or value for development"
                    )
                continue
            if not _ENV_VAR_NAME.match(name):
                problems.append(
                    f"credential '{credential.id}' env '{name}' is not a valid environment "
                    "variable name - put the variable NAME there, e.g. env: MY_KEY, or "
                    "env: ${MY_KEY_ENV:-MY_KEY} to pick the name through indirection"
                )
        return problems

    def known_names(self) -> list[str]:
        return sorted({*self.models.keys(), *self.aliases.keys()})
