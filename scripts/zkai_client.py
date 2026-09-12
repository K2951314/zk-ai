"""ZK-AI 客户端封装：给其他 Python 项目 / 后台脚本调用本网关用。

设计目标（第一性）：
  * 调用方只说一句「简单还是复杂」，不需要知道模型名和供应商；
  * 复杂任务走 zk-coding（K3 → GLM-5.2 → DeepSeek 兜底链），简单任务走 zk-fast；
  * 网关全黑时自动降级到快速档，并打印原因，绝不静默挂起；
  * 零第三方依赖（只用手头的标准库），拷给哪个项目都能直接跑。

用法：

    from zkai_client import ask

    print(ask("把这段日志里的金额汇总出来"))                        # 简单任务
    print(ask("帮我重构这个 ERP 入库模块……", task="complex"))       # 复杂任务
    print(ask("分析这批报价……", task="complex", session_id="erp-2026-09"))

环境变量（都有默认值，本机直接用即可）：
  ZKAI_BASE_URL   默认 http://127.0.0.1:8000
  ZKAI_API_TOKEN  仅当网关设了 api_token 时才需要（默认不设）
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("ZKAI_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API_TOKEN = os.environ.get("ZKAI_API_TOKEN", "")

#: 任务档位 → 网关别名。简单任务要的是快+省；复杂任务走能力路由的全家桶。
TASK_ALIASES = {
    "simple": "zk-fast",      # 6.8 Flash Lite 之类：低延迟、免费
    "complex": "zk-coding",   # K3 → GLM-5.2 → DeepSeek：长上下文 + 工具调用
    "default": "zk-auto",     # 不给档位时的均衡默认
}

DEFAULT_TIMEOUT = 120.0


class GatewayError(RuntimeError):
    """网关返回的业务错误（限流、全池冷却、上游 5xx 等）。"""


def _post(path: str, payload: dict, *, timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    req = urllib.request.Request(BASE_URL + path, data=body, headers=headers)  # noqa: S310 - BASE_URL 是本机网关，调用方自有环境变量控制
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 同上，只连本机网关
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise GatewayError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GatewayError(f"网关连不上（{BASE_URL}）：{exc.reason}") from exc


def ask(
    prompt: str,
    *,
    task: str = "default",
    session_id: str | None = None,
    max_tokens: int = 2048,
    system: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    fallback: bool = True,
) -> str:
    """问网关一个问题，直接拿回文本答案。

    task:
      "simple"  → zk-fast（低延迟）
      "complex" → zk-coding（最强模型链）
      其他/省略  → zk-auto
    session_id:
      可选。给了之后，同一通多轮对话会被网关钉在同一把 Key 上，
      命中供应商前缀缓存（省 token、快）。后台批量任务里传个稳定 id
      还能让不同批次分摊到不同账号。
    fallback:
      True 时，复杂档全黑会降级到简单档再试一次（拿到不如预期但可用的回答），
      并在标准输出打印降级原因。
    """
    alias = TASK_ALIASES.get(task, TASK_ALIASES["default"])
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    payload: dict = {"model": alias, "messages": messages, "max_tokens": max_tokens}
    if session_id:
        payload["session_id"] = session_id  # 会话亲和：钉住同一把 Key

    try:
        data = _post("/v1/chat/completions", payload, timeout=timeout)
    except GatewayError as exc:
        if not fallback or task != "complex":
            raise
        # 复杂档（K3 链路）整体不可用：降级到快速档，让后台任务不被卡死。
        print(f"[zkai_client] 复杂档不可用（{exc}），降级到 zk-fast 重试…", flush=True)
        payload["model"] = TASK_ALIASES["simple"]
        data = _post("/v1/chat/completions", payload, timeout=timeout)

    choices = data.get("choices") or []
    if not choices:
        raise GatewayError(f"网关返回了没有内容的响应：{json.dumps(data)[:200]}")
    return (choices[0].get("message") or {}).get("content") or ""


def ask_batch(
    prompts: list[str],
    *,
    task: str = "default",
    concurrency: int = 4,
    session_prefix: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    system: str | None = None,
    fallback: bool = True,
) -> list[str | Exception]:
    """并发批量调用，返回与输入等长、保序的结果列表。

    单条失败不会拖死整批：对应位置放 ``GatewayError`` 实例（``isinstance`` 判断即可），
    成功位置放文本。``session_prefix`` 给了之后，第 i 条用 ``f"{prefix}-{i}"`` 作为会话
    键——不同批次钉到不同的 Key 上，多账号额度天然摊平。

    concurrency 默认 4：别为了快把免费账号的 tpm/rpm 打爆，4 路足够又快又稳。
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(index: int, prompt: str) -> str | Exception:
        try:
            return ask(
                prompt,
                task=task,
                session_id=f"{session_prefix}-{index}" if session_prefix else None,
                timeout=timeout,
                system=system,
                fallback=fallback,
            )
        except Exception as exc:  # 批量模式要容错：单条失败不能拖死整批
            return exc

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(one, i, p) for i, p in enumerate(prompts)]
        return [future.result() for future in futures]


def ask_json(
    prompt: str,
    *,
    task: str = "complex",
    session_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """要 JSON 返回时用：让模型只输出 JSON，并解析成 dict。

    失败会把原始文本放在异常里，方便排查。
    """
    data = _post(
        "/v1/chat/completions",
        {
            "model": TASK_ALIASES.get(task, TASK_ALIASES["default"]),
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            **({"session_id": session_id} if session_id else {}),
        },
        timeout=timeout,
    )
    choices = data.get("choices") or []
    text = (choices[0].get("message") or {}).get("content") or ""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GatewayError(f"模型没有返回合法 JSON：{text[:300]}") from exc


if __name__ == "__main__":
    # 手动冒烟：python scripts/zkai_client.py "你好"
    import sys

    question = " ".join(sys.argv[1:]) or "你好，用一句话介绍你自己"
    print(ask(question))
