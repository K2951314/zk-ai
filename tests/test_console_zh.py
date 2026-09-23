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
    stripped = candidate.strip()
    if any(marker in stripped for marker in _CODE_MARKERS):
        return True
    if "." in stripped and " " not in stripped:
        return True
    return " " not in stripped and stripped.islower()


@pytest.mark.parametrize("page", ["index.html", "agent.html"])
def test_page_declares_chinese_lang(page: str) -> None:
    """``lang="zh-CN"`` 让浏览器选对中文字体与断行。"""
    assert 'lang="zh-CN"' in _html(page)


@pytest.mark.parametrize("page", ["index.html", "agent.html"])
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
        '<th class="acts">操作</th><th>模型</th><th>上下文</th>',
    ):
        assert thead in text, f"表头缺少左置操作列: {thead[:40]}"
    # 主键列要能换行，否则照样把表撑宽
    assert "td.name {" in text and "white-space: normal" in text


def test_console_supports_view_deep_links() -> None:
    """`?view=` 让「模型与别名」这类深页也能直接收藏 / 分享。"""
    text = _html("index.html")
    assert 'get("view")' in text
    assert "LOADERS[urlView]" in text, "必须校验视图名是否存在"


@pytest.mark.parametrize("page", ["index.html", "agent.html"])
def test_page_has_no_external_assets(page: str) -> None:
    """单文件零依赖是硬约束：不许引外部 CSS/JS/字体。"""
    text = _html(page)
    for marker in ('<link rel="stylesheet"', "<script src=", "@import url(",
                   "fonts.googleapis", "unpkg.", "jsdelivr."):
        assert marker not in text, f"{page} 不该引入外部资源（{marker}）"
