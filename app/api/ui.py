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
_EVENTS = _WEB / "events.html"
_TASKSPACES = _WEB / "taskspaces.html"
_MODELS = _WEB / "models.html"

#: CSS/JS companions for ``index.html`` (split out so the browser can cache them
#: independently and the HTML shell stays under 1k lines).
#: ``common.js`` 是独立页（agent/events/taskspaces/models）的共享外壳：
#: 令牌管理 + 带 401 识别的 api + 登录兜底（2026-11-08 手机远控 M1）。
_STATIC_ASSETS: dict[str, tuple[str, str]] = {
    "console.css": ("text/css; charset=utf-8", "console.css"),
    "console.js": ("application/javascript; charset=utf-8", "console.js"),
    "common.js": ("application/javascript; charset=utf-8", "common.js"),
}

#: 控制台是本地运维页，页面/脚本都很小但改动频繁。默认不带缓存头时浏览器会
#: 自行启发式缓存，重启网关甚至重新登录后仍拿旧的 index.html / console.js，
#: 表现为「刚加的入口和引导怎么都看不见」（2026-11-07 实测）。统一禁用缓存。
_NO_STORE = {"Cache-Control": "no-store, max-age=0"}


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
    return HTMLResponse(html, headers=_NO_STORE)


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
    return HTMLResponse(html, headers=_NO_STORE)


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
    return HTMLResponse(html, headers=_NO_STORE)


@router.get("/ui/events", include_in_schema=False, response_class=HTMLResponse)
async def events_console() -> HTMLResponse:
    """事件流页面：看得见「什么信号进来了、派给了谁、结论是什么」。

    这一页存在的理由和隔离横幅一样：自动化在后台跑，**必须让人看见它在跑什么**，
    否则「今天怎么一条都没有」会被当成系统坏了去查。
    """
    try:
        html = _EVENTS.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - file ships with the package
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"message": "事件流页面文件缺失"}},
        ) from exc
    return HTMLResponse(html, headers=_NO_STORE)


@router.get("/ui/taskspaces", include_in_schema=False, response_class=HTMLResponse)
async def taskspace_console() -> HTMLResponse:
    """任务空间页面：每个任务一个文件夹，看得见进度、能打开目录。

    与其它页面一样只发静态外壳；任务数据走 ``/zkadmin/agent/taskspaces``，
    仍由 ``require_admin`` 保护。
    """
    try:
        html = _TASKSPACES.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - file ships with the package
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"message": "任务空间页面文件缺失"}},
        ) from exc
    return HTMLResponse(html, headers=_NO_STORE)


@router.get("/ui/models", include_in_schema=False, response_class=HTMLResponse)
async def models_console() -> HTMLResponse:
    """模型与价格页面：每个工具能调哪些模型、分别什么价（来源逐条标明）。

    数据走 ``/zkadmin/agent/model-inventory``，仍由 ``require_admin`` 保护。
    """
    try:
        html = _MODELS.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - file ships with the package
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"message": "模型与价格页面文件缺失"}},
        ) from exc
    return HTMLResponse(html, headers=_NO_STORE)


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
    return PlainTextResponse(body, media_type=media_type, headers=_NO_STORE)


__all__ = ["router"]
