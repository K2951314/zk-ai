"""``GET /v1/models`` - OpenAI-compatible model catalogue."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import ContainerDep

router = APIRouter(tags=["models"])


@router.get("/v1/models", summary="List models and aliases")
async def list_models(container: ContainerDep) -> dict:
    """Every configured model plus every alias, in OpenAI's list envelope."""
    return container.model_service.list_openai_models()


@router.get("/v1/models/{model_id:path}", summary="Retrieve one model")
async def retrieve_model(model_id: str, container: ContainerDep) -> dict:
    """Return a single model (or alias) descriptor."""
    if container.config.is_alias(model_id):
        alias = container.config.aliases[model_id]
        return {
            "id": alias.name,
            "object": "model",
            "owned_by": "zk-ai:alias",
            "zk_ai": {
                "kind": "alias",
                "strategy": alias.strategy.value,
                "targets": list(alias.targets),
                "resolved_models": container.config.resolve_model_ids(alias.name),
                "description": alias.description,
            },
        }
    described = container.model_service.describe(model_id)
    if described is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "message": f"model '{model_id}' not found",
                    "type": "model_not_found",
                    "code": 404,
                }
            },
        )
    described.update({"object": "model", "zk_ai": {"kind": "model"}})
    return described
