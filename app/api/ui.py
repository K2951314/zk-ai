"""Serves the operator console at ``/ui``.

The page itself is a static, data-free shell: every value it shows comes from
the ``/admin/*`` endpoints, fetched from the browser with the admin token kept
in ``localStorage``. Serving the shell without a token therefore leaks nothing,
while the data endpoints stay protected by ``require_admin``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse, PlainTextResponse

router = APIRouter(tags=["web"])

_WEB = Path(__file__).resolve().parent.parent / "web"
_INDEX = _WEB / "index.html"
_AGENT = _WEB / "agent.html"
_REPORT = _WEB / "report.html"

#: CSS/JS companions for ``index.html`` (split out so the browser can cache them
#: independently and the HTML shell stays under 1k lines).
_STATIC_ASSETS: dict[str, tuple[str, str]] = {
    "console.css": ("text/css; charset=utf-8", "console.css"),
    "console.js": ("application/javascript; charset=utf-8", "console.js"),
}


@router.get("/ui", include_in_schema=False, response_class=HTMLResponse)
async def console() -> HTMLResponse:
    """The management dashboard (vanilla JS + CSS loaded from /ui/console.*)."""
    try:
        html = _INDEX.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - file ships with the package
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"message": "控制台页面文件缺失"}},
        ) from exc
    return HTMLResponse(html)


# IMPORTANT: /ui/agent and /ui/report must be registered BEFORE /ui/{asset},
# otherwise FastAPI matches them as asset="agent" / asset="report" and 404s.
@router.get("/ui/agent", include_in_schema=False, response_class=HTMLResponse)
async def agent_console() -> HTMLResponse:
    """The ZK-Agent task console (single-file vanilla JS)."""
    try:
        html = _AGENT.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - file ships with the package
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"message": "Agent 任务台页面文件缺失"}},
        ) from exc
    return HTMLResponse(html)


@router.get("/ui/report", include_in_schema=False, response_class=HTMLResponse)
async def health_report() -> HTMLResponse:
    """The plain-language gateway report (who serves what, and why it is slow).

    Same data endpoints as the console (``/admin/stats`` + ``/admin/requests``),
    so it stays protected by ``require_admin``; this shell carries no data of its
    own and reuses the token already stored in the browser by the console.
    """
    try:
        html = _REPORT.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - file ships with the package
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"message": "体检页面文件缺失"}},
        ) from exc
    return HTMLResponse(html)


@router.get("/ui/{asset}", include_in_schema=False)
async def console_asset(asset: str) -> PlainTextResponse:
    """Serve the console's CSS/JS companions (no data, no token needed)."""
    spec = _STATIC_ASSETS.get(asset)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    media_type, filename = spec
    path = _WEB / filename
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - file ships with the package
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"message": "控制台静态资源缺失"}},
        ) from exc
    return PlainTextResponse(body, media_type=media_type)


__all__ = ["router"]
