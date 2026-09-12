"""Register ZK-AI as a custom provider inside ZCode's config.

ZCode (``D:\\ZCode\\ZCode.exe``) stores every model provider in
``~/.zcode/v2/config.json`` under the ``provider`` key. A provider entry looks
like::

    "provider": {
      "<id>": {
        "name": "ZK-AI",
        "kind": "openai-compatible",
        "source": "custom",
        "options": {
          "apiKey": "any",
          "baseURL": "http://127.0.0.1:8317/v1",
          "apiKeyRequired": true
        },
        "models": {
          "<model-id>": {
            "limit": {"context": 1000000, "output": 32768},
            "modalities": {"input": ["text"], "output": ["text"]},
            "zcode": {"modalitiesConfigured": true}
          }
        }
      }
    }

This script **backs the file up first**, touches only the ``provider`` key, keeps
every other key byte-identical, and can undo itself with ``--remove``.

Usage::

    python scripts/setup_zcode.py            # 查看将要写入的内容（不落盘）
    python scripts/setup_zcode.py --apply    # 实际写入
    python scripts/setup_zcode.py --remove   # 移除本脚本写入的 provider
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

#: Fixed id so re-running the script replaces instead of duplicating.
PROVIDER_ID = "zk-ai-gateway"
PROVIDER_NAME = "ZK-AI Gateway"

#: Address of the local gateway. ZCode runs on the same machine, so loopback.
BASE_URL = os.environ.get(
    "ZKAI_BASE_URL", f"http://127.0.0.1:{os.environ.get('ZKAI_PORT', '8317')}/v1"
)
#: The gateway authenticates *providers*, not clients - any non-empty string works.
API_KEY = "zk-ai-local"

#: ZK-AI aliases exposed to ZCode, with the context window the gateway enforces.
MODELS: dict[str, dict[str, int | list[str]]] = {
    "zk-auto": {"context": 1_000_000, "output": 32_768, "input": ["text"]},
    "zk-lite": {"context": 256_000, "output": 32_768, "input": ["text", "image"]},
    "zk-coding": {"context": 1_000_000, "output": 32_768, "input": ["text"]},
    "zk-reasoning": {"context": 1_000_000, "output": 32_768, "input": ["text"]},
    "zk-vision": {"context": 256_000, "output": 32_768, "input": ["text", "image"]},
    "zk-long": {"context": 1_000_000, "output": 32_768, "input": ["text"]},
    "zk-fast": {"context": 1_000_000, "output": 32_768, "input": ["text"]},
    "zk-cheap": {"context": 1_000_000, "output": 32_768, "input": ["text"]},
}


def default_config_path() -> Path:
    return Path.home() / ".zcode" / "v2" / "config.json"


def build_provider() -> dict:
    models = {}
    for model_id, spec in MODELS.items():
        models[model_id] = {
            "limit": {"context": spec["context"], "output": spec["output"]},
            "modalities": {"input": spec["input"], "output": ["text"]},
            "zcode": {"modalitiesConfigured": True},
        }
    return {
        "name": PROVIDER_NAME,
        "kind": "openai-compatible",
        "source": "custom",
        "options": {
            "apiKey": API_KEY,
            "baseURL": BASE_URL,
            "apiKeyRequired": True,
        },
        "models": models,
    }


def load(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"配置文件不存在: {path}\n先启动一次 ZCode 让它生成默认配置。")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} 不是合法 JSON，先修好它再跑本脚本: {exc}") from exc


def backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return target


def save(path: Path, data: dict) -> None:
    """Write atomically with a trailing newline, preserving UTF-8."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def cmd_show(path: Path) -> int:
    data = load(path)
    providers = data.get("provider") or {}
    exists = PROVIDER_ID in providers

    print(f"配置文件      : {path}")
    print(f"现有 provider : {len(providers)} 个")
    for key, value in providers.items():
        mark = "  <-- 本脚本管理" if key == PROVIDER_ID else ""
        print(f"  - {key:44s} {value.get('name', '')}{mark}")
    print()
    if exists:
        print(f"注意：{PROVIDER_ID} 已存在，--apply 会**覆盖**它，其余 provider 不受影响。")
    else:
        print(f"将新增 provider '{PROVIDER_ID}'（{PROVIDER_NAME}），共 {len(MODELS)} 个模型：")
        for model_id in MODELS:
            print(f"  - {model_id}")
    print()
    print("--- 将要写入的内容 ---")
    print(json.dumps({PROVIDER_ID: build_provider()}, ensure_ascii=False, indent=2))
    return 0


def cmd_apply(path: Path) -> int:
    data = load(path)
    data.setdefault("provider", {})[PROVIDER_ID] = build_provider()

    saved = backup(path)
    print(f"已备份 -> {saved}")
    save(path, data)
    print(f"已写入 -> {path}")
    print(f"新增/更新 provider: {PROVIDER_ID} ({PROVIDER_NAME})，{len(MODELS)} 个模型")
    print()
    print("下一步：重启 ZCode，在模型选择器里应能看到 ZK-AI Gateway 下的 zk-* 模型。")
    print(f"前提：ZK-AI 网关正在 {BASE_URL} 上运行。")
    return 0


def cmd_remove(path: Path) -> int:
    data = load(path)
    providers = data.get("provider") or {}
    if PROVIDER_ID not in providers:
        print(f"{PROVIDER_ID} 不在配置里，无需移除。")
        return 0
    del providers[PROVIDER_ID]
    saved = backup(path)
    print(f"已备份 -> {saved}")
    save(path, data)
    print(f"已从 {path} 移除 {PROVIDER_ID}（其余 provider 未改动）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 ZK-AI 注册为 ZCode 的自定义 provider")
    parser.add_argument("--config", type=Path, default=default_config_path())
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="实际写入配置")
    group.add_argument("--remove", action="store_true", help="移除本脚本写入的 provider")
    args = parser.parse_args(argv)

    if args.remove:
        return cmd_remove(args.config)
    if args.apply:
        return cmd_apply(args.config)
    return cmd_show(args.config)


if __name__ == "__main__":
    sys.exit(main())
