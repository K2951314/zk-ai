"""独立页共享外壳与页面路由的钉子（2026-11-08 手机远控 M1）。

- ``/ui/common.js`` 必须能被 serve：它是 agent/events/taskspaces/models 四页
  共用的令牌管理 + 401 登录兜底，路由丢了等于手机端登录兜底整体失效。
- 四个独立页必须都引用它：少引用一页，那一页的手机首开就还是 401 死路。
- taskspaces 不许再把 task_id 拼进内联 onclick（引号注入面，审查 P2）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

_WEB = Path(__file__).resolve().parent.parent / "app" / "web"


def _client() -> TestClient:
    return TestClient(create_app(Settings(environment="test")))


def test_common_js_is_served() -> None:
    with _client() as client:
        r = client.get("/ui/common.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "var ZKUI" in r.text, "共享外壳的全局名变了，四页都会 ReferenceError"
    assert "promptLogin" in r.text


def test_independent_pages_share_the_common_shell() -> None:
    for page in ("agent.html", "events.html", "taskspaces.html", "models.html"):
        html = (_WEB / page).read_text(encoding="utf-8")
        assert '/ui/common.js' in html, f"{page} 没有接入共享外壳（登录兜底缺失）"


def test_common_js_only_keeps_token_in_localstorage() -> None:
    """令牌不许进 URL、不许写进页面 HTML——只进 localStorage。"""
    js = (_WEB / "common.js").read_text(encoding="utf-8")
    assert "localStorage.setItem" in js
    assert "location.search" not in js, "共享外壳不该引入 ?token= 这类 URL 传令牌的路"


def test_taskspaces_no_inline_onclick_with_task_id() -> None:
    """task_id 一旦来自目录名回退就可能带引号，内联拼接 = HTML/JS 注入面。"""
    html = (_WEB / "taskspaces.html").read_text(encoding="utf-8")
    assert "onclick=\"showDetail(" not in html
    assert "onclick=\"releaseSpace(" not in html
    assert "onclick=\"openFolder(" not in html
    assert 'data-act="detail"' in html, "动作按钮应改为事件委托 + data-task"


def test_pages_stay_same_origin_only() -> None:
    """前端不许引外部资源（密钥红线之外的第二条同源纪律）。"""
    for page in ("agent.html", "events.html", "taskspaces.html", "models.html"):
        html = (_WEB / page).read_text(encoding="utf-8")
        for needle in ('src="http', 'href="http', 'src="//', 'href="//'):
            assert needle not in html, f"{page} 引了外部资源：{needle}"
