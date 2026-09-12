"""Initialise the ZK-AI database and mirror the YAML configuration.

Usage::

    uv run python scripts/init_db.py                 # create + sync
    uv run python scripts/init_db.py --reset         # drop everything first
    uv run python scripts/init_db.py --show          # print what is stored
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings, load_app_config
from app.database.db import Database
from app.database.models import Base
from app.database.repository import ConfigRepository


async def main() -> int:
    parser = argparse.ArgumentParser(description="Initialise the ZK-AI database")
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    parser.add_argument("--show", action="store_true", help="print stored rows")
    args = parser.parse_args()

    config = load_app_config(Settings())
    database = Database(config.settings.resolved_database_url)

    if args.reset:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        print(f"dropped all tables in {database.url}")

    await database.init()

    repository = ConfigRepository(database)
    counts = await repository.sync_config(config)
    print("configuration mirrored:")
    for key, value in counts.items():
        print(f"  {key:<12} {value}")

    warnings = config.warnings
    if warnings:
        print("\nconfig warnings:")
        for warning in warnings:
            print(f"  - {warning}")

    if args.show:
        print("\nproviders:", [row["id"] for row in await repository.list_providers()])
        print("models:", [row["id"] for row in await repository.list_models()])
        print("aliases:", [row["name"] for row in await repository.list_aliases()])
        print("\ncredentials:")
        for row in await repository.list_credentials():
            print(
                f"  {row['id']:<20} status={row['status']:<10} "
                f"ref={row['secret_ref'] or '-':<24} provider={row['provider_id']}"
            )

    await database.dispose()
    print(f"\ndatabase ready: {database.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
