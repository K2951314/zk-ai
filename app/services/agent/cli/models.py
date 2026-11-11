"""发现某个执行体**真正能调**的模型清单。

## 只有实测算数（2026-11-11 被运营者当面纠正后的定稿）

上一版按「桌面端缓存是权威」来写。**错了**。运营者一句话点破：
「workbuddy 模型还是不对，不是试试获取的吗？」

把三条来源全实测一遍（本机真调 `/v1/chat/completions`，2026-11-11）：

- ``codebuddy --help`` 的 ``Currently supported:``（26 个）
  → 静态帮助文字，**17 个不存在**。
- 桌面缓存 ``acc-product-config-v3-*.json``（65 个）
  → 是整条**产品线**的模型表，不是你这个账号的权限。里面混着补全
    （``codewise-completions``）、视频（``kling-video-v3``）、图片
    （``hunyuan-image-alpha-edit``）。实测只有 **35 个是对话模型**，26 个 400。
- wb-proxy ``/v1/models``（14 个）
  → **也不准**：其中 8 个调不通（``11102 model not found``），
    而能用的 ``hy4free`` / ``glm-5.3`` 它**没列出来**。

结论：**没有任何清单可信，只有真发一次请求算数。**
这跟 AGENTS.md「供应商的 `/v1/models` 是目录不是你能调的」是同一条，
只是那次栽在供应商目录上，这次栽在桌面缓存和帮助文档上——三次都是
「把某种清单当成了权限」，教训一模一样。

## 现在的优先级

1. **活端点实测**（``probe_models()``）——对本机 wb-proxy 的实际可调模型
   逐个发 ``max_tokens=1``，只有 200 才算数。这是唯一可信来源。
2. **桌面缓存**——提供元数据（兑换率 / 工具 / 图片 / 上下文），
   但**只用来给实测通过的模型补充属性**，不能反过来当成清单。
3. **CLI 自己的口径**——``qoder --list-models`` / ``codebuddy --help``。
   ⚠ 保留它是因为运营者明确要求两条腿：CLI 登录的是另一个账号，额度叠加。
   但它是**静态文字**，只能当没有活端点时的兜底，且要标注置信度低。
4. 配置快照（调用方的事，返回空列表即是这个信号）。

## 为什么缓存里 26 个调不通

`11102 model not found` 一类是**这个账号没权限**；`11103 Backend [hunyu/image]`
是**后端根本没部署这类能力**（图片/视频模型走的是另一个接口，不是 chat）。
所以「缓存在列」连「目录里有」都不如——它是整个产品线的模型表。

缓存 5 分钟：实测要发 N 个 HTTP 请求（并发 6，约 3~5 秒），不能每次刷新都付。
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from app.services.agent.cli.launcher import LauncherError, resolve_command

_TTL = 300.0
#: ``tool:command`` -> (抓取时间, 模型 id 列表)
_CACHE: dict[str, tuple[float, list[str]]] = {}
#: ``tool:command`` -> (抓取时间, {model_id: 元数据})
_DETAIL_CACHE: dict[str, tuple[float, dict[str, dict]]] = {}

#: codebuddy --help 里的模型清单：`Currently supported: (a, b, c)`
_CB_SUPPORTED = re.compile(r"Currently supported:\s*\(([^)]*)\)", re.S)
#: qoder --list-models 的表头
_QODER_HEADER = "MODEL"
#: ``x0.21`` -> 0.21
_RATE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")

#: WorkBuddy / CodeBuddy 桌面端缓存目录。两个名字都试：
#: WorkBuddy 是 to C 端的封装，CodeBuddy 是同一套内核的另一个品牌。
_DESKTOP_CACHE_HOMES = ("~/.workbuddy/cache", "~/.codebuddy/cache")
_DESKTOP_CACHE_GLOB = "acc-product-config-v3-*.json"


# --------------------------------------------------------------------------- #
# WorkBuddy / CodeBuddy 桌面端缓存
# --------------------------------------------------------------------------- #
def _desktop_cache_files() -> list[Path]:
    """按 mtime 倒序列出桌面端缓存文件（最新的在前）。"""
    out: list[Path] = []
    for home in _DESKTOP_CACHE_HOMES:
        root = Path(os.path.expanduser(home))
        if not root.is_dir():
            continue
        try:
            out.extend(Path(p) for p in glob.glob(str(root / _DESKTOP_CACHE_GLOB)))
        except OSError:
            continue
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def _parse_rate(value: object) -> float | None:
    """``"x0.21"`` → 0.21；数字原样；``None``/``"?"`` → None。"""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = _RATE_RE.search(value)
    return float(match.group(1)) if match else None


def _norm_desktop_model(raw: dict) -> dict | None:
    """桌面端缓存的一条模型 → 我们的统一形状。

    跳过 ``custom-local:`` 开头的——那是用户自建的本地模型，
    不属于这个执行体的可调清单。
    """
    model_id = raw.get("id")
    if not model_id or str(model_id).startswith("custom-local:"):
        return None
    max_in = raw.get("maxInputTokens") or 0
    return {
        "id": str(model_id),
        "name": str(raw.get("name") or model_id),
        "rate": _parse_rate(raw.get("credits")),
        "tools": bool(raw.get("supportsToolCall")),
        "images": bool(raw.get("supportsImages")),
        "reasoning": bool(raw.get("supportsReasoning")),
        "max_input_tokens": int(max_in) if max_in else 0,
        "description": str(raw.get("descriptionZh") or raw.get("descriptionEn") or ""),
        "is_default": bool(raw.get("isDefault")),
        "vendor": str(raw.get("vendor") or ""),
    }


def read_desktop_catalog() -> tuple[list[dict], str]:
    """读桌面端缓存里的完整模型清单。

    返回 ``(models, source_note)``。读不到时 ``([], "")``——调用方回落到 CLI。
    这是**唯一的兑换率来源**：CLI 的 ``--help`` 和 ``--list-models`` 都不带价格。
    """
    for path in _desktop_cache_files()[:6]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        raw_models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(raw_models, list) or not raw_models:
            continue
        models = [m for m in (_norm_desktop_model(r) for r in raw_models if isinstance(r, dict)) if m]
        if models:
            return models, f"桌面端缓存 {path.name}"
    return [], ""


def desktop_discount_factor(model_id: str, now: float | None = None) -> float | None:
    """该模型当前生效的夜间折扣因子（未命中返回 None）。

    规则逆向自桌面端 ``modelPromotions``：折扣只在 23:00–次日 08:00 生效，
    同模型命中多条时取**最小**因子（对运营者最有利的那条）。
    """
    stamp = time.localtime(now if now is not None else time.time())
    if not (stamp.tm_hour >= 23 or stamp.tm_hour < 8):
        return None
    for path in _desktop_cache_files()[:6]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        promos = data.get("modelPromotions") if isinstance(data, dict) else None
        if not isinstance(promos, list):
            continue
        best: float | None = None
        for promo in promos:
            if not isinstance(promo, dict) or promo.get("kind") != "discount":
                continue
            if not promo.get("enabled", True):
                continue
            ids = promo.get("modelIds") or []
            if model_id not in ids:
                continue
            try:
                raw_factor = (promo.get("discount") or {}).get("factor")
                if raw_factor is None:
                    continue
                factor = float(raw_factor)
            except (TypeError, ValueError):
                continue
            best = factor if best is None else min(best, factor)
        if best is not None:
            return best
    return None


# --------------------------------------------------------------------------- #
# CLI 口径（保留：多账号 / 桌面端未登录时的唯一来源）
# --------------------------------------------------------------------------- #
def parse_qoder_models(text: str) -> list[str]:
    """``qoder --list-models`` 的输出是「MODEL + 每行一个名字」。"""
    models: list[str] = []
    for line in text.splitlines():
        name = line.strip()
        if not name or name.upper() == _QODER_HEADER:
            continue
        if name.startswith(("-", "Usage:", "Options:")):
            continue
        models.append(name)
    return models


def parse_codebuddy_models(text: str) -> list[str]:
    """从 ``--help`` 的 --model 说明里抠出清单。

    ⚠ 这是**静态帮助文字**，不是账号真实可用清单。实测 2026-11-11：
    26 个名字里 17 个在桌面端缓存中不存在。只当兜底用。
    """
    match = _CB_SUPPORTED.search(text)
    if not match:
        return []
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def _run(argv: list[str], timeout: float = 25.0) -> str:
    """跑一次 CLI 抓输出；失败一律当"没有清单"，不让面板因此报错。"""
    try:
        argv = resolve_command(argv)
    except LauncherError:
        return ""
    try:
        # argv 来自 cli_tools.yaml 的命令名 + 已解析的入口，不经 shell；
        # 这里只是"列个模型"，不接任何外部输入拼命令。
        proc = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def _cli_models(tool: str, command: str) -> list[str]:
    if shutil.which(command) is None:
        return []
    if tool == "qoder":
        return parse_qoder_models(_run([command, "--list-models"]))
    if tool in {"codebuddy", "workbuddy"}:
        return parse_codebuddy_models(_run([command, "--help"]))
    return []


# --------------------------------------------------------------------------- #
# 活端点实测（唯一可信来源）
# --------------------------------------------------------------------------- #
#: 本机 wb-proxy 的默认地址。它把 WorkBuddy 桌面端的订阅转发成 OpenAI 兼容
#: ``/v1``，所以「能不能调」在这里就是一次真实 chat 请求能不能 200。
#: 运营者可用 ``ZKAI_WB_PROXY_URL`` 覆盖（换端口时）。
_WB_PROXY_URL = "http://127.0.0.1:8787/v1"
#: 探测并发数。34 个模型 × 串行 3 秒 = 100 秒，没人会等；6 路约 5 秒。
#: 别开到 20+：wb-proxy 后面是真实订阅账号，并发探测本身就是一次限流压力。
_PROBE_CONCURRENCY = 6
#: 单个模型探测的超时。``max_tokens=1`` 只需等首字节，20s 足够分辨「挂死」和「慢」。
_PROBE_TIMEOUT = 20.0
#: 实测结果缓存 10 分钟（比清单长，因为它是几十次真实 HTTP 请求的代价）。
_PROBE_TTL = 600.0
#: ``{model_id: (可用, 错误摘要)}`` 及其抓取时间
_PROBE_CACHE: tuple[float, dict[str, tuple[bool, str]]] | None = None


def _wb_proxy_url() -> str:
    """wb-proxy 的 base_url，可用 ``ZKAI_WB_PROXY_URL`` 覆盖。"""
    return os.environ.get("ZKAI_WB_PROXY_URL", _WB_PROXY_URL).rstrip("/")


def _wb_proxy_key() -> str:
    """读 wb-proxy 的本地 API Key。

    ⚠ 只从环境/``.env`` 读，**绝不硬编码**（密钥红线，AGENTS.md）。
    读不到返回空串——调用方据此跳过实测，而不是发一个必定 401 的请求
    把「没配 Key」误判成「所有模型都挂了」。
    """
    key = os.environ.get("ZKAI_WBPROXY_KEY", "")
    if key:
        return key
    # .env 未必已进进程环境（AGENTS.md「.env 不会自动进进程环境」那条）。
    # 用字节读避免 CRLF 归一化，只取这一个键。
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[4] / ".env"):
        try:
            raw = candidate.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        for line in raw.splitlines():
            if line.startswith("ZKAI_WBPROXY_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


#: 探测请求给多少输出预算。
#: ⚠ **必须给足，不能用 max_tokens=1**（2026-11-11 实测踩过）：
#: 推理模型（``hy3`` / ``hy4free`` / ``step-5-preview`` 等 16 个）会先把 token
#: 全花在 ``reasoning_content`` 上，``content`` 为空、``finish_reason=length``。
#: 用 1 探测会把它们**全部误判成不可用**——实测误杀 16/39，比不探测还糟。
#: 200 够一次思考 + 一个词的回答，且探测成本仍可忽略。
_PROBE_MAX_TOKENS = 200
#: 探测用的提示词：要求一个确定的短回答，便于分辨「真在干活」和「回了空壳」。
_PROBE_PROMPT = "Reply with exactly one word: pong"


async def _probe_one(client: Any, model_id: str, key: str) -> tuple[str, bool, str]:
    """对一个模型发一次真实对话请求，判断它是否真的能干活。

    返回 ``(model_id, 是否可用, 错误摘要)``。这是唯一能证明「这个账号能调它」
    的方式——目录、帮助文字、桌面缓存全都可能撒谎（见模块 docstring 的表）。

    **可用的判据是「产出了内容」，不是「HTTP 200」**：
    - ``content`` 有字 → 正常对话模型
    - ``reasoning_content`` 有字 → 推理模型（思考了，正文可能还没开始）
    - 两者都空且 ``finish_reason=error`` → 不是对话模型（如 ``codewise-*``
      这类补全专用模型，只回一个空代码块）
    """
    try:
        resp = await client.post(
            f"{_wb_proxy_url()}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": _PROBE_PROMPT}],
                "max_tokens": _PROBE_MAX_TOKENS,
            },
        )
    except Exception as exc:
        return model_id, False, type(exc).__name__
    if resp.status_code != 200:
        # 把上游错误码留在摘要里：11102=模型不存在/无权限，11103=后端没这类能力。
        # 运营者要靠这个区分「没权限」和「压根不是对话模型」。
        detail = ""
        try:
            body = resp.json()
            detail = str((body.get("error") or {}).get("detail") or "")[:120]
        except Exception:
            detail = resp.text[:120]
        return model_id, False, f"HTTP {resp.status_code} {detail}".strip()
    try:
        body = resp.json()
    except Exception:
        return model_id, False, "200 但响应不是 JSON"
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = str(message.get("content") or "").strip()
    reasoning = str(message.get("reasoning_content") or "").strip()
    finish = choice.get("finish_reason")
    # ``finish_reason=error`` 是硬否决：``codewise-*`` 这类补全专用模型
    # 会返回只含空代码块的非空壳子，靠「有没有字」判不出来。
    # 实测 2026-11-11：3 个 codewise 全是这个形状。
    if finish == "error":
        return model_id, False, "finish_reason=error（不是对话模型）"
    if content.strip("` ") or reasoning:
        return model_id, True, ""
    return model_id, False, f"无产出（finish_reason={finish}）"


async def probe_models(candidates: list[str], *, force: bool = False) -> dict[str, tuple[bool, str]]:
    """对候选模型逐个实测，返回 ``{model_id: (可用, 错误摘要)}``。

    ``candidates`` 通常是桌面缓存里的全量 id，本函数负责筛出真正能调的那些。
    wb-proxy 不可达（没启动）或没配 Key 时返回空 dict——调用方据此回落到
    未实测的清单，而不是把「代理没开」当成「所有模型都挂了」。
    """
    global _PROBE_CACHE
    if not force and _PROBE_CACHE and time.time() - _PROBE_CACHE[0] < _PROBE_TTL:
        snapshot = _PROBE_CACHE[1]
        # 只回缓存里本次候选集的部分，不拿旧结论回答新问题
        hit = {m: snapshot[m] for m in candidates if m in snapshot}
        if hit:
            return hit
    key = _wb_proxy_key()
    if not key or not candidates:
        return {}
    import asyncio

    import httpx

    results: dict[str, tuple[bool, str]] = {}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=_PROBE_TIMEOUT) as client:
            sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

            async def guarded(model_id: str) -> tuple[str, bool, str]:
                async with sem:
                    return await _probe_one(client, model_id, key)

            for item in await asyncio.gather(
                *(guarded(m) for m in candidates), return_exceptions=True
            ):
                if isinstance(item, BaseException):
                    continue
                model_id, ok, err = item
                results[model_id] = (ok, err)
    except Exception:
        return {}
    if results:
        _PROBE_CACHE = (time.time(), results)
    return results


def probed_available(snapshot: dict[str, tuple[bool, str]]) -> set[str]:
    """从实测结果里挑出可用的 id，供调用方过滤清单。"""
    return {mid for mid, (ok, _) in snapshot.items() if ok}


def probe_errors(snapshot: dict[str, tuple[bool, str]]) -> dict[str, str]:
    """不可用模型 → 错误摘要，面板上要显示「为什么它不在清单里」。"""
    return {mid: err for mid, (ok, err) in snapshot.items() if not ok}


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def discover_models(tool: str, command: str) -> list[str]:
    """返回该执行体的可用模型（空列表 = 这条路径没有可信来源）。

    优先级：桌面端缓存（有兑换率，权威）→ CLI 自己的清单 → 空。
    """
    key = f"{tool}:{command}"
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < _TTL:
        return list(cached[1])
    if tool in {"codebuddy", "workbuddy"}:
        catalog, _ = read_desktop_catalog()
        ids = [str(m["id"]) for m in catalog]
        if ids:
            _CACHE[key] = (time.time(), ids)
            return list(ids)
    from_cli = _cli_models(tool, command)
    if from_cli:
        _CACHE[key] = (time.time(), from_cli)
    return from_cli


async def discover_model_details(tool: str, command: str) -> list[dict]:
    """返回模型清单 + 元数据（兑换率 / 工具 / 图片 / 上下文）。

    **桌面缓存只提供元数据，不决定清单**——它有 65 个 id，其中 26 个实测调不通
    （补全/视频/图片模型，或这个账号没权限）。清单由 :func:`probe_models`
    真发请求筛出来：只有 200 的才进结果。

    每个条目多两个字段：
    - ``probed``：``True`` 表示这条真发过请求且成功（唯一可信的证据）
    - ``probe_error``：不可用时的上游错误摘要（面板用来解释「为什么它不在列表里」）

    实测拿不到（wb-proxy 没开 / 没配 Key）时，**如实返回空列表**而不是把
    桌面缓存的 65 个当成可用清单——那正是上一版的错。
    """
    key = f"{tool}:{command}"
    cached = _DETAIL_CACHE.get(key)
    if cached and time.time() - cached[0] < _TTL:
        return [dict(m) for m in cached[1].values()]
    if tool not in {"codebuddy", "workbuddy"}:
        return [
            {
                "id": model_id, "name": model_id, "rate": None,
                "tools": None, "images": None, "reasoning": None,
                "max_input_tokens": 0, "description": "", "is_default": False,
                "vendor": "", "probed": False, "probe_error": "未实测",
            }
            for model_id in _cli_models(tool, command)
        ]
    catalog, _ = read_desktop_catalog()
    if not catalog:
        return []
    meta = {m["id"]: m for m in catalog}
    snapshot = await probe_models(list(meta))
    if not snapshot:
        # 一条都没测成：不能拿未验证的目录冒充清单
        return []
    details: dict[str, dict] = {}
    for model_id, (ok, err) in snapshot.items():
        base = dict(meta.get(model_id) or {
            "id": model_id, "name": model_id, "rate": None,
            "tools": None, "images": None, "reasoning": None,
            "max_input_tokens": 0, "description": "", "is_default": False,
            "vendor": "",
        })
        base["probed"] = ok
        base["probe_error"] = err
        details[model_id] = base
    if details:
        _DETAIL_CACHE[key] = (time.time(), details)
    return [dict(m) for m in details.values()]


def available_model_details(tool: str, command: str, *, include_unprobed: bool = True) -> list[dict]:
    """同步版：只返回**实测可用**的模型（给高频列表接口用）。

    与 :func:`discover_model_details` 的区别：这里绝不触发实测，只读
    :func:`probe_models` 的进程缓存。缓存冷时 ``include_unprobed=True``
    返回空（宁缺勿错），``False`` 同样返回空——两种情况下调用方都该回落到
    配置快照，而不是显示一份没验证过的清单。

    桌面缓存只用来**补元数据**，不用来决定有哪些模型。
    """
    if tool not in {"codebuddy", "workbuddy"}:
        return []
    global _PROBE_CACHE
    if _PROBE_CACHE is None or time.time() - _PROBE_CACHE[0] >= _PROBE_TTL:
        return []
    catalog, _ = read_desktop_catalog()
    meta = {m["id"]: m for m in catalog}
    out: list[dict] = []
    for model_id, (ok, err) in _PROBE_CACHE[1].items():
        base = dict(meta.get(model_id) or {"id": model_id, "name": model_id,
                                           "rate": None, "tools": None,
                                           "images": None, "reasoning": None,
                                           "max_input_tokens": 0, "description": "",
                                           "is_default": False, "vendor": ""})
        base["probed"] = ok
        base["probe_error"] = err
        out.append(base)
    return out


def cached_models(tool: str, command: str) -> list[str]:
    """取模型清单，**绝不触发 HTTP 或子进程**。

    对 WorkBuddy/CodeBuddy：只用 :func:`probe_models` 的实测缓存
    （实测 2026-11-11：桌面缓存 65 个里只有 39 个真能调，26 个 400）。
    **不再拿桌面缓存当清单**——上一版就是这么错的：面板列出
    ``kling-video-v3`` 这类根本不是对话模型的 id。
    实测缓存冷时返回空，调用方回落到配置快照。

    对其它执行体（qoder / gemini / …）：CLI 清单只从进程缓存读（要起 node，
    不能在高频列表接口里等）。
    """
    key = f"{tool}:{command}"
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < _TTL:
        return list(cached[1])
    if tool in {"codebuddy", "workbuddy"}:
        ids = [d["id"] for d in available_model_details(tool, command) if d.get("probed")]
    else:
        ids = []
    if ids:
        _CACHE[key] = (time.time(), ids)
    return ids


def cached_model_details(tool: str, command: str) -> list[dict]:
    """取模型详情（含兑换率），**只读实测缓存，冷时退回桌面元数据**。

    三级：
    1. 实测缓存命中 → 只返回 ``probed=True`` 的那些（真验证过，最可信）
    2. 实测缓存冷 → 返回桌面缓存的条目，全部标 ``probed=False``，
       兑换率照给（它是本地 JSON 读的，不是猜的），但**能不能调未知**
    3. 桌面缓存也没有 → 空

    ⚠ 第 2 级是 ``/model-inventory`` 首屏秒开的关键：该接口是高频的只读页，
    不能为了"确认真能调"去发 65 次 HTTP 请求（那要 5 秒）。但它也**不该**
    因为缓存冷就什么都不显示——桌面文件里明明有兑换率。
    所以如实区分：``probed=False`` 的条目，前端要说「未实测」，
    等运营者点「刷新实测」或任务台拉到清单，再升级成 ``probed=True``。
    """
    if tool not in {"codebuddy", "workbuddy"}:
        return []
    probed = [d for d in available_model_details(tool, command) if d.get("probed")]
    if probed:
        return probed
    catalog, _ = read_desktop_catalog()
    out: list[dict] = []
    for model in catalog:
        entry = dict(model)
        entry["probed"] = False
        entry["probe_error"] = "未实测"
        out.append(entry)
    return out


def clear_cache() -> None:
    """测试用：清掉进程级缓存（含实测快照）。"""
    global _PROBE_CACHE
    _CACHE.clear()
    _DETAIL_CACHE.clear()
    _PROBE_CACHE = None
