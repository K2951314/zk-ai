"""Serves the operator console at ``/ui``.

The page itself is a static, data-free shell: every value it shows comes from
the ``/admin/*`` endpoints, fetched from the browser with the admin token kept
in ``localStorage``. Serving the shell without a token therefore leaks nothing,
while the data endpoints stay protected by ``require_admin``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["web"])

_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"


@router.get("/ui", include_in_schema=False, response_class=HTMLResponse)
async def console() -> HTMLResponse:
    """The management dashboard (single-file vanilla JS)."""
    try:
        html = _INDEX.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - file ships with the package
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"message": "console assets unavailable"}},
        ) from exc
    return HTMLResponse(html)


__all__ = ["router"]
