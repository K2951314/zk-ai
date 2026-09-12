"""Model alias registry.

Aliases decouple the client from the underlying model: a client sends
``zk-coding`` and the gateway decides which concrete model serves it. The registry
is deliberately mutable at runtime - ``upsert`` / ``remove`` take effect on the
next request without any client change (exposed through
``POST /admin/router/reload`` and ``POST /admin/aliases``).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

from app.core.logging import get_logger
from app.models.provider import AliasStrategy, ModelAliasConfig

logger = get_logger("routing.aliases")

#: Aliases the gateway always knows about, so clients can rely on them.
BUILTIN_ALIASES: tuple[str, ...] = (
    "zk-auto",
    "zk-coding",
    "zk-reasoning",
    "zk-fast",
    "zk-cheap",
    "zk-long",
    "zk-vision",
    "zk-local",
    "zk-erpnext",
)


class AliasRegistry:
    """Thread-safe, hot-swappable alias table."""

    def __init__(self, aliases: Iterable[ModelAliasConfig] | None = None) -> None:
        self._lock = threading.RLock()
        self._aliases: dict[str, ModelAliasConfig] = {}
        for alias in aliases or ():
            self._aliases[alias.name] = alias

    # ------------------------------------------------------------------ #
    def get(self, name: str) -> ModelAliasConfig | None:
        with self._lock:
            alias = self._aliases.get(name)
            return alias if alias and alias.enabled else None

    def get_raw(self, name: str) -> ModelAliasConfig | None:
        with self._lock:
            return self._aliases.get(name)

    def is_alias(self, name: str) -> bool:
        with self._lock:
            return name in self._aliases

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._aliases)

    def enabled(self) -> list[ModelAliasConfig]:
        with self._lock:
            return [alias for alias in self._aliases.values() if alias.enabled]

    def all(self) -> list[ModelAliasConfig]:
        with self._lock:
            return list(self._aliases.values())

    # ------------------------------------------------------------------ #
    def upsert(self, alias: ModelAliasConfig) -> ModelAliasConfig:
        """Create or replace an alias (takes effect immediately)."""
        with self._lock:
            self._aliases[alias.name] = alias
        logger.info(
            "alias '%s' upserted (strategy=%s, targets=%s)",
            alias.name,
            alias.strategy.value,
            ",".join(alias.targets),
        )
        return alias

    def remove(self, name: str) -> bool:
        with self._lock:
            removed = self._aliases.pop(name, None) is not None
        if removed:
            logger.info("alias '%s' removed", name)
        return removed

    def enable(self, name: str, enabled: bool = True) -> bool:
        with self._lock:
            alias = self._aliases.get(name)
            if alias is None:
                return False
            alias.enabled = enabled
        logger.info("alias '%s' %s", name, "enabled" if enabled else "disabled")
        return True

    def replace_all(self, aliases: Iterable[ModelAliasConfig]) -> int:
        """Swap the whole table (used by config reload)."""
        with self._lock:
            self._aliases = {alias.name: alias for alias in aliases}
            return len(self._aliases)

    # ------------------------------------------------------------------ #
    def expand(self, name: str, *, model_ids: set[str], _depth: int = 0) -> list[str]:
        """Expand an alias into concrete model ids, following nested aliases.

        Targets that are neither a model nor an alias are skipped (a warning is
        emitted once by :meth:`validate`).
        """
        if _depth > 8:
            logger.warning("alias '%s' nesting too deep - aborting expansion", name)
            return []
        alias = self.get(name)
        if alias is None:
            return [name] if name in model_ids else []

        result: list[str] = []
        for target in alias.targets:
            if target in model_ids:
                result.append(target)
            elif self.get(target) is not None:
                result.extend(self.expand(target, model_ids=model_ids, _depth=_depth + 1))
        seen: dict[str, None] = {}
        for item in result:
            seen.setdefault(item, None)
        if not result:
            logger.warning("alias '%s' resolved to zero known models", name)
        return list(seen)

    def validate(self, *, model_ids: set[str]) -> list[str]:
        """Return configuration problems (unknown targets, empty aliases)."""
        problems: list[str] = []
        for alias in self.all():
            if not alias.targets:
                problems.append(f"alias '{alias.name}' has no targets")
            for target in alias.targets:
                if target not in model_ids and target not in self._aliases:
                    problems.append(f"alias '{alias.name}' target '{target}' is unknown")
        return problems

    def describe(self) -> dict[str, dict]:
        """Registry dump for the admin API."""
        return {
            alias.name: {
                "targets": list(alias.targets),
                "strategy": alias.strategy.value,
                "enabled": alias.enabled,
                "requires": alias.requires,
                "weights": alias.weights,
                "description": alias.description,
            }
            for alias in self.all()
        }

    # ------------------------------------------------------------------ #
    @classmethod
    def default(cls, *, model_ids: list[str]) -> AliasRegistry:
        """Build the built-in alias set (used when config defines none)."""
        ordered = sorted(model_ids)
        presets: dict[str, tuple[AliasStrategy, str, dict[str, float]]] = {
            "zk-auto": (AliasStrategy.CAPABILITY, "balanced default", {}),
            "zk-coding": (AliasStrategy.CAPABILITY, "code generation", {"coding": 6.0}),
            "zk-reasoning": (AliasStrategy.CAPABILITY, "deep reasoning", {"reasoning": 6.0}),
            "zk-fast": (AliasStrategy.SPEED, "low latency", {"speed": 6.0}),
            "zk-cheap": (AliasStrategy.COST, "low cost", {"cost": 6.0}),
            "zk-long": (
                AliasStrategy.CAPABILITY,
                "long context",
                {"long_context": 6.0},
            ),
            "zk-vision": (
                AliasStrategy.CAPABILITY,
                "vision",
                {"vision": 6.0},
            ),
            "zk-local": (
                AliasStrategy.PRIORITY,
                "local Ollama models",
                {},
            ),
            "zk-erpnext": (
                AliasStrategy.CAPABILITY,
                "ERP integration tasks (tools + structured output)",
                {"tool_use": 5.0, "structured_output": 5.0},
            ),
        }
        aliases = [
            ModelAliasConfig(
                name=name,
                targets=list(ordered),
                strategy=presets[name][0],
                description=presets[name][1],
                weights=presets[name][2],
            )
            for name in BUILTIN_ALIASES
            if name in presets
        ]
        return cls(aliases)
