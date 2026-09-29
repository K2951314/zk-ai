"""FastAPI application factory.

Run it with::

    uv run uvicorn app.main:app --host 0.0.0.0 --port 8317

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
from fastapi.exceptions import RequestValidationError, StarletteHTTPException
from fastapi.responses import JSONResponse, Response

from app.api import admin, agent, chat, health, messages, models, responses, ui
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


#: Paths that exist, but only under ``/v1``. A request to the bare path is a
#: client whose base_url is missing the ``/v1`` suffix - the single most common
#: misconfiguration for this gateway, and one the project's own providers.yaml
#: comments warn about twice.
_VERSIONED_PATHS: tuple[str, ...] = (
    "/chat/completions",
    "/messages",
    "/responses",
    "/models",
)


def _missing_v1_hint(path: str) -> tuple[str, str] | None:
    """Return ``(correct_path, base_url_with_v1)`` when *path* only lacks ``/v1``.

    ``None`` for every other 404, so unrelated unknown routes keep Starlette's
    default ``{"detail": "Not Found"}`` and no existing behaviour changes.
    """
    if not path.startswith("/v1/") and path != "/v1":
        for known in _VERSIONED_PATHS:
            if path == known or path.startswith(known + "/"):
                return f"/v1{path}", "/v1"
    return None


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
    app.include_router(messages.router)
    app.include_router(responses.router)
    app.include_router(ui.router)
    # /admin/agent/*: routes internally 404 when ZKAI_AGENT_ENABLED=false, and
    # require_admin already 404s when the admin surface is off.
    app.include_router(agent.router)
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
            "agent": "/ui/agent",
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
            "messages": "/v1/messages",
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
                # 这条分支以前直接 return，跳过了下面统一的 ``logger.info``，
                # 于是 413 在 gateway.log 里一个字都不留（53,402 行实测 0 命中），
                # 客户端只看到一句「请求体过大」，网关侧查无实据。
                # content-length 只有 >0 才算数：chunked 上传不带这个头，
                # declared=0 时这道闸整个失效，那种情况必须能从日志看出来。
                logger.warning(
                    "请求体过大，已拒绝 %s %s | 声明 %d B > 上限 %d B (req=%s)",
                    request.method,
                    request.url.path,
                    declared,
                    max_body_bytes,
                    request_id,
                )
                request_id_var.reset(token)
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "message": f"请求体过大（上限 {max_body_bytes // (1 << 20)}MB）",
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

    @app.exception_handler(StarletteHTTPException)
    async def not_found_handler(request: Request, exc: StarletteHTTPException) -> Response:
        """Say *what* was wrong with the path, not just "Not Found".

        2026-09-28 实测：客户端把 base_url 配成 ``http://127.0.0.1:8317``（漏了
        ``/v1``），网关只回 ``{"detail":"Not Found"}``。调用方看到的是
        「自定义模型 custom-local:zk-auto 错误，404」，既猜不到是路径问题，也
        猜不到正确答案——而同一次切换模型前它是好的，所以表面像「模型没了」。

        本项目自己的配置也踩过同一个坑（providers.yaml 里 sensenova / NVIDIA 的
        base_url 都必须以 /v1 结尾），所以这里对「少了一层 /v1」的路径直接给出
        正确的完整 URL，其余 404 保持 Starlette 原样，不改变任何既有契约。
        """
        if exc.status_code == 404:
            hint = _missing_v1_hint(request.url.path)
            if hint is not None:
                suggested, actual = hint
                logger.warning(
                    "404：%s %s 少了 /v1 前缀，正确路径是 %s",
                    request.method, request.url.path, suggested,
                )
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": {
                            "message": (
                                f"路径 '{request.url.path}' 不存在。"
                                f"OpenAI 兼容接口都挂在 /v1 下，正确路径是 "
                                f"'{suggested}'。请把客户端的 base_url 改成 "
                                f"'{actual}'（注意结尾要带 /v1）。"
                            ),
                            "type": "invalid_request_error",
                            "code": 404,
                            "details": {"correct_path": suggested},
                        }
                    },
                )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "请求体不合法",
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
                    "message": "网关内部错误",
                    "type": "internal_error",
                    "code": 500,
                }
            },
        )


app = create_app()
