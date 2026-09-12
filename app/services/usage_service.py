"""Usage / cost accounting service."""

from __future__ import annotations

from typing import Any

from app.core.config import AppConfig
from app.core.logging import credential_var, get_logger, provider_var
from app.database.repository import UsageRepository
from app.models.response import Usage

logger = get_logger("services.usage")


class UsageService:
    """Turn provider usage reports into persistent statistics."""

    def __init__(self, config: AppConfig, repository: UsageRepository | None = None) -> None:
        self.config = config
        self.repository = repository

    # ------------------------------------------------------------------ #
    def estimate_cost(
        self, *, model_id: str, deployment_id: str | None, usage: Usage
    ) -> float:
        """Estimate USD cost from the deployment's per-million-token prices."""
        model = self.config.models.get(model_id)
        if model is None:
            return 0.0
        deployment = None
        for candidate in model.deployments:
            if deployment_id is None or candidate.id == deployment_id:
                deployment = candidate
                break
        if deployment is None:
            return 0.0
        cost = (
            usage.prompt_tokens * deployment.input_cost_per_mtok
            + usage.completion_tokens * deployment.output_cost_per_mtok
        ) / 1_000_000
        return round(cost, 8)

    async def record(
        self,
        *,
        request_id: str,
        provider_id: str,
        model_id: str,
        deployment_id: str | None,
        credential_id: str | None,
        usage: Usage,
        latency_ms: float = 0.0,
    ) -> float:
        """Persist a usage row; returns the estimated cost."""
        cost = self.estimate_cost(
            model_id=model_id, deployment_id=deployment_id, usage=usage
        )
        if self.repository is None:
            return cost
        provider_var.set(provider_id)
        credential_var.set(credential_id or "-")
        try:
            await self.repository.record(
                request_id=request_id,
                provider_id=provider_id,
                model=model_id,
                credential_id=credential_id,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
            )
        except Exception:
            logger.exception("failed to persist usage record for %s", request_id)
        return cost

    async def summary(self, *, days: int = 7) -> dict[str, Any]:
        if self.repository is None:
            return {"window_days": days, "requests": 0, "total_tokens": 0, "cost_usd": 0.0}
        return await self.repository.summary(days=days)

    async def daily(self, *, days: int = 14) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        return await self.repository.daily(days=days)

    async def backfill_zero_cost(self) -> dict[str, Any]:
        """Reprice usage rows whose ``cost_usd`` is 0/NULL using today's prices.

        Cost is computed at record time, so rows written before the list prices
        existed sit at zero and skew the dashboard. Same idempotency contract as
        ``scripts/backfill_cost.py``: only zero rows are touched.
        """
        if self.repository is None:
            return {"updated": 0, "total_cost_usd": 0.0}
        prices: dict[tuple[str, str], tuple[float, float]] = {}
        fallback: dict[str, tuple[float, float]] = {}
        for model in self.config.models.values():
            for deployment in model.deployments:
                pair = (deployment.input_cost_per_mtok, deployment.output_cost_per_mtok)
                prices[(model.id, deployment.provider_id)] = pair
                if model.id not in fallback and (pair[0] or pair[1]):
                    fallback[model.id] = pair
        return await self.repository.backfill_costs(prices, fallback)
