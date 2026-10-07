"""控制台 / Agent 页面的汉化守卫。

前端是 vanilla JS，没有组件库也没有 i18n 框架——文案散落在 HTML
与模板字符串里。``index.html`` 已拆出 ``console.css`` / ``console.js``（同源提供），
但 ``agent.html`` / ``report.html`` 仍是单文件。这个测试的目的不是「全面翻译」，
而是**守住底线**：以后新增或改动视图时，不要把英文 UI 文案带回来。

白名单保留的内容：
- ``error_type`` / 字段名 / HTTP 头名等机器契约；
- ``zk-*`` 模型与别名、``base_url`` / ``config.toml`` / ``.env`` 等技术标识；
- 牌价单位（$/Mtok）、``tools`` 计数、``JSON 模式`` 这类工具名词；
- CSS 属性名、颜色值、字体名（``<style>`` 整块不扫）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT

_WEB = PROJECT_ROOT / "app" / "web"
_UI_PAGES = tuple(sorted(p.name for p in _WEB.glob("*.html") if p.is_file()))
#: 按目录自动发现，不手写清单。
#: 2026-09-30 之前就是因为手写清单漏掉了报告页（那时只列了
#: index/agent两个）。现在新加页面自动进入扫截。
assert _UI_PAGES, "app/web 下没有任何 html？路径不对"
#: 扫描范围：同时覆盖应用代码和运行脚两个目录。
_SCAN_ROOTS = (
    PROJECT_ROOT / "app",
    PROJECT_ROOT / "scripts",
)

# 允许出现的英文片段（技术名词 / 机器契约 / 代码示例）
ALLOWED_SNIPPETS = (
    "Tools", "Token", "HTTP", "Key", "YAML", "JSON", "zk-", "zk_ai",
    "base_url", "config.toml", "env_key", "wire_api", "model_provider",
    "ZKAI_", "SENSENOVA_", "NVIDIA_", "MODELSCOPE_", "MOONSHOT_",
    "mcp_servers", "plugins", "projects", "auth.json",
    "app/services", "sensenova-", "glm-", "kimi-", "deepseek-",
    "ui/agent", "http://127.0.0.1", "Authorization", "Bearer",
    "X-Admin-Token", "localhost", "Microsoft YaHei", "Segoe UI",
    "PingFang SC", "Cascadia Mono", "Consolas", "Courier New",
    "agent.default.model", "provider_display", "official_model",
    "model_reasoning_effort", "sensenova", "nvidia", "modelscope",
    "moonshot", "stepfun", "openai_compatible", "anthropic", "gemini",
    "openrouter", "ollama", "Mtok", "cred", "affinity", "rotation",
    "summary", "session", "status", "provider", "credential",
    "priority", "weight", "deployment", "deployments", "target",
    "targets", "strategy", "enabled", "disabled", "timeout",
    "context_window", "max_output_tokens", "input_cost_per_mtok",
    "output_cost_per_mtok", "capabilities", "vision_input",
    "reasoning_effort", "error_type", "error_rates", "window_seconds",
    "max_requests", "max_tokens", "rate_limits", "instance",
    # 桌面版 app 自己吐的原话，提示里必须原文引用，用户才能对上号
    "Missing environment variable",
    # 补 Key 进 .env 的 PowerShell 命令（代码示例，不是英文文案）
    "Add-Content -Path .env -Value",
    "GetEnvironmentVariable",
    # 模型家族显示名：体检页把 kimi-k3 显示成 Kimi K3、 step-5 显示成 Step 5，
    # 是产品名不是英文句子（与上面 sensenova- / glm- 同类）。
    "Kimi K3", "Step 5", "DeepSeek V4",
    "Step / DeepSeek", "Step 5 / DeepSeek",
    "GLM-5.3 / Step 5 / DeepSeek",
    # 产品名（不是英文句子）：接入面板的客户端清单 + 周锚点格式示例
    "Claude Code",
    "Method POST",
    "Wed 18:10",
)

_STYLE_RE = re.compile(r"<style>.*?</style>", re.DOTALL)
_SCRIPT_RE = re.compile(r"<script>.*?</script>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ATTR_NAME_RE = re.compile(r'\s[a-zA-Z-]+(?==)')
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_JS_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)

# 形如句子的英文：>=2 个单词、首字母大写，且不含 CSS/JS 标记
_SENTENCE_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Za-z0-9'(),.%/:;_\-]+){1,7}\b")

_CODE_MARKERS = (
    "font-", "border-", "background-", "transition", "transform",
    "animation", "box-shadow", "backdrop", "overflow", "position",
    "display", "flex", "grid-", "text-", "white-space", "word-break",
    "line-height", "font-family", "letter-spacing", "z-index",
    "placeholder", "vertical-align", "font-variant", "max-height",
    "min-width", "min-height", "max-width", "border-radius",
    "border-collapse", "linear-gradient", "rgba", "translateX",
    "translateY", "important", "before", "after",
)


def _html(name: str) -> str:
    path = _WEB / name
    assert path.exists(), f"{name} 应该随仓库一起提交"
    text = path.read_text(encoding="utf-8")
    # Inline external CSS/JS so the old <style>/<script> extraction helpers
    # keep working after the console was split into console.css / console.js.
    # The href/src may be a same-origin path like "/ui/console.css"; we take
    # the basename and look it up in app/web/.
    css_link = re.search(r'<link\s+rel="stylesheet"\s+href="([^"]+)"\s*>', text)
    if css_link:
        css_path = _WEB / Path(css_link.group(1)).name
        if css_path.exists():
            text = text.replace(
                css_link.group(0),
                f"<style>{css_path.read_text(encoding='utf-8')}</style>",
                1,
            )
    js_src = re.search(r'<script\s+src="([^"]+)">\s*</script>', text)
    if js_src:
        js_path = _WEB / Path(js_src.group(1)).name
        if js_path.exists():
            text = text.replace(
                js_src.group(0),
                f"<script>{js_path.read_text(encoding='utf-8')}</script>",
                1,
            )
    return text


def _raw_html(name: str) -> str:
    """Read the file as-is, without inlining external CSS/JS (for asset checks)."""
    path = _WEB / name
    assert path.exists(), f"{name} 应该随仓库一起提交"
    return path.read_text(encoding="utf-8")


def _scannable_text(html: str) -> str:
    """去掉 <style>/注释/标签名/属性名，只留下人看得见的文字内容。"""
    text = _STYLE_RE.sub(" ", html)
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _CSS_COMMENT_RE.sub(" ", text)
    text = _JS_COMMENT_RE.sub(" ", text)
    for allowed in ALLOWED_SNIPPETS:
        text = text.replace(allowed, " ")
    return text


def _scannable_js(html: str) -> str:
    """JS 里的模板字符串 / 字符串字面量也要扫（UI 文案大多在这儿）。"""
    match = _SCRIPT_RE.search(html)
    if not match:
        return ""
    js = match.group(0)
    js = _JS_COMMENT_RE.sub(" ", js)
    for allowed in ALLOWED_SNIPPETS:
        js = js.replace(allowed, " ")
    return js


def _is_code_like(candidate: str) -> bool:
    """形似代码而不是 UI 文案的匹配。"""
    stripped = candidate.strip()
    # 跨行的匹配一律算代码：UI 标签不会断成两行，
    # 而 <pre> 里的示例代码会。 2026-09-30 实测：体检页那段可复制的
    # Python 片段跨行匹出 "Router\nq"，靠白名单逐字替换拦不住。
    if chr(10) in candidate or chr(13) in candidate:
        return True
    if any(marker in stripped for marker in _CODE_MARKERS):
        return True
    if "." in stripped and " " not in stripped:
        return True
    return " " not in stripped and stripped.islower()


@pytest.mark.parametrize("page", _UI_PAGES)
def test_page_declares_chinese_lang(page: str) -> None:
    """``lang="zh-CN"`` 让浏览器选对中文字体与断行。"""
    assert 'lang="zh-CN"' in _html(page)


@pytest.mark.parametrize("page", _UI_PAGES)
def test_no_leftover_english_ui_sentences(page: str) -> None:
    """没有成句的英文 UI 文案（标题 / 提示 / 按钮标签级别）。"""
    text = _scannable_text(_html(page)) + "\n" + _scannable_js(_html(page))
    leftovers = [
        match.strip()
        for match in _SENTENCE_RE.findall(text)
        if not _is_code_like(match)
    ]
    assert not leftovers, f"{page} 里还有英文文案：{leftovers[:6]}"


def test_console_localises_the_key_prompts() -> None:
    """几个关键提示必须是中文，且不含英文句子。"""
    text = _html("index.html")
    for zh in (
        "管理令牌无效或缺失",   # 401 toast
        "模型不能为空",         # ChatGPT 面板校验
        "保存失败",             # 通用保存失败
        "删除失败",
        "需要管理令牌",
    ):
        assert zh in text, f"控制台应包含中文提示「{zh}」"


def test_console_keeps_untouched_dom_contract() -> None:
    """结构重构不能动 JS 依赖的钩子：视图 id、弹窗容器、导航 data-view。"""
    text = _html("index.html")
    for view in ("view-overview", "view-pool", "view-requests", "view-usage", "view-models"):
        assert f'id="{view}"' in text, f"视图 {view} 必须保留"
    for modal in (
        "model-modal", "alias-modal", "limits-modal", "market-modal",
        "addkey-modal", "providers-modal", "chatgpt-modal",
    ):
        assert f'id="{modal}"' in text, f"弹窗容器 {modal} 必须保留"
    for tab in ("overview", "pool", "requests", "usage", "models"):
        assert f'data-view="{tab}"' in text, f"导航 {tab} 必须保留"
    for hook in ("server-dot", "server-line", "btn-token", "btn-chatgpt",
                 "btn-refresh", "btn-theme", "auto-interval", "drawer", "toasts"):
        assert f'id="{hook}"' in text, f"钩子 {hook} 必须保留"


def test_client_access_panel_covers_both_deployment_shapes() -> None:
    """接入面板必须同时会渲染「公网调用」和「本机写入」两块。

    只做一边的另一半是静默失效：远程部署时仍显示写 config.toml（服务器的
    文件系统，不是打开控制台那台电脑的），或本机部署时反而让人去抄一个
    打不开的公网地址。判据就是 deploy 块的 is_remote / can_write_local。
    """
    html = _html("index.html")
    assert "cgPublicUrl()" in html
    assert "cgTokenName()" in html
    # 三种客户端的可复制片段（OpenAI SDK / curl / ZCode / Claude / ChatGPT 桌面版）
    for client in ("openai", "curl", "zcode", "claude", "chatgpt"):
        assert f'case "{client}":' in html, f"{client} 的配置片段不能少"
    # 远程时本机那段要整块隐藏，而不是留一个必然失败的按钮
    assert "cg-local-zone" in html
    assert 'localZone.style.display = "none"' in html
    assert "can_write_local" in html
    # 超时这个「抄错就打不开」的点必须出现在片段里（上游最坏跑几百秒）
    assert "TimeoutSec 420" in html


def test_server_status_line_does_not_depend_on_admin_calls() -> None:
    """/health 通了就必须显示连接状态，不能因为 /admin/* 401 就停在「未连接」。

    /health 不带 token 也能过（Caddy 对它有 Referer/Sec-Fetch-Site 白名单），
    而 /admin/* 要令牌。合成一个 Promise.all 的话，未登录时两个 admin 调用
    401 抛出，整段都不执行，状态行停在初始的「未连接」——运营者以为服务器挂了，
    其实只是没填令牌。2026-10-03 线上实测过这个表现。
    """
    html = _html("index.html")
    assert "renderServerLine(health)" in html
    assert "function renderServerLine(" in html
    # 部署形态要说出来：运营者第一件事就是确认该押哪个 base_url
    assert "服务器模式" in html
    assert "本机模式" in html
    # /health 失败时给明确原因，而不是留一句无信息的「未连接」
    assert "网关连不上" in html


def test_burner_view_has_log_and_reconcile_cards() -> None:
    """消耗器页必须同时有实时日志与账号核对两块，且都有 DOM 挂点。

    2026-10-07 改造：账本校准表格已合并到主窗口账表格（每行内联可编辑），
    rec-table 不再存在；rec-btn-diff / rec-btn-apply 移到主表格下方。
    """
    html = _html("index.html")
    for hook in ("bl-log", "bl-state", "bl-follow", "bl-copy", "bl-clear",
                 "rec-btn-diff", "rec-btn-apply", "rec-only",
                 "cal-actual", "cal-account", "cal-btn", "cal-adopt"):
        assert f'id="{hook}"' in html, f"挂点 {hook} 必须保留"
    # 主表格行内联可编辑：rec-in class 的 input 必须存在
    assert 'class="rec-in"' in html or "class='rec-in'" in html, "主表格必须有可编辑 input（rec-in）"
    # SSE + 降级：EventSource 断了要能自动重连，且轮询兜底
    assert "EventSource" in html
    assert "/admin/burner/log/stream" in html
    assert "/admin/burner/log/tail" in html
    # 核对走「预演 -> 确认 -> 落盘」三步，不能让一次手滑直接写账本
    assert "/admin/burner/reconcile" in html
    assert "/admin/burner/calibrate" in html


def test_burner_account_names_are_shortened() -> None:
    """消耗器页的账号名必须用简称（S_02）而不是全名（SENSENOVA_API_KEY_02）。

    全名 23 字符在表格里挤掉有用信息空间。简称 4 字符，title 属性保留全名
    供鼠标悬停查看。后端 ``_resolve_account_name`` 把简称还原成全名查账本。
    """
    js = _script(_html("index.html"))
    assert "function sName(" in js, "sName 函数必须存在"
    # 正则要把 SENSENOVA_API_KEY_02 变成 S_02、SENSENOVA_API_KEY 变成 S_01
    assert "S_" in js
    # placeholder 不得再出现过期的 _10（已改名到 _01）
    assert "SENSENOVA_API_KEY_10" not in js, "KEY_10 已改名到 _01，placeholder 不该再出现"


def test_console_js_has_no_duplicate_top_level_declarations() -> None:
    """同一顶层 const/let/function 不许声明两次——重复声明会让整段脚本中止。

    2026-10-05 真机翻车：我给消耗器页加 BURNER_GROUPS 分组时漏看了文件里
    已有同名声明，`Identifier 'BURNER_GROUPS' has already been declared`
    使整个 <script> 中止，控制台连接状态永远停在「未连接」、登录框也永不弹出。
    **所有现役测试都没抓到它**——中文扫描只看文案，DOM 契约只看 id，
    只有浏览器真跑 JS 才暴露。所以在这里用「顶层声明唯一性」静态兜住。
    """
    js = _script(_html("index.html"))
    seen: dict[str, str] = {}
    for decl in re.finditer(r"^(?:const|let|function)\s+([A-Za-z_$][\w$]*)", js, re.M):
        name = decl.group(1)
        assert name not in seen, (
            f"顶层声明 {name} 重复了（第 {decl.start()} 与 {seen[name]} 字节处）"
            "——重复声明会让整个脚本中止，页面静默失效"
        )
        seen[name] = str(decl.start())


def test_mobile_layout_has_a_real_information_architecture() -> None:
    """手机上不能只是「桌面仪表盘被撞窄」。

    改造前的实测（iPhone 390px）：侧边导航变成顶部横滑、凭证池表格 1171px 宽、
    消耗器页 4331px 高。所以要求三件事同时成立：底部 Tab（挤指可及）、
    表格卡片化（不再横拖）、长页面手风琴（一次只展开一节）。
    """
    html = _html("index.html")
    assert 'id="mnav"' in html, "底部 Tab 栏必须存在"
    # 底部 Tab 由 JS 从现有导航派生，不另写一份清单
    assert "function mnavBuild(" in html
    assert "MNAV = [" in html
    # 表格卡片化：通过 data-label 让 CSS 把列名显示在值前面
    assert "function tableCards(" in html
    assert "dataset.label" in html
    # 手风琴
    assert "function accordionSections(" in html
    assert '"msec"' in html
    # 底部 Tab 要四个（总览 / 模型 / 消耗器 / 更多），其余收进抽屉
    assert '{ view: "more"' in html, "\u5176\u4f59\u5165\u53e3\u6536\u8fdb\u62bd\u5c49\uff08more\uff09"


def test_desktop_keeps_the_sidebar_and_plain_tables() -> None:
    """手机化不能扫待桌面。

    实测（三个断点）：1440px 侧边导航可见、表格还是 table-row、没有手风琴、
    底部 Tab 隐藏；820px 与 390px 切换到手机版。这里把那套断路固化下来。
    """
    html = _html("index.html")
    # 底部 Tab 默认隐藏，只在窄屏媒体查询里打开
    assert "  #mnav { display: none; }" in html
    # 手风琴样式写在窄屏块里，不能漏到桌面
    css = _STYLE_RE.search(html).group(0)
    mobile_start = css.index("@media (max-width: 860px)")
    mobile_css = css[mobile_start:]
    assert ".msec {" in mobile_css
    assert ".msec {" not in css[:mobile_start]


def test_mobile_css_never_lets_content_widen_the_viewport() -> None:
    """哪个子元素写死宽度都不能把视口撑开。

    2026-10-04 改造时实测到：`#mnav` 被一个 392px 的 td 子元素带到 869px，
    固定定位的底部 Tab 就飘到屏幕外了。根节点要截断，所有 flex 子项要能收缩。
    """
    html = _html("index.html")
    assert "overflow-x: hidden" in html
    assert ".tablewrap tbody td > * { min-width: 0; }" in html
    # 进度条的内联 px 宽度在手机上要改流式
    assert "width: auto !important" in html
    # 长串（部署链 / 解析结果）必须能断行
    assert "word-break: break-all" in html


def test_mobile_time_shows_the_date_when_it_is_not_today() -> None:
    """只显示 10:44:41 让运营者无法判断请求是否过期。"""
    html = _html("index.html")
    assert "sameDay" in html
    assert "opts.month" in html and "sameDay" in html


def test_agent_page_keeps_untouched_dom_contract() -> None:
    """Agent 页面同样：交互钩子一个都不能少。"""
    text = _html("agent.html")
    for hook in ("modelSel", "cancelBtn", "newBtn", "refreshBtn",
                 "sessionList", "stream", "taskInput", "composer",
                 "sendBtn", "wsChip", "empty"):
        assert f'id="{hook}"' in text, f"钩子 {hook} 必须保留"


def test_report_page_is_self_contained_and_explains_the_division() -> None:
    """体检视图（原 /ui/report 独立页，2026-10-07 改为控制台内置）：
    用控制台同源的两个端点（/admin/stats + /admin/requests）把决策摊开，
    令牌复用控制台存在 localStorage 里那份。

    体检逻辑已移入 console.js 的 loadReport()，这里检查 console.js 的内容。
    """
    text = _html("index.html")  # 内联了 console.js
    # 数据端点与延迟归因都要在
    assert "/admin/stats" in text and "/admin/requests" in text
    assert "latency_ms" in text
    # 讲的是人话：规则的每一条都要落在页面上
    for phrase in ("派给", "平均等", "接活标准", "Kimi K3"):
        assert phrase in text, f"体检视图该解释「{phrase}」"
    # 令牌从控制台复用，不自己造输入框
    assert "localStorage.getItem('zkai_admin_token')" in text or "state.token" in text


def test_report_page_shows_live_state_on_open() -> None:
    """体检视图打开就必须有内容：用户原话「而不是我打开后一片空白」。

    2026-10-07：体检从独立页改为控制台内置视图，逻辑在 console.js 的 loadReport()。
    三个回归点，都是这次修过的真实 bug：
    ① 数据只在 onclick 里拉，页面加载时不跑 → 打开是空白表单；
    ② 读 reqs.requests / reqs.items，而接口的载荷键是 data → 明细表永远是
       「暂无数据」，KPI 却有数字（假绿）；
    ③ status=success 过滤器把 429 / 超时整批藏起来 —— 失败才是体检要看的东西。
    最后一条：实时区必须读 /health（进程内存里的"此刻"），不能只查库。
    """
    text = _html("index.html")  # 内联了 console.js

    # ① 打开即加载，不等按钮
    assert "reportLoadLive()" in text, "页面加载必须自动拉一次实时区"
    assert "reportLoadHistory()" in text, "页面加载必须自动拉一次历史区"
    assert "setInterval(reportLoadLive" in text, "实时区要周期性自刷新"
    # 按钮退化成「刷新」，不再兼作首次加载
    assert "reportLoadHistory" in text

    # ② 明细的载荷键（/admin/requests -> {object,total,limit,offset,data}）
    assert "reqs.data" in text, "明细必须读 data 键"
    assert "reqs.requests" not in text and "reqs.items" not in text, (
        "requests/items 都是不存在的键，会让明细表恒为空"
    )

    # ③ 不能再把失败过滤掉
    assert "status=success" not in text or "status === 'success'" in text, (
        "别再用 status=success 掩盖失败请求"
    )
    # 失败要能在明细里被认出来
    assert "r.status !== 'success'" in text

    # 实时区读的是进程内存，不是库
    assert "'/health'" in text or '"/health"' in text, "实时区要读 /health"
    for key in ("quarantined_deployments", "credential_env_gaps", "rate_limits"):
        assert key in text, f"实时区该呈现 /health 里的 {key}"


def test_report_page_live_region_has_real_markup() -> None:
    """实时区的 DOM 必须真实存在，否则 JS 写进 innerHTML 时会静默失败。

    2026-10-07：体检从独立页改为控制台内置视图，DOM 由 loadReport() 动态生成。
    """
    text = _html("index.html")  # 内联了 console.js
    for node in (
        'id="report-live-grid"', 'id="report-alerts"',
        'id="report-live-dot"', 'id="report-live-meta"',
    ):
        assert node in text, f"体检视图缺实时区节点 {node}"
    # 历史区必须真的画出模型表（6 列），空态 colspan 要跟列数走。
    assert 'colspan="6"' in text, "模型表空态 colspan 要跟列数一致"

def test_console_links_to_the_report_page() -> None:
    """控制台要有一个入口，否则这页只能靠背路径。

    2026-10-07：体检从独立页 /ui/report 改为控制台内置视图（data-view="report"）。
    旧链接 /ui/report 仍保留，但只做重定向到 /ui。
    """
    text = _raw_html("index.html")
    assert 'data-view="report"' in text, "侧栏该有「体检」入口（内置视图）"
    # 旧 a 标签跳转方式已废弃
    assert 'href="/ui/report"' not in text, "体检已改为内置视图，不再用 a 跳转"


def test_agent_page_renders_spawn_cards_in_chinese() -> None:
    """子任务派发的卡片必须有，且保持中文 + 单文件零依赖。

    spawn_subagent 是唯一不走审批卡的工具（它的 display 里没有 diff/command），
    所以前端必须认 `d.tool === "spawn_subagent"` 单独渲染——否则子任务结论会
    掉进通用 tool 分支，用户看到的是一堵 <pre>。
    """
    text = _html("agent.html")
    assert 'd.tool === "spawn_subagent"' in text, "spawn 卡片分支不能少"
    assert "派生子任务" in text and "查看子会话" in text
    # 子会话进度透传也要有对应分支，否则父页面完全看不到子在干什么
    assert 'case "child_event":' in text
    assert "childEventLabel" in text
    # 新样式必须真的定义了（少了就是渲染出来一坨没样式的原文）
    for css in (".spawn-body {", ".linklike {"):
        assert css in text, f"缺少 {css}"


def test_wide_tables_keep_actions_reachable_without_scrolling() -> None:
    """表格再宽，操作按钮也必须一直在手边。

    2026-09-23 的用户反馈：模型表 / 别名表的「编辑 / 删除」排在最右一列，表一宽就得
    右滑到底才够得着。修法是把操作列 `position: sticky` 钉在左（编辑）右（删除）两侧，
    主键列改成可换行、不再为它撑出横向滚动。

    这里守住三件事：sticky 规则存在、两个表的表头确实用了这两个 class、滚动容器
    是 `.tablewrap`（sticky 才有意义）。
    """
    text = _html("index.html")
    assert ".tablewrap" in text and "overflow: auto" in text, "滚动容器必须是 .tablewrap"
    for cls in ("th.acts, td.acts {", "th.acts-end, td.acts-end {"):
        assert cls in text, f"缺少 {cls} sticky 规则"
    assert "position: sticky" in text
    # 模型表 / 别名表 / 凭据池 / 供应商 / 模型市场 五张表
    for thead in (
        '<th class="acts">操作</th><th>模型</th>',
        '<th class="acts">操作</th><th>别名</th>',
        '<th class="acts">操作</th><th>凭据</th>',
        '<th class="acts">操作</th><th>供应商</th>',
        # 模型市场：2026-09-29 在「模型」与「上下文」之间插了「可调用」列
        '<th class="acts">操作</th><th>模型</th><th>可调用</th>',
    ):
        assert thead in text, f"表头缺少左置操作列: {thead[:40]}"
    # 主键列要能换行，否则照样把表撑宽
    assert "td.name {" in text and "white-space: normal" in text


def test_console_supports_view_deep_links() -> None:
    """`?view=` 让「模型与别名」这类深页也能直接收藏 / 分享。"""
    text = _html("index.html")
    assert 'get("view")' in text
    assert "LOADERS[urlView]" in text, "必须校验视图名是否存在"


@pytest.mark.parametrize("page", _UI_PAGES)
def test_page_has_no_external_assets(page: str) -> None:
    """不许引外部 CSS/JS/字体（CDN、第三方域名）。

    ``index.html`` 已拆出同源的 ``console.css`` / ``console.js``（由网关
    ``/ui/console.*`` 路由提供），它们是同源资源不是外部依赖。真正要禁的是
    ``fonts.googleapis`` / ``unpkg.`` / ``jsdelivr.`` 这类 CDN，以及
    ``@import url(https://...)`` 外部 URL。
    """
    text = _raw_html(page)
    # 外部 CDN 域名 / 协议绝对 URL 的 @import 一律禁止
    for marker in ("fonts.googleapis", "unpkg.", "jsdelivr.", "cdn.", "cdnjs.",
                   "@import url(http", "@import url(//"):
        assert marker not in text, f"{page} 不该引入外部资源（{marker}）"
    # link/script 的 href/src 必须是同源路径（/ 开头），不能是 http(s)://
    for m in re.finditer(r'<(?:link|script)\s+[^>]*(?:href|src)="([^"]+)"', text):
        url = m.group(1)
        assert not url.startswith(("http://", "https://", "//")), (
            f"{page} 不该引外部资源（{url}），只允许同源路径")


def test_all_page_js_lives_inside_the_script_tag() -> None:
    """所有 JS 必须在 <script> 里，<script> 里必须真的有个函数定义。

    2026-09-24 的真实事故：消耗器那 130 行（loadBurner / BURNER_LABELS /
    saveBurnerConfig…）被拼到了 `</html>` **之后**。浏览器把 </html> 之后的
    文本当普通 DOM 文本节点，根本不当脚本执行——于是 LOADERS.burner 指向一个
    从未声明的 loadBurner，进页面就 ReferenceError，整个控制台白屏，
    只剩一堆看起来像乱码的静态文字。截图放大看半天都看不出来，因为 DOM 里
    文字确实"在"，只是脚本死了。

    这里用「</html> 之后只剩空白」和「script 块里能匹配到顶层定义」两道断言
    把这类拼接错误钉死。
    """
    for page in _UI_PAGES:
        raw = _html(page)
        closed = raw.rindex("</html>")
        tail = raw[closed + len("</html>"):]
        assert tail.strip() == "", (
            f"{page} 的 </html> 之后还有 {len(tail.strip())} 个非空白字符，"
            "浏览器不会执行它")

        # 每个 <script> 块自身能匹配到顶层声明（空 script = 拼坏的信号）
        for i, block in enumerate(re.findall(r"<script[^>]*>(.*?)</script>",
                                             raw, re.DOTALL)):
            assert re.search(r"^(?:const|let|var|function|class)\s+\w",
                             block, re.MULTILINE), (
                f"{page} 的第 {i} 个 <script> 块里没有任何顶层声明，"
                "很可能有代码被拼到了标签外面")


def test_burner_view_loaders_are_declared_inside_the_script() -> None:
    """LOADERS.burner 引用的函数必须在同一个 <script> 块里 define。

    这正是上面那次事故的机理：LOADERS 写在块内，loadBurner 写在块外，
    对象字面量求值时抛 ReferenceError，把后面全部启动代码一起带走。
    """
    raw = _html("index.html")
    block = re.search(r"<script[^>]*>(.*?)</script>", raw, re.DOTALL).group(1)

    # LOADERS 里每个值都必须在这个 script 块内定义
    loaders = re.search(r"const LOADERS = \{(.*?)\n\};", block, re.DOTALL)
    assert loaders, "LOADERS 对象没找到"
    for name in re.findall(r"^\s*(\w+):\s*(\w+)\s*,?\s*$",
                           loaders.group(1), re.MULTILINE):
        registered = name[1]
        assert re.search(rf"^(?:async )?function {registered}\b",
                         block, re.MULTILINE), (
            f"LOADERS 引用了 {registered}，但同一 <script> 块里没有它的定义")


def test_attention_panel_receives_the_config_block() -> None:
    """「需要处理」面板必须真拿到 /health 的 config 段。

    2026-09-29 抓到的事实：面板读 ``health.config?.warnings``，而
    ``GET /health`` 从来没返回过 ``config`` 键——那一行面板**永远是空的**。
    功能写了、没人看见，比没写更糟：运营者以为配置很干净。
    """
    html = _html("index.html")

    assert "attentionPanel(providers.data, health.config?.warnings || [], health.config)" in html
    # 凭据缺口那一行也要接线到位（渲染 + 展开命令 + 点击分支）
    assert "cfg.credential_env_gaps" in html
    assert 'fix: "envgap"' in html
    assert 'b.dataset.fix === "envgap"' in html
    assert 'block.classList.contains("att-fix")' in html


def test_every_burner_config_key_has_a_chinese_label() -> None:
    """控制台表单里每个参数都要有中文名，不许裸奔英文键名。

    2026-09-30 真实发生过：饥饿救济、限流停靠、超时这三批参数是后来加的，
    BURNER_LABELS 是手写的映射表，加参数的人没同步它——于是运营者在表单里
    看到的是 ``rate_park_after`` 而不是「限流停靠：连续 429 次数」。
    修完那次靠的是人工反查；这个测试把反查固化，下次加参数会自动撞上。
    """
    from app.services import burner_service
    from scripts import burn_sensenova

    script = _script(_html("index.html"))
    labels = re.search(r"BURNER_LABELS = \{(.*?)\n\};", script, re.DOTALL)
    assert labels, "BURNER_LABELS 没找到"
    covered = set(re.findall(r'^\s*(\w+):\s*["\x27]', labels.group(1), re.MULTILINE))

    # 会出现在表单里的键 = 表单字段全集（FORM_FIELDS 是后端列出来渲染的顺序）
    missing = sorted(k for k in burner_service.FORM_FIELDS if k not in covered)
    assert not missing, f"这些参数在表单里还是英文键名：{missing}"

    # 可热改的键同样该有标签：它们随时可能被加进表单，先备好名字
    live = set(burner_service.FORM_FIELDS) | set(burn_sensenova._HOT_KEYS)
    unlabelled = sorted(k for k in live if k not in covered)
    assert not unlabelled, f"这些可配键没有中文标签：{unlabelled}"


def test_the_console_says_whether_config_edits_apply_by_themselves() -> None:
    """「改完 YAML 会不会自己生效」必须写在状态行上。

    关掉监听时（``ZKAI_WATCH_CONFIG=false``）运营者每改一次配置都得记得去点
    「重载配置」——而他不会记得。所以这不是装饰，是这条路径唯一的信息来源。
    """
    html = _html("index.html")

    assert "health.config_watch" in html
    assert "配置自动生效" in html
    assert "配置需手动重载" in html


# ================= 弹窗互斥（2026-09-30 回归守卫） =================


def _script(html: str) -> str:
    match = re.search(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    assert match, "找不到 <script> 块"
    return match.group(1)


def test_only_one_modal_shade_can_be_up_at_a_time() -> None:
    """openModal 必须先关掉别人再打开自己，且不许有裸的 classList.add("show")。

    2026-09-30 的两个真实症状同根因：新建供应商后加 Key 打不开、模型市场
    点「＋ 添加」没反应。七个 .modal-mask 原本共用同一个 z-index（55），
    谁盖住谁只由 DOM 顺序决定，而子弹窗（#addkey-modal / #model-modal）
    写在父弹窗（#providers-modal / #market-modal）**前面**，一打开就被压住。
    运营者只能退出重进，才发现东西早就加上了。
    """
    js = _script(_html("index.html"))

    assert "function openModal(sel)" in js, "打开弹窗必须走 openModal()"
    assert "function closeModal(sel)" in js
    assert "function closeModals(keep)" in js

    body = js.split("function openModal(sel)")[1].split("function closeModals")[0]
    remove_at = body.find("classList.remove")
    add_at = body.find("classList.add")
    assert remove_at != -1 and add_at != -1, "openModal 里没看到 remove/add"
    assert remove_at < add_at, "openModal 必须先关其他遮罩再打开目标，否则两层并存"

    for line in js.split("\n"):
        s = line.strip()
        if ("classList.add(\"show\")" not in s or "openModal" in s
                or "showAuth" in s):
            continue
        if s.startswith("*") or s.startswith("//") or s.startswith("/*"):
            continue
            raise AssertionError(f"绕过 openModal 直接开遮罩：{s}")


def test_modal_z_indexes_are_layered_not_all_identical() -> None:
    """z-index 必须分层：子 > 父。一个值就是「猜 DOM 顺序」，正是这次的根因。"""
    html = _html("index.html")
    css = re.search(r"<style>(.*?)</style>", html, re.DOTALL).group(1)

    def z_of(selector: str) -> int:
        found = re.search(re.escape(selector) + r"[^{]*\{[^}]*z-index:\s*(\d+)", css)
        assert found, f"{selector} 没有 z-index"
        return int(found.group(1))

    base = z_of(".modal-mask")
    assert base > 0
    for child in ("#model-modal", "#alias-modal", "#limits-modal"):
        assert z_of(child) > base, f"{child} 必须高于父遮罩"
    for opener in ("#market-modal", "#addkey-modal", "#chatgpt-modal"):
        assert z_of(opener) > base, f"{opener} 必须高于父遮罩"
    # 供应商管理本身也是唤起者（从凭据池打开）
    assert z_of("#providers-modal") > base
    # 令牌失校验必须压住一切
    assert z_of("#auth") > z_of("#providers-modal")


def test_saving_a_new_provider_stays_in_the_add_key_form() -> None:
    """新建供应商保存后直接停在加 Key 表单，不用先退出供应商列表。

    旧代码保存后先 openProvidersModal() 重绘列表、再 openAddKeyModal()，
    加 Key 窗口被列表压在下面——运营者原话：「新建供应商后退出才能继续
    添加 api key，不可以」。
    """
    js = _script(_html("index.html"))
    save = js.split('api("/admin/providers", { method: "POST"', 1)
    assert len(save) == 2, "POST /admin/providers 的调用点应只有一处"
    tail = save[1][:900]

    assert "openAddKeyModal(payload.id)" in tail
    # 从保存成功到打开加 Key 之间不许再调 openProvidersModal()：
    # 那一步会先把列表重绘出来，再让加 Key 压在它下面（原始 bug）。
    head = tail.split("openAddKeyModal(payload.id)")[0]
    assert "openProvidersModal" not in head, (
        "保存供应商后又回到列表，加 Key 窗口会被它盖住")

def test_market_add_model_saves_back_into_the_market() -> None:
    """市场点「＋ 添加」保存后回到市场并重探，让「已收录」立刻刷新。

    already_added 是探测那一刻算的旧值。不重探，保存完列表毫无变化，
    运营者会以为「点了添加没什么反应，退出后才发现已经加上了」。
    """
    js = _script(_html("index.html"))

    assert "_from_market: true" in js, "市场点添加时要标记来源"
    assert "const fromMarket = !modelId && !!(prefill && prefill._from_market)" in js

    body = js.split("#mm-save")[1].split("async function deleteModel")[0]
    assert "if (fromMarket)" in body
    assert "closeModals()" in body and "openMarketModal(" in body
    assert 'closeModal("#model-modal")' in body


def test_marketplace_verdict_labels_cover_the_backend_vocabulary() -> None:
    """后端可能返回的每种 verdict，前端都得有中文标签。

    2026-09-30 实测漏网：``_probe_verdict`` 会返回 ``timeout``（挂死），而
    VERDICT 映射表里没有这个键——「验证可调用」就把 ``timeout`` 这个英文原词
    直接渲染给运营者。挂死恰恰是这页最重要的一条结论（NVIDIA 的 glm-5.3
    就是这样每次白等 121 秒），结论越重越不能裸奔英文。

    词表从后端源码推导，不手抄一份清单：以后谁在 _probe_verdict 里新增一种
    结论，这个测试立刻撞上。和 BURNER_LABELS 加参数自动撞上是同一个套路。
    """
    backend = (PROJECT_ROOT / "app" / "api" / "admin" / "providers.py").read_text(encoding="utf-8")
    produced = set(re.findall(r'return "([a-z_]+)"', backend))
    produced |= set(re.findall(r'verdict": "([a-z_]+)"', backend))

    js = _script(_html("index.html"))
    block = re.search(r"const VERDICT = \{(.*?)\n      \};", js, re.DOTALL)
    assert block, "模型市场的 VERDICT 映射表没找到"
    labelled = set(re.findall(r"^\s*([a-z_]+):\s*\[", block.group(1), re.MULTILINE))

    missing = sorted(produced - labelled)
    assert not missing, f"这些 verdict 在前端还是裸英文：{missing}"
    # 每行都必须是 [颜色, 中文标签] 的形状。逐个拼正则引号太多，会被
    # shell/编辑器各种吃掉，所以按行拆开比。
    for line in block.group(1).splitlines():
        verdict = re.match(r"\s*([a-z_]+):", line)
        if verdict is None or verdict.group(1) not in produced:
            continue
        assert re.search(r'"([^"]+)"', line), (
            f"{verdict.group(1)} 这一行不是 [颜色, 中文标签] 的形状"
        )


def test_every_modal_close_button_is_actually_bound() -> None:
    """每个弹窗的关闭/取消按钮都要真的绑上 onclick。

    我把 classList.remove("show") 机械替换成 closeModal() 时，吃掉过 6 处
    `$("#xxx").onclick = ...` 绑定，另 5 处丢了选择器开头的 `#`
    （`$("mm-cancel")` 永远取不到元素）。两道断言一起钉住。
    """
    html = _html("index.html")
    js = _script(html)
    for btn in ("pv-close", "pf-cancel", "ak-cancel", "mm-cancel", "mk-close",
                "am-cancel", "cg-cancel", "cg-x", "lm-close"):
        assert f'id="{btn}"' in html, f"{btn} 按钮不在 HTML 里"
        assert f'$("#{btn}").onclick' in js, f"{btn} 没有 onclick 绑定（点了没反应）"
        assert f'$("{btn}").onclick' not in js, f"{btn} 的选择器少了 #"

    for btn in ("pf-save", "ak-save", "mm-save", "am-save"):
        assert f'$("#{btn}").onclick' in js, f"{btn} 保存按钮没有绑定"


def test_chatgpt_modal_close_is_not_inside_the_local_zone() -> None:
    """「客户端接入」的关闭路径不许再藏进 #cg-local-zone（2026-10-05 事故：
    服务器部署会 display:none 整个本机区，唯一的「取消」按钮跟着消失，
    面板上没有任何能关它的控件，只能刷新页面）。"""
    html = _html("index.html")
    zone = re.search(r'<div id="cg-local-zone">.*?(?=<hr class="sep">)', html, re.S)
    assert zone, "找不到 #cg-local-zone 块"
    assert 'id="cg-cancel"' not in zone.group(0), "关闭按钮又回到了本机区内部"
    assert 'id="cg-x"' not in zone.group(0), "✕ 按钮不该放在本机区内部"


# ---------------------------------------------------------------------------
# 用户可见英文的常驻守卫（2026-09-30 补；两次漏网换来的）
# ---------------------------------------------------------------------------

#: 后端会把这些字符串交给前端显示：经 snapshot() 进控制台凭据池页面，或经 API
#: 响应进 toast / 详情行。``reason`` 是这次漏网的直接原因——disabled_reason 与
#: Transition.reason 一路显示英文（auto-recovered / cooldown expired），而当时的
#: 扫描只覆盖 app/web，前端当然查不出后端的问题。
_USER_VALUE_KEYS = (
    "message",
    "detail",
    "reason",
    "hint",
    "label",
    "prompt",
)

# 两种写法都认：赋值 `reason = "..."`（允许一个下划线前缀，故
# disabled_reason 也命中）与字典 `{"message": "..."}`。
#
# ⚡正则由上面元组推导而来，不要在正则里再手写一遍。
# 2026-09-30 之前写死两份，新加键时漏改一处就静默忽略。
_PREFIXED = r"(?:[A-Za-z0-9]+_)?"
_NEG = r"(?<![A-Za-z0-9_])"
_Q = chr(34) + chr(39)          # both quote characters, immune to shell mangling
_VAL = r"([^" + _Q + "]{10,})"
_DQUOTE = chr(34)
_KEYS_ALT = "|".join(_USER_VALUE_KEYS)
_KEYS_QUOTED = "|".join(chr(34) + k + chr(92) + chr(34) for k in _USER_VALUE_KEYS)
_ASSIGN_RX = re.compile(
    _NEG + _PREFIXED + "(?:" + _KEYS_ALT + ")"
    + chr(92) + "s*[:=]" + chr(92) + "s*[f]?[" + _Q + "]"
    + _VAL + "[" + _Q + "]"
)
_DICT_RX = re.compile(
    "(?:" + _KEYS_QUOTED + ")"
    + chr(92) + "s*:" + chr(92) + "s*[f]?[" + _Q + "]"
    + _VAL + "[" + _Q + "]"
)

# 技术契约白名单：错误类型枚举、模型 id、协议串、牌价单位——机器契约，不是给人的。
_EN_ALLOWED_SUBSTR = (
    "timeout",
    "rate_limit_error",
    "quota_exceeded_error",
    "authentication_error",
    "permission_denied",
    "connection_error",
    "model_not_found",
    "invalid_model",
    "context_length_exceeded",
    "no_available_credential",
    "upstream_error",
    "flash-lite",
    "kimi-k3",
    "glm-",
    "deepseek-",
    "sensenova-",
    "qwen",
    "Mtok",
    "/v1/",
    "Bearer",
    "Authorization",
    "JSON",
    "YAML",
    "HTTP",
    "ZKAI_",
    "SENSENOVA_",
    "NVIDIA_",
    "MODELSCOPE_",
    "zk-",
# mock_upstream.py 的上游错误体：
# 它们存在的意义就是模拟真实上游的响应形状，
# 改成中文就测不到真实的错误分类逻辑了。
    "mock: invalid request",
    "mock: invalid api key",
    "mock: permission denied",
    "mock: model not found",
    "mock: rate limit exceeded",
    "mock: internal error",
    "mock: service unavailable",
    "mock: overloaded",
    "unknown path",
)

# 两个以上空格分隔的英文词 = 读起来像句子（允许小写开头：health check passed）
_CJK_RX = re.compile(r"[" + chr(0x4E00) + "-" + chr(0x9FFF) + "]")
_EN_SENTENCE_RX = re.compile(r"[A-Za-z]{2,}(?: [A-Za-z0-9'(),.%/:;_-]+){1,7}")


def _english_user_values(source: str) -> list[tuple[int, str]]:
    """(行号, 文案)：赋给用户可见键、且读起来是英文句子的字符串。"""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for match in list(_ASSIGN_RX.finditer(line)) + list(_DICT_RX.finditer(line)):
            value = match.group(1)
            if _CJK_RX.search(value):
                continue  # 中文为主体，夹带的英文是技术名词，不是给人读的句子
            if not _EN_SENTENCE_RX.search(value):
                continue
            if any(allowed in value for allowed in _EN_ALLOWED_SUBSTR):
                continue
            hits.append((lineno, value))
    return hits


def test_backend_never_hands_the_console_an_english_sentence() -> None:
    """后端不许把英文句子交给前端显示。

    守的是**路径**而不是某一处文案：值从 app/**/*.py 赋给 message/detail/reason
    等键，经 snapshot() 或 API 响应直达控制台页面。2026-09-30 的漏网正是这样：
    disabled_reason 显示 auto-recovered、cooldown expired，运营者天天看，而当时的
    扫描只覆盖 app/web——前端永远查不出后端的问题。

    两类合法英文被排除：① 中文句子夹带的技术名词（API Key、POST /admin/...）；
    ② 错误类型枚举、模型 id、协议串等机器契约。
    """
    offenders: list[str] = []
    for path in sorted(p for root in _SCAN_ROOTS for p in root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - 非源码文件
            continue
        for lineno, value in _english_user_values(source):
            loc = path.relative_to(PROJECT_ROOT).as_posix()
            offenders.append(f"{loc}:{lineno} {value!r}")
    assert not offenders, (
        "这些地方会把英文句子显示给运营者：" + chr(10) + chr(10).join(offenders)
    )


def test_the_guard_actually_scans_rather_than_always_passing() -> None:
    """守卫必须真的在扫，不能恒真。

    植一句英文进去，扫描器要报出来。否则以后有人把正则改坏了，测试仍然全绿——
    这条守卫就成了摆设。本项目已经为「规则写进文档不等于会执行」付过代价
    （AGENTS.md 有对应条目）。
    """
    planted = chr(10).join([
        "def f():",
        "    return {" + chr(34) + "message" + chr(34)
                + ": " + chr(34) + "Connection refused by upstream" + chr(34) + "},"
    ])
    hits = _english_user_values(planted)
    assert hits, "扫描器抓不到明显该抓的英文句子，它已经坏了"
    assert "Connection refused by upstream" in hits[0][1]


def test_the_guard_ignores_chinese_prose_and_machine_contracts() -> None:
    """中文句子夹带技术名词、以及纯错误类型枚举，都不算英文文案。

    否则测试会被 API Key / POST /admin 刷红，运营者学会忽略它，真正该抓的那次
    就被淹了——和自相矛盾的提示是同一类失败。
    """
    planted = chr(10).join([
        'def f():',
        '    return {\"message\": \"API Key 缿失或无效\", \"reason\": \"rate_limit_error\"}',
    ])
    assert _english_user_values(planted) == []


def test_the_guard_regexes_are_derived_from_the_key_tuple() -> None:
    """正则必须由键名元组推导，不允许再把键名写死一遍。

    2026-09-30 之前：键名清单在元组里一份、每个正则里又各写一遍。
    结果新加一个键时漏改元组那处，正则还是旧的，新键静默忽略。

    本测试保证这两个正则的口径覆盖到元组里的每个键。"""
    for rx in (_ASSIGN_RX, _DICT_RX):
        for key in _USER_VALUE_KEYS:
            assert key in rx.pattern, (
                f"{rx.pattern!r} 里没有 {key!r}，正则是不是又手写了一遍键名？ 键名只能出现在一处",
            )


def test_adding_a_key_to_the_tuple_flows_into_the_regexes() -> None:
    """真正的的防漏方式是：把一个新键加进元组之后，
    重建出来的正则能否拢住它。只检查今天的键可以看到这一点。

    所以这里按模块里的方式重建一遍正则再断言。
    """
    alt = "|".join((*_USER_VALUE_KEYS, "tooltip"))
    rebuilt = re.compile(
        _NEG + _PREFIXED + "(?:" + alt + ")"
        + chr(92) + "s*[:=]" + chr(92) + "s*[f]?[" + _Q + "]"
        + _VAL + "[" + _Q + "]"
    )
    assert "tooltip" in rebuilt.pattern

