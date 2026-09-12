"""Model / alias catalogue service (read-mostly, no I/O against providers)."""

from __future__ import annotations

import time
from typing import Any

from app.core.config import AppConfig
from app.models.provider import ModelConfig
from app.routing.router import Router


class ModelService:
    """Serve ``GET /v1/models`` and the admin model views."""

    def __init__(self, config: AppConfig, router: Router) -> None:
        self.config = config
        self.router = router

    # ------------------------------------------------------------------ #
    def list_openai_models(self) -> dict[str, Any]:
        """OpenAI-compatible ``/v1/models`` payload (models *and* aliases)."""
        created = int(time.time())
        data: list[dict[str, Any]] = []
        for model in self.config.enabled_models():
            providers = sorted({d.provider_id for d in model.deployments})
            data.append(
                {
                    "id": model.id,
                    "object": "model",
                    "created": created,
                    "owned_by": model.owned_by or (providers[0] if providers else "zk-ai"),
                    "permission": [],
                    "root": model.id,
                    "parent": None,
                    # ZK-AI extensions (ignored by OpenAI SDKs):
                    "zk_ai": {
                        "kind": "model",
                        "display_name": model.display_name or model.id,
                        "context_window": model.context_window,
                        "capabilities": model.capabilities.as_dict(),
                        "providers": providers,
                        "deployments": [d.id for d in model.deployments],
                    },
                }
            )
        for alias in self.config.enabled_aliases():
            model_ids = self.config.resolve_model_ids(alias.name)
            data.append(
                {
                    "id": alias.name,
                    "object": "model",
                    "created": created,
                    "owned_by": "zk-ai:alias",
                    "permission": [],
                    "root": alias.name,
                    "parent": None,
                    "zk_ai": {
                        "kind": "alias",
                        "strategy": alias.strategy.value,
                        "targets": list(alias.targets),
                        "resolved_models": model_ids,
                        "description": alias.description,
                    },
                }
            )
        data.sort(key=lambda item: (item["zk_ai"]["kind"], item["id"]))
        return {"object": "list", "data": data}

    # ------------------------------------------------------------------ #
    def get(self, model_id: str) -> ModelConfig | None:
        return self.config.models.get(model_id)

    def describe(self, model_id: str) -> dict[str, Any] | None:
        model = self.config.models.get(model_id)
        if model is None:
            return None
        return {
            "id": model.id,
            "display_name": model.display_name,
            "enabled": model.enabled,
            "owned_by": model.owned_by,
            "description": model.description,
            "context_window": model.context_window,
            "capabilities": model.capabilities.as_dict(),
            "deployments": [
                {
                    "id": deployment.id,
                    "provider_id": deployment.provider_id,
                    "upstream_model": deployment.model,
                    "enabled": deployment.enabled,
                    "priority": deployment.priority,
                    "weight": deployment.weight,
                    "context_window": deployment.context_window,
                    "max_output_tokens": deployment.max_output_tokens,
                    "capabilities": deployment.capabilities,
                    "input_cost_per_mtok": deployment.input_cost_per_mtok,
                    "output_cost_per_mtok": deployment.output_cost_per_mtok,
                    "supported_params": deployment.supported_params,
                    "tags": deployment.tags,
                }
                for deployment in model.deployments
            ],
        }

    def alias_table(self) -> dict[str, dict]:
        return self.router.aliases.describe()

    def summary(self) -> dict[str, Any]:
        return {
            "models": len(self.config.models),
            "aliases": len(self.config.aliases),
            "providers": len(self.config.providers),
            "model_ids": self.config.public_model_ids(),
            "alias_ids": sorted(self.config.aliases),
        }
