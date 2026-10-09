"""structlog con redaccion de PII y secretos."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_KEYLIKE = re.compile(r"\b(sk-[A-Za-z0-9_-]{10,}|AIza[A-Za-z0-9_-]{20,}|AC[a-f0-9]{32})\b")
_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "ciphertext",
    "phone",
    "email",
    "body",
)


def redact_text(value: str) -> str:
    value = _EMAIL.sub("[email]", value)
    value = _BEARER.sub(r"\1[redacted]", value)
    value = _KEYLIKE.sub("[redacted]", value)
    return _PHONE.sub("[tel]", value)


def _redact_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(s in lowered for s in _SENSITIVE_KEYS):
        return "[redacted]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(key + "_item", v) if isinstance(v, dict) else v for v in value]
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key in {"event", "level", "timestamp", "logger"}:
            if key == "event" and isinstance(event_dict[key], str):
                event_dict[key] = redact_text(event_dict[key])
            continue
        event_dict[key] = _redact_value(key, event_dict[key])
    return event_dict


_configured = False


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    global _configured
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper(), force=True)
    renderer: Any = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=False,
    )
    _configured = True


def get_logger(name: str | None = None) -> Any:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
