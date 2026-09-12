"""OpenAI provider (``https://api.openai.com/v1``).

The OpenAI wire format *is* the canonical format, so this adapter only pins the
provider identity and documents the token-parameter switch (``max_tokens`` vs
``max_completion_tokens`` for the reasoning models).

API reference: https://platform.openai.com/docs/api-reference/chat
"""

from __future__ import annotations

from app.models.provider import DeploymentConfig, ProviderType
from app.providers.base import OpenAICompatibleAdapter


class OpenAIAdapter(OpenAICompatibleAdapter):
    """Adapter for the OpenAI Chat Completions API."""

    provider_type = ProviderType.OPENAI
    openai_compatible = True

    def token_param_name(self, deployment: DeploymentConfig) -> str:
        """Reasoning models reject ``max_tokens``; deployments can opt into the new name."""
        return str(deployment.options.get("max_tokens_param", "max_tokens"))
