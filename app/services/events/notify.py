"""审批/结论推送：飞书群机器人 webhook。

为什么用「群自定义机器人」而不是自建应用：
自建应用要 app_id/app_secret、要申请权限、要公网回调地址验签才能收回卡片动作。
**第一版只做单向推送**（卡片上带「去控制台处理」的按钮链接），
一个 webhook URL 就能跑，10 分钟能通。等单向推送真的不够用了，
再上自建应用 + 卡片回调 —— 那时候你已经知道自己需要点哪个按钮。

签名算法按飞书文档：``sign = base64(hmac_sha256(key=f"{ts}\\n{secret}", msg=""))``。
注意 key 是「时间戳+密钥」拼出来的字符串、msg 是空串，不是反过来。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.logging import get_logger
from app.services.events.rules import EventRule, RuleSet, in_silent_window, parse_windows

logger = get_logger("events.notify")

_FEISHU_TIMEOUT = 10.0

#: ``${FEISHU_WEBHOOK}`` -> 环境变量值。events.yaml 不受 load_app_config 的
#: 插值管，所以这里自己做一遍 —— webhook URL 里带 token，属密钥红线，
#: 只能进 ``.env``，不能写进被 git 跟踪的模板。
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: str) -> str:
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


@dataclass(slots=True)
class NotifyConfig:
    """推送配置。字段全部来自 ``events.yaml`` 的 ``notify`` 段。"""

    webhooks: list[str] = field(default_factory=list)
    secret: str = ""
    #: feishu = 发卡片；json = 原样 POST 事件 JSON（接自建服务/其他 IM 用）
    format: str = "feishu"
    silent: list[tuple[dt.time, dt.time]] = field(default_factory=list)
    #: 飞书是境内服务，默认直连不走系统代理（网关自己有代理，别串味）
    use_proxy: bool = False
    #: 卡片按钮跳转的控制台地址
    console_base: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.webhooks)


def build_notify_config(ruleset: RuleSet, *, console_base: str = "") -> NotifyConfig:
    raw = ruleset.notify or {}
    webhooks = raw.get("webhook") or raw.get("webhooks") or []
    if isinstance(webhooks, str):
        webhooks = [webhooks]
    urls = [_expand_env(str(url).strip()) for url in webhooks if str(url).strip()]
    urls = [url for url in urls if url]
    return NotifyConfig(
        webhooks=urls,
        secret=_expand_env(str(raw.get("secret") or "")),
        format=str(raw.get("format") or "feishu").lower(),
        silent=parse_windows(raw.get("silent")),
        use_proxy=bool(raw.get("use_proxy", False)),
        console_base=str(console_base or raw.get("console_base") or "").rstrip("/"),
    )


def should_push(
    *,
    tell: str,
    need_decision: bool,
    config: NotifyConfig,
    rule: EventRule | None,
    now: dt.datetime | None = None,
) -> tuple[bool, str]:
    """要不要推、不推的原因。返回 ``(是否推送, 原因)``。

    顺序有意为之：静默时段在 ``tell`` 之前判 —— 半夜「必须你批」的
    也要等到早上，除非规则自己写了 ``tell: always`` 且没配静默。
    """
    if not config.enabled:
        return False, "未配置 webhook"
    if tell == "never":
        return False, "规则要求不推送"
    if tell == "needed" and not need_decision:
        return False, "结论不需要拍板"
    windows = list(config.silent)
    if rule is not None:
        windows.extend(parse_windows(rule.silent))
    if windows and in_silent_window(windows, now or dt.datetime.now()):
        return False, "处于静默时段"
    return True, ""


def sign_feishu(timestamp: str, secret: str) -> str:
    """飞书群机器人签名。secret 为空时返回空串（未开启签名校验）。"""
    if not secret:
        return ""
    key = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(key, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_card(event: dict[str, Any], *, console_base: str = "") -> dict[str, Any]:
    """把一条事件渲染成飞书卡片：标题 + 结论 + 跳转按钮。"""
    need = bool(event.get("need_decision"))
    header_color = "orange" if need else "blue"
    title = "需要你拍板" if need else "自动化任务已完成"
    summary = str(event.get("summary") or "（无摘要）")
    verdict = str(event.get("verdict") or "").strip()
    if len(verdict) > 1200:
        verdict = verdict[:1200] + "…"

    lines = [
        f"**事件**：{summary}",
        f"**来源**：{event.get('source')} · {event.get('type')}",
        f"**规则**：{event.get('rule') or '（无）'} · 执行体：{event.get('role') or '（无）'}",
    ]
    if event.get("object"):
        lines.append(f"**对象**：{event['object']}")
    if verdict:
        lines.append("")
        lines.append("**结论**")
        lines.append(verdict)

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
    ]
    actions: list[dict[str, Any]] = []
    link = str(event.get("link") or "")
    if link:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看来源"},
                "type": "default",
                "url": link,
            }
        )
    if console_base:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "去控制台处理"},
                "type": "primary",
                "url": f"{console_base}/ui/agent",
            }
        )
    if actions:
        elements.append({"tag": "action", "actions": actions})
    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"事件 #{event.get('id')} · 花费 ¥{float(event.get('cost_cny') or 0):.4f}",
                }
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": header_color, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


async def push_event(
    event: dict[str, Any],
    config: NotifyConfig,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """推一条事件到所有 webhook。任一成功即算成功；失败只记日志不抛。"""
    if not config.enabled:
        return False
    ok = False
    for url in config.webhooks:
        body = _build_body(event, config)
        if body is None:
            continue
        if config.format == "feishu" and config.secret:
            timestamp = str(int(now.timestamp() if now else time.time()))
            body["timestamp"] = timestamp
            body["sign"] = sign_feishu(timestamp, config.secret)
        try:
            async with httpx.AsyncClient(
                timeout=_FEISHU_TIMEOUT, trust_env=config.use_proxy
            ) as client:
                response = await client.post(url, json=body)
            if response.status_code >= 400:
                logger.warning("事件推送失败 HTTP %s：%s", response.status_code, response.text[:200])
                continue
            payload = _safe_json(response)
            # 飞书成功是 {"code":0,...} / 老版本 {"StatusCode":0,...}
            code = payload.get("code", payload.get("StatusCode", 0))
            if code not in (0, None):
                logger.warning("事件推送被拒 code=%s：%s", code, payload.get("msg") or payload)
                continue
            ok = True
        except httpx.HTTPError as exc:
            logger.warning("事件推送网络失败：%s", exc)
    return ok


def _build_body(event: dict[str, Any], config: NotifyConfig) -> dict[str, Any] | None:
    if config.format == "feishu":
        return {
            "msg_type": "interactive",
            "card": build_card(event, console_base=config.console_base),
        }
    if config.format == "json":
        return {"event": event}
    logger.warning("不认识的推送格式 %r，已跳过", config.format)
    return None


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "NotifyConfig",
    "build_card",
    "build_notify_config",
    "push_event",
    "should_push",
    "sign_feishu",
]
