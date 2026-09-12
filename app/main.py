"""FastAPI application factory.

Run it with::

    uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

Responsibilities: configure logging, build the container during the lifespan,
install the request-id middleware, mount the routers and translate every error
into a stable JSON envelope.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.api import admin, chat, health, models, responses, ui
from app.core.config import Settings, get_app_config, reset_config_cache
from app.core.container import Container, build_container
from app.core.errors import ZKAIError
from app.core.logging import get_logger, request_id_var, setup_logging

logger = get_logger("main")

DESCRIPTION = """
ZK-AI is a personal AI gateway / model router.

* OpenAI-compatible `/v1/chat/completions` and a `/v1/responses` subset
* Provider / Deployment / Model / Credential abstraction
* Intelligent key pool with an explicit credential state machine
* Error-class driven retry, cooldown and failover
* Model aliases (`zk-auto`, `zk-coding`, ...) and a capability router
* Request, attempt, usage and health statistics
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    settings = settings or Settings()
    setup_logging(settings.log_level, json_logs=settings.log_json)
    reset_config_cache()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container: Container = await build_container(settings)
        app.state.container = container
        if not settings.admin_token and settings.admin_enabled:
            logger.warning(
                "admin API is enabled without ZKAI_ADMIN_TOKEN - set it before exposing "
                "the gateway publicly"
            )
        try:
            yield
        finally:
            await container.shutdown()

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    # The request-size guard middleware reads this; keep it on app.state so the
    # setting stays a config concern rather than a middleware import.
    app.state.max_request_body_bytes = int(settings.max_request_body_mb * (1 << 20))

    _install_middleware(app)
    _install_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(chat.router)
    app.include_router(responses.router)
    app.include_router(ui.router)
    if settings.admin_enabled:
        app.include_router(admin.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        config = get_app_config()
        return {
            "name": settings.app_name,
            "version": settings.version,
            "docs": "/docs",
            "health": "/health",
            "console": "/ui",
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
            "aliases": sorted(config.aliases),
        }

    return app


def _install_middleware(app: FastAPI) -> None:
    max_body_bytes = int(getattr(app.state, "max_request_body_bytes", 0) or 0)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Assign a request id, time the request and log its outcome."""
        request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:24]}"
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()

        # Reject oversized bodies *before* pydantic reads them into memory.
        # ``max_request_body_bytes`` comes from ``max_request_body_mb``.
        if max_body_bytes > 0:
            try:
                declared = int(request.headers.get("content-length", 0) or 0)
            except ValueError:
                declared = 0
            if declared > max_body_bytes:
                request_id_var.reset(token)
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "message": f"request body too large (limit {max_body_bytes // (1 << 20)}MB)",
                            "type": "context_length_exceeded",
                            "code": 413,
                        }
                    },
                )

        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        latency_ms = (time.perf_counter() - started) * 1000
        response.headers["x-request-id"] = request_id
        response.headers["x-zkai-latency-ms"] = f"{latency_ms:.1f}"
        if not request.url.path.startswith(("/health", "/docs", "/openapi")):
            logger.info(
                "%s %s -> %d in %.1fms",
                request.method,
                request.url.path,
                response.status_code,
                latency_ms,
            )
        return response


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ZKAIError)
    async def zkai_error_handler(_request: Request, exc: ZKAIError) -> JSONResponse:
        # Shared with the chat routes so every error path carries Retry-After.
        return chat.error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "invalid request payload",
                    "type": "invalid_request_error",
                    "code": 400,
                    "details": [
                        {"loc": list(item.get("loc", [])), "msg": item.get("msg", "")}
                        for item in exc.errors()[:10]
                    ],
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "internal gateway error",
                    "type": "internal_error",
                    "code": 500,
                }
            },
        )


app = create_app()
