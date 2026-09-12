"""OpenRouter provider (OpenAI-compatible with attribution headers).

Reference: https://openrouter.ai/docs/api-reference/overview
Base URL: ``https://openrouter.ai/api/v1``
Extra headers: ``HTTP-Referer`` and ``X-Title`` (``X-OpenRouter-Title``) for app
attribution. OpenRouter also accepts ``models: [...]`` for its own fallback, but
ZK-AI does not delegate failover - the router owns that decision.
"""

from __future__ import annotations

from typing import Any

from app.models.credential import CredentialRuntime
from app.models.provider import DeploymentConfig, ProviderType
from app.models.request import ChatCompletionRequest
from app.providers.base import OpenAICompatibleAdapter

#: Parameters OpenRouter documents as unsupported for non-OpenAI models
#: (they are silently ignored upstream, so we surface them in the logs).
OPENROUTER_IGNORED = frozenset({"logit_bias", "logprobs", "top_logprobs"})


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """Adapter for OpenRouter (multi-vendor aggregation)."""

    provider_type = ProviderType.OPENROUTER
    openai_compatible = True

    def _base_headers(self) -> dict[str, str]:
        headers = super()._base_headers()
        referer = self.config.referer or self.config.options.get("referer")
        title = self.config.app_title or self.config.options.get("app_title")
        if referer:
            headers["HTTP-Referer"] = str(referer)
        if title:
            headers["X-Title"] = str(title)
        return headers

    def _auth_headers(self, credential: CredentialRuntime | None) -> dict[str, str]:
        if credential and credential.secret:
            return {"authorization": f"Bearer {credential.secret}"}
        return {}

    def build_payload(
        self, request: ChatCompletionRequest, deployment: DeploymentConfig
    ) -> tuple[dict[str, Any], list[str]]:
        payload, unsupported = super().build_payload(request, deployment)
        # Surface OpenRouter-only advanced parameters when explicitly passed through.
        for key in ("top_k", "min_p", "route", "models", "provider", "plugins"):
            if key in (request.model_extra or {}):
                payload[key] = request.model_extra[key]  # type: ignore[index]
        if request.top_k is not None:
            payload["top_k"] = request.top_k
            if "top_k" not in self.allowed_params(deployment):
                unsupported.append("top_k")
        # Never let the gateway delegate failover to OpenRouter — but tell the
        # operator the parameter was dropped instead of silently swallowing it.
        for dropped in ("models", "route"):
            if payload.pop(dropped, None) is not None:
                unsupported.append(f"{dropped}(gateway-owned)")
        return payload, sorted(set(unsupported))
