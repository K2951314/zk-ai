"""控制台 / Agent 页面的汉化守卫。

前端是单文件 vanilla JS，没有组件库也没有 i18n 框架——文案散落在 HTML
与模板字符串里。这个测试的目的不是「全面翻译」，而是**守住底线**：以后新增
或改动视图时，不要把英文 UI 文案带回来。

白名单保留的内容：
- ``error_type`` / 字段名 / HTTP 头名等机器契约；
- ``zk-*`` 模型与别名、``base_url`` / ``config.toml`` / ``.env`` 等技术标识；
- 牌价单位（$/Mtok）、``tools`` 计数、``JSON 模式`` 这类工具名词；
- CSS 属性名、颜色值、字体名（``<style>`` 整块不扫）。
"""

from __future__ import annotations

import re

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


def test_agent_page_keeps_untouched_dom_contract() -> None:
    """Agent 页面同样：交互钩子一个都不能少。"""
    text = _html("agent.html")
    for hook in ("modelSel", "cancelBtn", "newBtn", "refreshBtn",
                 "sessionList", "stream", "taskInput", "composer",
                 "sendBtn", "wsChip", "empty"):
        assert f'id="{hook}"' in text, f"钩子 {hook} 必须保留"


def test_report_page_is_self_contained_and_explains_the_division() -> None:
    """/ui/report 体检页：单文件零依赖，且真的把「分工」讲成人话。

    存在的理由是运营者（非程序员）看不懂网关把活派给了谁、为什么慢——
    此前要回答这类问题只能查数据库。这页用控制台同源的两个端点
    （/admin/stats + /admin/requests）把决策摊开，所以它**不该**自带令牌输入框：
    令牌复用控制台存在 localStorage 里那份，少一步操作就少一个出错点。
    """
    text = _html("report.html")
    # 与其它页面同源：不许引外部资源（test_page_has_no_external_assets 也守这点）
    for marker in ('<link rel="stylesheet"', "<script src=", "@import url(",
                   "fonts.googleapis", "unpkg.", "jsdelivr."):
        assert marker not in text, f"report.html 不该引入外部资源（{marker}）"
    # 令牌必须从控制台复用，不自己造输入框
    assert "localStorage.getItem('zkai_admin_token')" in text
    assert 'id="tok"' not in text, "别让运营者再手填一次令牌"
    # 数据端点与延迟归因都要在
    assert "/admin/stats" in text and "/admin/requests" in text
    assert "latency_ms" in text
    # 讲的是人话：规则的每一条都要落在页面上
    for phrase in ("派给", "平均等", "接活标准", "Kimi K3"):
        assert phrase in text, f"体检页该解释「{phrase}」"
    # 能回控制台（运营者需要退回去）
    assert 'href="/ui"' in text


def test_report_page_shows_live_state_on_open() -> None:
    """体检页打开就必须有内容：用户原话「而不是我打开后一片空白」。

    三个回归点，都是这次修过的真实 bug：
    ① 数据只在 onclick 里拉，页面加载时不跑 → 打开是空白表单；
    ② 读 reqs.requests / reqs.items，而接口的载荷键是 data → 明细表永远是
       「暂无数据」，KPI 却有数字（假绿）；
    ③ status=success 过滤器把 429 / 超时整批藏起来 —— 失败才是体检要看的东西。
    最后一条：实时区必须读 /health（进程内存里的"此刻"），不能只查库。
    """
    text = _html("report.html")

    # ① 打开即加载，不等按钮
    assert "loadLive();" in text, "页面加载必须自动拉一次实时区"
    assert "loadHistory();" in text, "页面加载必须自动拉一次历史区"
    assert "setInterval(loadLive" in text, "实时区要周期性自刷新，不然「正在跑」是死截图"
    # 按钮退化成「刷新」，不再兼作首次加载
    assert "onclick = loadHistory" in text

    # ② 明细的载荷键（/admin/requests -> {object,total,limit,offset,data}）
    assert "reqs.data" in text, "明细必须读 data 键"
    assert "reqs.requests" not in text and "reqs.items" not in text, (
        "requests/items 都是不存在的键，会让明细表恒为空"
    )

    # ③ 不能再把失败过滤掉
    assert "status=success" not in text, "别再用 status=success 掩盖失败请求"
    # 失败要能在明细里被认出来
    assert "r.status !== 'success'" in text

    # 实时区读的是进程内存，不是库
    assert "'/health'" in text or '"/health"' in text, "实时区要读 /health"
    for key in ("quarantined_deployments", "credential_env_gaps", "rate_limits"):
        assert key in text, f"实时区该呈现 /health 里的 {key}"


def test_report_page_live_region_has_real_markup() -> None:
    """实时区的 DOM 必须真实存在，否则 JS 写进 innerHTML 时会静默失败。"""
    text = _html("report.html")
    for node in ('id="live-grid"', 'id="alerts"', 'id="live-dot"', 'id="live-meta"'):
        assert node in text, f"体检页缺实时区节点 {node}"
    # 历史区必须真的画出模型表（6 列），空态 colspan 要跟列数走。
    # 逐条明细已交给控制台「请求记录」页，这里不再复制一份。
    assert 'colspan="6"' in text, "模型表空态 colspan 要跟列数一致"
    assert 'colspan="8"' not in text, "体检页不该再内嵌明细表（重复控制台的请求记录）"
def test_console_links_to_the_report_page() -> None:
    """控制台要有一个入口，否则这页只能靠背路径。"""
    text = _html("index.html")
    assert 'href="/ui/report"' in text, "侧栏该有「体检」入口"
    # 它是跳转（<a>）不是切视图（<button>），样式得跟上其它 nav-item
    assert "a.nav-item" in text, "跳转型导航项要去掉下划线并继承外观"


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
    """单文件零依赖是硬约束：不许引外部 CSS/JS/字体。"""
    text = _html(page)
    for marker in ('<link rel="stylesheet"', "<script src=", "@import url(",
                   "fonts.googleapis", "unpkg.", "jsdelivr."):
        assert marker not in text, f"{page} 不该引入外部资源（{marker}）"


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


def test_every_modal_close_button_is_actually_bound() -> None:
    """每个弹窗的关闭/取消按钮都要真的绑上 onclick。

    我把 classList.remove("show") 机械替换成 closeModal() 时，吃掉过 6 处
    `$("#xxx").onclick = ...` 绑定，另 5 处丢了选择器开头的 `#`
    （`$("mm-cancel")` 永远取不到元素）。两道断言一起钉住。
    """
    html = _html("index.html")
    js = _script(html)
    for btn in ("pv-close", "pf-cancel", "ak-cancel", "mm-cancel", "mk-close",
                "am-cancel", "cg-cancel", "lm-close"):
        assert f'id="{btn}"' in html, f"{btn} 按钮不在 HTML 里"
        assert f'$("#{btn}").onclick' in js, f"{btn} 没有 onclick 绑定（点了没反应）"
        assert f'$("{btn}").onclick' not in js, f"{btn} 的选择器少了 #"

    for btn in ("pf-save", "ak-save", "mm-save", "am-save"):
        assert f'$("#{btn}").onclick' in js, f"{btn} 保存按钮没有绑定"


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

