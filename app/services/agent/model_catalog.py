"""重点模型画像：只收录「最好的几款 + 免费的」，供选型与路由参考。

为什么不做全量：各执行体的模型清单会随账号和活动变化（qoder 从 3 个变 2 个、
codebuddy 的 hy4 只暴露 preview），手抄全量必然过期。全量交给 live 发现，
这里只维护一份**人工确认过**的重点清单，字段里带 ``verified`` 标明证据等级。

坏条目一律跳过并给中文告警，不让一条写错把整份画像拖垮。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: 成本档位（与 cli_tools.yaml 的 cost_tier 同一套词表）
_TIERS = frozenset({"free", "cheap", "paid"})
#: 证据等级：live=接口实测在列 / config=配置或日志确认 / unverified=待核实
_VERIFIED = frozenset({"live", "config", "unverified"})


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """一条重点模型画像。"""

    id: str
    name: str
    via: str
    tier: str
    context: int
    strengths: tuple[str, ...]
    verified: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "via": self.via,
            "tier": self.tier,
            "context": self.context,
            "strengths": list(self.strengths),
            "verified": self.verified,
            "note": self.note,
        }


def _parse(item: Any, index: int, warnings: list[str]) -> ModelEntry | None:
    """校验单条画像；不合法时记中文告警并返回 None。"""
    if not isinstance(item, dict):
        warnings.append(f"第 {index + 1} 条模型不是映射，已忽略")
        return None
    model_id = str(item.get("id") or "").strip()
    if not model_id:
        warnings.append(f"第 {index + 1} 条模型缺 id，已忽略")
        return None
    tier = str(item.get("tier") or "").strip()
    if tier not in _TIERS:
        warnings.append(
            f"模型 {model_id} 的 tier={tier!r} 不合法（可选 {sorted(_TIERS)}），已忽略"
        )
        return None
    verified = str(item.get("verified") or "unverified").strip()
    if verified not in _VERIFIED:
        warnings.append(
            f"模型 {model_id} 的 verified={verified!r} 不合法，按 unverified 处理"
        )
        verified = "unverified"
    try:
        context = max(0, int(item.get("context") or 0))
    except (TypeError, ValueError):
        context = 0
    raw_strengths = item.get("strengths")
    strengths: tuple[str, ...] = ()
    if isinstance(raw_strengths, list):
        strengths = tuple(
            text for text in (str(value).strip() for value in raw_strengths) if text
        )
    return ModelEntry(
        id=model_id,
        name=str(item.get("name") or model_id).strip(),
        via=str(item.get("via") or "").strip(),
        tier=tier,
        context=context,
        strengths=strengths,
        verified=verified,
        note=str(item.get("note") or "").strip(),
    )


def load_catalog(config_dir: Path) -> tuple[list[ModelEntry], list[str]]:
    """读 ``model_catalog.yaml``；文件缺失返回空（属正常状态）。"""
    path: Path | None = None
    for candidate in (
        Path(config_dir) / "model_catalog.yaml",
        Path(config_dir) / "model_catalog.yml",
    ):
        if candidate.exists():
            path = candidate
            break
    if path is None:
        return [], []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [], [f"模型画像读取失败：{exc}"]
    if raw is None:
        return [], []
    if not isinstance(raw, dict):
        return [], ["模型画像顶层不是映射，已整体忽略"]
    items = raw.get("models")
    if not isinstance(items, list):
        return [], ["模型画像缺少 models 列表，已忽略"]
    entries: list[ModelEntry] = []
    warnings: list[str] = []
    for index, item in enumerate(items):
        entry = _parse(item, index, warnings)
        if entry is not None:
            entries.append(entry)
    return entries, warnings
