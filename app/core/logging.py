"""Structured, secret-safe logging.

Every log line carries ``request_id`` / ``attempt`` from context variables, so a
full request lifecycle can be reconstructed from the log file. A logging filter
scrubs anything that looks like a credential before it hits a handler.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from typing import Any

from app.core.security import redact_mapping, redact_text

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("zkai_request_id", default="-")
attempt_var: contextvars.ContextVar[int] = contextvars.ContextVar("zkai_attempt", default=0)
provider_var: contextvars.ContextVar[str] = contextvars.ContextVar("zkai_provider", default="-")
credential_var: contextvars.ContextVar[str] = contextvars.ContextVar("zkai_credential", default="-")
model_var: contextvars.ContextVar[str] = contextvars.ContextVar("zkai_model", default="-")

_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


class ContextFilter(logging.Filter):
    """Inject request context and scrub secrets."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.attempt = attempt_var.get()
        record.provider = provider_var.get()
        record.credential = credential_var.get()
        record.model = model_var.get()

        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact_mapping(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_text(a) if isinstance(a, str) else a for a in record.args
                )
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line - friendly for log shippers."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "attempt": getattr(record, "attempt", 0),
            "provider": getattr(record, "provider", "-"),
            "model": getattr(record, "model", "-"),
            "credential_id": getattr(record, "credential", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_") and key not in payload:
                payload[key] = redact_mapping(value)
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Compact human readable format for local development."""

    default_format = (
        "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"
        " (provider=%(provider)s model=%(model)s cred=%(credential)s attempt=%(attempt)s)"
    )

    def __init__(self, fmt: str | None = None) -> None:
        super().__init__(fmt or self.default_format, datefmt="%H:%M:%S")


_CONFIGURED = False


def setup_logging(level: str = "INFO", *, json_logs: bool = False, force: bool = False) -> None:
    """Configure the root logger once (idempotent unless *force* is set)."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else TextFormatter())
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # httpx is extremely chatty at INFO and could echo URLs/query params.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger (``zkai.<name>``)."""
    return logging.getLogger(name if name.startswith("zkai.") else f"zkai.{name}")
