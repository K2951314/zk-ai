"""独立页共享外壳与页面路由的钉子（2026-11-08 手机远控 M1）。

- ``/ui/common.js`` 必须能被 serve：它是 agent/events/taskspaces/models 四页
  共用的令牌管理 + 401 登录兜底，路由丢了等于手机端登录兜底整体失效。
- 四个独立页必须都引用它：少引用一页，那一页的手机首开就还是 401 死路。
- taskspaces 不许再把 task_id 拼进内联 onclick（引号注入面，审查 P2）。
"""

from __future__ import annotations

import re
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
    """令牌只存 localStorage，**不许残留**在 URL / 页面 HTML 里。

    2026-11-11 修正断言方式：原来直接禁掉 ``location.search``，把
    「读一次 ?token= 然后立刻 replaceState 抹掉」这个**正确做法**也拦了。
    意图是「令牌不留在地址栏/历史/反代日志里」，不是「不许出现 location.search」。

    现在要求：①只写 localStorage；②如果从 URL 读，必须紧接着 replaceState
    抹掉——只读不抹等于把令牌留在历史里，那才是这条要防的事。
    """
    js = (_WEB / "common.js").read_text(encoding="utf-8")
    assert "localStorage.setItem" in js
    if "location.search" in js:
        assert "history.replaceState" in js, (
            "从 URL 读了令牌就必须立刻 replaceState 抹掉，否则令牌留在浏览器历史里"
        )
        assert 'params.delete("token")' in js, "要从 query 里删掉 token 再替换 URL"


def test_taskspaces_no_inline_onclick_with_task_id() -> None:
    """task_id 一旦来自目录名回退就可能带引号，内联拼接 = HTML/JS 注入面。"""
    html = (_WEB / "taskspaces.html").read_text(encoding="utf-8")
    assert "onclick=\"showDetail(" not in html
    assert "onclick=\"releaseSpace(" not in html
    assert "onclick=\"openFolder(" not in html
    assert 'data-act="detail"' in html, "动作按钮应改为事件委托 + data-task"


def test_agent_delegation_payload_carries_workspace() -> None:
    """@ 执行体直跑必须把界面选中的工作区一起提交。

    2026-11-12 实测：payload 只有 ``{ items }``，后端 ``_resolve_workspace``
    回落到网关项目根，把「修刀具 PDF 跳转」的任务派进了 ZK-AI 项目；
    CLI 全库检索不到 RCMT0803M0 后正确拒绝修改，但白跑 300s 超时。
    后端 ``DelegationPayload.workspace`` 一直支持，缺的就是前端这一行。
    """
    html = (_WEB / "agent.html").read_text(encoding="utf-8")
    start = html.index("const payload = { items };")
    end = html.index('api("/zkadmin/agent/delegations"', start)
    block = html[start:end]
    assert "payload.workspace = state.workspace" in block, (
        "@ 委派又漏了 workspace——后端会回落到网关项目根"
    )


def test_pages_stay_same_origin_only() -> None:
    """前端不许引外部资源（密钥红线之外的第二条同源纪律）。"""
    for page in ("agent.html", "events.html", "taskspaces.html", "models.html"):
        html = (_WEB / page).read_text(encoding="utf-8")
        for needle in ('src="http', 'href="http', 'src="//', 'href="//'):
            assert needle not in html, f"{page} 引了外部资源：{needle}"


def test_common_shell_exports_the_shared_infrastructure() -> None:
    """共享外壳必须真的导出渲染/反馈基础设施，而不只是令牌。

    2026-11-08 的 M1 只抽了「令牌 + 401 + 登录框」，`esc` 仍有 5 份、
    `api` 包装 3 份、时间格式化 4 份、反馈机制 4 种。2026-11-10 补齐：
    这些成员必须存在，否则四个独立页会各自长回一份。
    """
    js = (_WEB / "common.js").read_text(encoding="utf-8")
    for member in ("esc", "toast", "fmtTime", "fmtEpoch", "dur", "theme",
                   "token", "setToken", "api", "authError", "promptLogin"):
        assert f"{member}: {member}" in js, f"共享外壳没有导出 {member}"


def test_independent_pages_use_the_shared_esc_and_toast() -> None:
    """页面不许再定义自己的 esc / 时间格式化 / 反馈。

    允许页面保留**领域专用**的格式化（例如 events 的短时间、taskspaces 的
    卡片时间），但通用的 HTML 转义必须走 ZKUI.esc —— 五份实现各写各的
    转义规则是 XSS 面，也是"改一处漏四处"的根源。
    """
    for page in ("agent.html", "events.html", "taskspaces.html", "models.html"):
        html = (_WEB / page).read_text(encoding="utf-8")
        assert "ZKUI.esc" in html, f"{page} 还在用自己那份 esc，应改为 ZKUI.esc"
        # 自己再定义一份 function esc( 就说明没切干净
        assert "function esc(" not in html, f"{page} 仍定义了本地 esc()"


def test_agent_page_reads_the_token_lazily() -> None:
    """任务台的令牌必须每次读取，不能在加载时快照。

    原来是 `const TOKEN = localStorage.getItem(...)`：在控制台换了令牌之后，
    这个页面仍拿旧值发请求 → 401。ZKUI.promptLogin 保存后是 location.reload()
    （整页刷新），而 iframe 里的页面不会因为父页面刷新而重建，
    于是「换了令牌但任务台还是 401」。
    """
    html = (_WEB / "agent.html").read_text(encoding="utf-8")
    # 先剥掉注释再断言：注释里为了讲清历史会引用旧写法原文
    # （`/* */` 块内的中文续行不以 // 开头，只按行过滤抓不住）。
    code = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "const TOKEN = localStorage.getItem" not in code, "又变回加载时快照了"
    assert "ZKUI.token()" in code, "应从共享外壳按需取值"


def test_agent_stream_auth_failure_does_not_retry_forever() -> None:
    """SSE 遇到 401 必须弹登录框并停止重试。

    原行为：`throw new Error("事件流连接失败（HTTP 401）")` → scheduleStreamRetry
    按 1/2/4/8/16 秒退避重连，每次都是 401，而 promptLogin 永不调用。
    手机锁屏后令牌过期就是这个形状：页面一直自己连自己。
    """
    html = (_WEB / "agent.html").read_text(encoding="utf-8")
    assert "res.status === 401" in html, "事件流要单独处理 401"
    assert "noRetry" in html, "鉴权失败不能进重试队列"
    assert "ZKUI.promptLogin" in html


def test_session_list_surfaces_the_task_space_binding() -> None:
    """会话列表要显示它与任务空间的绑定关系。

    后端 `service._enter_task_space` 用 `session_id` 建任务空间，两者是
    **一对一绑定**的；但界面上原来完全看不出这层关系——左边是会话题，
    另一个一级导航是任务空间卡片，用户得自己猜哪张卡属于哪个会话。
    「任务空间」被做成了与「会话」平级的第二个主角，而它本该是会话的属性。
    """
    html = (_WEB / "agent.html").read_text(encoding="utf-8")
    assert "spaceBySession" in html, "会话列表要按 session_id 关联任务空间"
    assert "/zkadmin/agent/taskspaces" in html, "要拉任务空间列表来建索引"


def test_iframe_pages_inherit_the_console_theme() -> None:
    """iframe 里的页要能跟随控制台的主题，不能只看系统偏好。

    控制台用 `documentElement.dataset.theme` 切主题；iframe 子文档拿不到父文档
    的这个属性，而 events.html 原来只靠 `prefers-color-scheme` ——
    父文档暗、系统亮时，整个 iframe 亮着。共享外壳的 `theme()` 负责三级回落
    （父文档 → localStorage → 系统偏好）。
    """
    common = (_WEB / "common.js").read_text(encoding="utf-8")
    assert "window.parent" in common, "theme() 得先尝试读父文档的 data-theme"
    assert "prefers-color-scheme" in common, "最后一级要回落系统偏好"
