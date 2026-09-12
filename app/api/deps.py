"""FastAPI dependencies: container access, admin auth, request metadata."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.container import Container
from app.core.security import constant_time_equals


def get_container(request: Request) -> Container:
    """Return the application container (built during startup)."""
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - startup always sets it
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"message": "gateway is starting up", "type": "not_ready"}},
        )
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def require_admin(
    request: Request,
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Protect ``/admin/*`` with ``ZKAI_ADMIN_TOKEN`` when it is configured.

    Behaviour:
      * ``admin_enabled=false`` -> 404 (the surface disappears entirely).
      * token configured -> ``X-Admin-Token`` or ``Authorization: Bearer`` must match.
      * no token configured -> admin endpoints are open (local development only);
        a warning is logged once at startup.
    """
    container = get_container(request)
    if not container.settings.admin_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="admin API disabled")

    expected = container.settings.admin_token
    if not expected:
        return

    provided = x_admin_token
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not constant_time_equals(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"message": "invalid admin token", "type": "unauthorized"}},
            headers={"WWW-Authenticate": "Bearer"},
        )


#: Use as a parameter annotation: ``admin: AdminDep``.
AdminDep = Annotated[Any, Depends(require_admin)]


async def require_client_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Protect the inference surface (``/v1/*``) when ``api_token`` is configured.

    OpenAI-compatible clients authenticate with ``Authorization: Bearer <key>``;
    the gateway historically ignored it. When ``ZKAI_API_TOKEN`` is set it is now
    compared (constant-time). Unset => fully open, unchanged from before.
    """
    container = get_container(request)
    expected = getattr(container.settings, "api_token", None)
    if not expected:
        return
    provided: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not constant_time_equals(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"message": "invalid or missing api key", "type": "authentication_error"}},
            headers={"WWW-Authenticate": "Bearer"},
        )


def client_metadata(request: Request) -> dict[str, str | None]:
    """Client IP + user agent, used for request telemetry."""
    forwarded = request.headers.get("x-forwarded-for")
    client_ip = forwarded.split(",")[0].strip() if forwarded else None
    if not client_ip and request.client:
        client_ip = request.client.host
    return {"client_ip": client_ip, "user_agent": request.headers.get("user-agent")}
