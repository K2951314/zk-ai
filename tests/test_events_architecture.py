"""架构边界，写成测试。

借自 agent-autoloop 的 tests/purity.test.ts：它用测试钉住「内核不认识任何厂家
名字、内核自己不发请求、内核里时间和随机数从外面进来」。理由跟 AGENTS.md 上
那条一模一样——**规则写进文档 ≠ 会执行，要工具化**。

这里钉的是本项目的等价边界。每一条都对应一个曾经真实发生过、或者
很容易发生的腐化：

1. 事件中台层（app/services/events/）不该认识具体供应商。
   它只该知道「有个部署被隔离了」。一旦这里出现 sensenova / nvidia，
   换供应商时就会有人来改这一层，而这一层是按「通用调度」设计的。
2. 事件中台层不该自己发 HTTP。推送统一走 notify.py（那里才有
   代理开关、超时、签名这些细节）；散落的 httpx.post 会绕过它。
   notify.py 自己是唯一例外 —— 它就是那个出口。
3. 回执标记 RECEIPT_MARKER 必须是单一的来源。提示词里教的标记和解析器
   认的标记不一致，是「配置漂移」的另一种形状：系统照常运行，
   只是永远没人回执。
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVENTS = REPO / "app" / "services" / "events"

VENDORS = (
    "sensenova",
    "nvidia",
    "modelscope",
    "moonshot",
    "stepfun",
    "deepseek",
    "openrouter",
    "kimi",
    "qwen",
    "glm",
)


def _source_files() -> list[Path]:
    return sorted(path for path in EVENTS.rglob("*.py") if "__pycache__" not in path.parts)


def _code_only(path: Path) -> str:
    """去掉注释和文档字符串，只留代码。

    为什么要这样：这一层的注释里**故意**提到了供应商名字（举例说明
    「429 不等于渠道坏」）。注释不参与执行，不该被这条规则误伤。
    """
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return source
    pieces = [
        token.string
        for token in tokens
        if token.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE)
    ]
    return " ".join(pieces)


def test_the_event_layer_knows_no_vendor() -> None:
    """事件中台层只处理「部署 / 角色 / 事件」，不该出现具体供应商名。"""
    offenders: list[str] = []
    for path in _source_files():
        code = _code_only(path).lower()
        for vendor in VENDORS:
            if vendor in code:
                offenders.append(f"{path.relative_to(REPO)}: {vendor}")
    assert not offenders, (
        "事件中台层出现了供应商名（这一层应只认识部署和角色，"
        "供应商知识属于路由层）：\n  " + "\n  ".join(offenders)
    )


def test_only_notify_makes_outbound_requests() -> None:
    """推送统一出口是 notify.py —— 别的模块自己发 HTTP 会绕过代理/超时/签名。"""
    offenders: list[str] = []
    for path in _source_files():
        if path.name == "notify.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\bhttpx\.(AsyncClient|Client|post|get|request|stream)\b", text):
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, (
        "事件层里出现了绕过 notify.py 的 HTTP 调用：\n  " + "\n  ".join(offenders)
    )


def test_the_receipt_marker_has_one_definition() -> None:
    """回执标记只能定义一次。

    定义两处、其中一处改了另一处没改 = 提示词教的格式解析器不认。
    系统照常跑，只是永远没有人回执，而它看起来「一切正常」。
    """
    hits: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*RECEIPT_MARKER\s*=", text, re.M):
            hits.append(path.name)
    assert hits == ["service.py"], f"RECEIPT_MARKER 应当只在 service.py 定义一次，实际：{hits}"


def test_the_prompt_spec_uses_the_same_marker_as_the_parser() -> None:
    """提示词里教的标记，必须就是解析器认的那个 —— 不许各写一份字面量。

    拿真正会出现在结论里的形状（指令里教的代码块）去喂解析器，
    解析器要认得出，且要认对标记。
    """
    from app.services.events.service import (
        _RECEIPT_BLOCK,
        _RECEIPT_SPEC,
        RECEIPT_MARKER,
    )

    # 指令里必须真的出现这个标记，否则 Agent 根本不知道要写它
    assert RECEIPT_MARKER in _RECEIPT_SPEC
    # 而且必须是作为代码块的语言标记出现
    assert f"```{RECEIPT_MARKER}" in _RECEIPT_SPEC

    # 解析器必须能认出指令里教的那种写法
    sample = "结论。\n```" + RECEIPT_MARKER + '\n{"ask": []}\n```\n'
    match = _RECEIPT_BLOCK.search(sample)
    assert match is not None, "提示词教的写法解析器认不出来"
    assert '"ask"' in match.group(1)
