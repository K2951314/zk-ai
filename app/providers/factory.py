"""Adapter factory: provider type -> concrete :class:`ProviderAdapter`."""

from __future__ import annotations

from collections.abc import Callable

from app.core.errors import ConfigError
from app.models.provider import ProviderConfig, ProviderType
from app.providers.anthropic import AnthropicAdapter
from app.providers.base import ProviderAdapter
from app.providers.gemini import GeminiAdapter
from app.providers.ollama import OllamaAdapter
from app.providers.openai import OpenAIAdapter
from app.providers.openrouter import OpenRouterAdapter

_REGISTRY: dict[ProviderType, Callable[[ProviderConfig], ProviderAdapter]] = {
    ProviderType.OPENAI: OpenAIAdapter,
    ProviderType.OPENAI_COMPATIBLE: OpenAIAdapter,
    ProviderType.ANTHROPIC: AnthropicAdapter,
    ProviderType.GEMINI: GeminiAdapter,
    ProviderType.OPENROUTER: OpenRouterAdapter,
    ProviderType.OLLAMA: OllamaAdapter,
}


def register_adapter(
    provider_type: ProviderType | str,
    factory: Callable[[ProviderConfig], ProviderAdapter],
) -> None:
    """Register (or override) an adapter factory - used by tests and plugins."""
    if not isinstance(provider_type, ProviderType):
        try:
            provider_type = ProviderType(provider_type)
        except ValueError as exc:
            raise ConfigError(
                f"unknown provider type '{provider_type}'; "
                f"supported: {', '.join(t.value for t in ProviderType)}"
            ) from exc
    _REGISTRY[provider_type] = factory


def create_adapter(config: ProviderConfig) -> ProviderAdapter:
    """Instantiate the adapter for *config*."""
    factory = _REGISTRY.get(config.type)
    if factory is None:  # pragma: no cover - guarded by the enum
        raise ConfigError(f"unsupported provider type '{config.type}'")
    return factory(config)


def supported_provider_types() -> list[str]:
    return [item.value for item in _REGISTRY]
