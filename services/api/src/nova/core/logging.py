"""Structured logging.

structlog is configured once at startup. Every log line carries the request ID
bound by :mod:`nova.middleware.request_context`, so a single request can be
traced across services without threading a logger through call signatures.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from nova.core.config import ObservabilitySettings


def configure_logging(settings: ObservabilitySettings) -> None:
    """Install the structlog + stdlib logging configuration.

    Safe to call more than once; the last call wins.
    """
    level = getattr(logging, settings.log_level)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    # Configure stdlib logging first: structlog renders to a string and hands
    # it to a stdlib logger, so the handler must already exist.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # A stdlib factory, not PrintLogger: the stdlib processors above read
        # `logger.name`, which only a real logging.Logger has. It also puts
        # NOVA's lines and uvicorn's through one handler.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    for noisy in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for ``name``."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
