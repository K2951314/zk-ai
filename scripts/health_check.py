"""Standalone health check for ZK-AI providers.

Usage::

    uv run python scripts/health_check.py                  # all enabled providers
    uv run python scripts/health_check.py -p openai        # one provider
    uv run python scripts/health_check.py --json           # machine readable
    uv run python scripts/health_check.py --remote http://127.0.0.1:8000

Local mode builds the object graph directly (no HTTP server needed) and probes
``GET /models`` on each provider - it never consumes inference quota.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings, load_app_config
from app.credentials.pool import CredentialPool
from app.routing.router import Router
from app.services.health_service import HealthService


async def local_check(provider_ids: list[str] | None) -> dict[str, Any]:
    config = load_app_config(Settings())
    pool = CredentialPool(allow_inline_secrets=config.settings.allow_inline_secrets)
    for provider in config.providers.values():
        pool.register_provider(provider)
    router = Router(config)
    service = HealthService(config=config, router=router, pool=pool)
    try:
        return await service.check_all(kind="manual", provider_ids=provider_ids)
    finally:
        await router.aclose()


async def remote_check(base_url: str) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{base_url.rstrip('/')}/admin/health/check", json={})
        response.raise_for_status()
        return response.json()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Probe ZK-AI providers")
    parser.add_argument("-p", "--provider", action="append", dest="providers")
    parser.add_argument("--json", action="store_true", help="raw JSON output")
    parser.add_argument(
        "--remote", metavar="URL", help="call a running gateway instead of probing directly"
    )
    args = parser.parse_args()

    report = (
        await remote_check(args.remote)
        if args.remote
        else await local_check(args.providers)
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"health check ({report.get('kind', 'manual')}): "
          f"{report.get('ok', 0)} ok / {report.get('failed', 0)} failed "
          f"in {report.get('duration_ms', 0):.0f}ms\n")
    for result in report.get("results", []):
        flag = "OK  " if result["ok"] else "FAIL"
        detail = result.get("error_type") or f"{len(result.get('models') or [])} models"
        print(
            f"  [{flag}] {result['provider_id']:<14} "
            f"cred={result.get('credential_id') or '-':<16} "
            f"{result.get('latency_ms', 0):>8.1f}ms  {detail}"
        )
    return 0 if report.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
