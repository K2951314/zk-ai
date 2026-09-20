"""First-run bootstrap for a fresh clone / new machine.

Two things that a clone is missing because they are deliberately untracked:
``.env`` (secrets) and the live ``config/*.yaml`` (operators' own config). A new
machine used to be told "copy .env.example and fill in the keys" in a batch file
that also warned about a missing ``.env`` only after the fact.

This script makes it a one-liner for the launcher: materialise the templates in
place, then report precisely what is still empty so the operator knows what to
fill. It is idempotent - once the files exist it does nothing and exits 0.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
#: Live config -> committed template. ``models.real``/``providers.real`` point
#: at real providers; the plain templates are the safe local/mock default.
_TEMPLATES: dict[str, str] = {
    "config/config.yaml": "config/config.example.yaml",
    "config/providers.yaml": "config/providers.example.yaml",
    "config/models.yaml": "config/models.example.yaml",
}
#: Variables an operator must fill before any provider can be reached.
_KEY_VARS = ("SENSENOVA_API_KEY", "NVIDIA_API_KEY", "MODELSCOPE_API_KEY", "MOONSHOT_API_KEY")


def ensure_env() -> str | None:
    """Create ``.env`` from the template. Returns the path when it was created."""
    target, template = _ROOT / ".env", _ROOT / ".env.example"
    if target.exists() or not template.exists():
        return None
    shutil.copyfile(template, target)
    return ".env"


def ensure_config_templates() -> list[str]:
    """Materialise the untracked live config files. Returns their paths."""
    created: list[str] = []
    for live, template in _TEMPLATES.items():
        source = _ROOT / template
        if (_ROOT / live).exists() or not source.exists():
            continue
        shutil.copyfile(source, _ROOT / live)
        created.append(live)
    return created


def _env_var(name: str) -> str:
    """Read one variable straight out of ``.env`` (empty when unset)."""
    try:
        text = (_ROOT / ".env").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def needs_keys() -> list[str]:
    """Provider key variables that are still empty (the "not configured yet" list)."""
    return [name for name in _KEY_VARS if not _env_var(name)]


def main(argv: list[str]) -> int:
    if "--quiet" in argv:
        return 0
    created: list[str] = []
    env_created = ensure_env()
    if env_created:
        created.append(env_created)
    created += ensure_config_templates()

    print("____________________________________________________")
    if created:
        print("  首次运行：已为你建好这些文件（从模板复制）")
        for path in created:
            print(f"    - {path}")
        print("  它们都在 .gitignore 里，不会被提交。")
    else:
        print("  首次运行检查：.env 与 config/*.yaml 都已就绪。")

    if env_created:
        missing = needs_keys()
        admin_empty = not _env_var("ZKAI_ADMIN_TOKEN")
        print("____________________________________________________")
        print("  还需你手动填两处（用记事本打开 .env 即可）：")
        if admin_empty:
            print("    1) ZKAI_ADMIN_TOKEN = 填一串随机字符（控制台登录用）")
        if missing:
            print(f"    2) {' / '.join(missing)} = 你的供应商 Key（至少填一个）")
        print("  保存后重新启动即可。删掉 .env 再启动会重复这一步。")
        print("____________________________________________________")
        return 1  # the launcher pauses and exits: do not start without a token
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
