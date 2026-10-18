"""Logs estructurados en JSON con ocultación de secretos."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

_SECRET_PATTERNS = [
    re.compile(r"(api[-_]?key=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(://[^:/\s]+:)[^@\s]+(@)"),  # usuario:contraseña@ en URLs
    re.compile(r"(bot)\d+:[A-Za-z0-9_-]{20,}"),  # token de bot de Telegram
]


def redact(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(
            lambda m: m.group(1) + "***" + (m.group(2) if m.lastindex == 2 else ""), value
        )
    return value


def _redact_processor(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    return {k: redact(v) if isinstance(v, str) else v for k, v in event.items()}


class RedactingFilter(logging.Filter):
    """Oculta secretos también en los logs de librerías que usan ``logging`` estándar."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def configure(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())
    # httpx registra cada petición con su URL completa, que puede llevar la clave.
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
