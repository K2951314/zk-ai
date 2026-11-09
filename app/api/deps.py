"""FastAPI dependencies: container access, admin auth, request metadata."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Query, Request, status

from app.core.container import Container
from app.core.security import constant_time_equals


def get_container(request: Request) -> Container:
    """Return the application container (built during startup)."""
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - startup always sets it
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"message": "网关正在启动", "type": "not_ready"}},
        )
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def _require_admin(
    request: Request,
    x_admin_token: str | None,
    authorization: str | None,
    query_token: str | None,
) -> None:
    """Shared body of :func:`require_admin` / :func:`require_admin_query`."""
    container = get_container(request)
    if not container.settings.admin_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="管理接口已停用")

    expected = container.settings.admin_token
    if not expected:
        return

    provided = x_admin_token
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided and query_token:
        provided = query_token.strip()
    if not constant_time_equals(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"message": "管理令牌无效", "type": "unauthorized"}},
            headers={"WWW-Authenticate": "Bearer"},
        )


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
        a warning is logged once at startup, and remote deployments refuse to
        mount the surface at all (see ``main.create_app``).
    """
    await _require_admin(request, x_admin_token, authorization, None)


async def require_admin_query(
    request: Request,
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
) -> None:
    """Admin auth that additionally accepts ``?token=`` (SSE endpoints only).

    ``EventSource`` 无法携带自定义 header，SSE 端点曾经因此把鉴权整个去掉
    （2026-11-08 审查 P0：/zkadmin/burner/log/* 裸奔）。这里的取舍：令牌进
    query 会落在反向代理的访问日志里，网关自身的访问日志只记 path（main.py
    中间件），Caddy/Nginx 若开了 query 日志则由部署方决定。根治方案是
    一次性短票（POST 换 ticket，60s 有效），那属于手机远控 M2 的令牌交接改造。
    """
    await _require_admin(request, x_admin_token, authorization, token)


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
            detail={"error": {"message": "API Key 缺失或无效", "type": "authentication_error"}},
            headers={"WWW-Authenticate": "Bearer"},
        )


def client_metadata(request: Request) -> dict[str, str | None]:
    """Client IP + user agent, used for request telemetry."""
    forwarded = request.headers.get("x-forwarded-for")
    client_ip = forwarded.split(",")[0].strip() if forwarded else None
    if not client_ip and request.client:
        client_ip = request.client.host
    return {"client_ip": client_ip, "user_agent": request.headers.get("user-agent")}
