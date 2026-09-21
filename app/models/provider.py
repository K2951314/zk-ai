"""Configuration models: providers, credentials, deployments, models, aliases."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Capability dimensions every model is scored on (0-10).
CAPABILITY_NAMES: tuple[str, ...] = (
    "coding",
    "reasoning",
    "tool_use",
    "vision",
    "long_context",
    "structured_output",
    "speed",
    "cost",
)


class CapabilityScores(BaseModel):
    """0-10 scores. ``cost`` is inverted: 10 means cheapest."""

    model_config = ConfigDict(extra="forbid")

    coding: float = 5.0
    reasoning: float = 5.0
    tool_use: float = 5.0
    vision: float = 5.0
    long_context: float = 5.0
    structured_output: float = 5.0
    speed: float = 5.0
    cost: float = 5.0

    @field_validator("*")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return float(min(10.0, max(0.0, value)))

    def as_dict(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in CAPABILITY_NAMES}


class ProviderType(str, Enum):
    """Supported upstream protocol families."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


class CredentialConfig(BaseModel):
    """Static credential definition. The secret itself lives in the env var."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider_id: str | None = None
    #: ``${ENV_VAR}`` reference; the only recommended way to supply a key.
    env: str | None = None
    #: Explicit env var name (equivalent to ``env: ${NAME}``).
    env_var: str | None = None
    #: Inline value, development only. Never commit a real key.
    value: str | None = None
    enabled: bool = True
    priority: int = 100
    weight: float = 1.0
    tags: list[str] = Field(default_factory=list)
    #: Consecutive failures before the credential is marked UNHEALTHY.
    max_consecutive_failures: int = 3

    def env_reference(self) -> str | None:
        """Return the ``${VAR}`` style reference this credential resolves from."""
        if self.env:
            return self.env if self.env.startswith("${") else "${" + self.env + "}"
        if self.env_var:
            return "${" + self.env_var + "}"
        return None


class ProviderConfig(BaseModel):
    """An upstream provider (one endpoint, many credentials)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: ProviderType
    base_url: str
    enabled: bool = True
    timeout: float = 60.0
    connect_timeout: float = 10.0
    #: Provider-wide retry ceiling for a single credential.
    max_retries: int = 2
    api_version: str | None = None
    #: Extra static headers (never put a key here; use credentials).
    headers: dict[str, str] = Field(default_factory=dict)
    referer: str | None = None
    app_title: str | None = None
    #: Provider specific switches, e.g. ``{"max_tokens_param": "max_completion_tokens"}``.
    options: dict[str, Any] = Field(default_factory=dict)
    credentials: list[CredentialConfig] = Field(default_factory=list)
    health_check_enabled: bool = True

    @model_validator(mode="after")
    def _stamp_provider_id(self) -> ProviderConfig:
        for credential in self.credentials:
            credential.provider_id = self.id
        return self

    @property
    def requires_credential(self) -> bool:
        """Ollama and local endpoints run without an API key."""
        return self.type is not ProviderType.OLLAMA


class DeploymentConfig(BaseModel):
    """A concrete (provider, upstream model) pair - the unit of failover."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider_id: str
    #: Provider-native model name sent upstream, e.g. ``gpt-5-mini``.
    model: str
    enabled: bool = True
    priority: int = 100
    weight: float = 1.0
    context_window: int = 128_000
    max_output_tokens: int | None = None
    #: Overrides merged on top of the model's capability scores.
    capabilities: dict[str, float] = Field(default_factory=dict)
    #: USD per 1M tokens, informational (used for cost statistics).
    input_cost_per_mtok: float = 0.0
    output_cost_per_mtok: float = 0.0
    #: Request parameters this deployment accepts; anything else is reported.
    supported_params: list[str] | None = None
    #: Free-form provider options merged into the outbound payload.
    options: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @property
    def supports_vision(self) -> bool:
        return self.capabilities.get("vision", 0) > 0


class ModelConfig(BaseModel):
    """A public model exposed by the gateway."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str | None = None
    enabled: bool = True
    context_window: int = 128_000
    capabilities: CapabilityScores = Field(default_factory=CapabilityScores)
    deployments: list[DeploymentConfig] = Field(default_factory=list)
    description: str | None = None
    owned_by: str | None = None

    def effective_capabilities(self, deployment: DeploymentConfig) -> CapabilityScores:
        """Model scores with per-deployment overrides applied."""
        base = self.capabilities.as_dict()
        base.update({k: v for k, v in deployment.capabilities.items() if k in CAPABILITY_NAMES})
        return CapabilityScores(**base)


class AliasStrategy(str, Enum):
    """How an alias orders its candidate models."""

    PRIORITY = "priority"
    CAPABILITY = "capability"
    COST = "cost"
    SPEED = "speed"
    ROUND_ROBIN = "round_robin"
    WEIGHTED = "weighted"


class ModelAliasConfig(BaseModel):
    """``zk-coding`` -> an ordered list of concrete models."""

    model_config = ConfigDict(extra="forbid")

    name: str
    targets: list[str] = Field(default_factory=list)
    strategy: AliasStrategy = AliasStrategy.PRIORITY
    description: str | None = None
    enabled: bool = True
    #: Hard capability gates, e.g. ``{"vision": 1}`` means "must support images".
    requires: dict[str, float] = Field(default_factory=dict)
    #: Relative importance when ``strategy=capability``; defaults to weights inferred
    #: from the request.
    weights: dict[str, float] = Field(default_factory=dict)
    fallback_to_local: bool = False
    #: When true, `targets[0]` is a hard pin that always leads the attempt order,
    #: even under `strategy=capability`. Set by the console's model hot-swap so an
    #: explicit operator choice overrides the capability ranking.
    pin_first: bool = False

    @field_validator("targets")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for item in value:
            seen.setdefault(item, None)
        return list(seen)


__all__ = [
    "CAPABILITY_NAMES",
    "AliasStrategy",
    "CapabilityScores",
    "CredentialConfig",
    "DeploymentConfig",
    "ModelAliasConfig",
    "ModelConfig",
    "ProviderConfig",
    "ProviderType",
]
