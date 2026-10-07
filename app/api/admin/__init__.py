"""Administration API (``/admin/*``).

Everything an operator needs: inspect providers/models/credentials, force health
checks, enable/disable keys, explain routing decisions and read statistics.

Protected by ``ZKAI_ADMIN_TOKEN`` when configured (see :func:`app.api.deps.require_admin`).
Credential responses never contain secret material - only a masked fingerprint.

The surface is split into domain sub-modules that all register routes on the
single :class:`APIRouter` defined in :mod:`._common`. Importing this package
side-effects every sub-module, so ``from app.api import admin; admin.router``
yields the fully-wired router just as it did when everything lived in one file.
"""

from __future__ import annotations

# Importing the sub-modules registers their routes on the shared ``router``.
# The order does not matter (FastAPI matches by path+method, not registration
# order), but grouping by domain keeps the import list readable.
from app.api.admin import (  # noqa: F401  (side-effect: route registration)
    burner,
    chatgpt,
    models,
    providers,
    stats,
)
from app.api.admin._common import router

__all__ = ["router"]
