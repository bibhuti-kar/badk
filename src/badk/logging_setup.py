"""Shared structlog setup (design 6.2): JSON lines at LOG_LEVEL."""

import logging
from typing import TextIO

import structlog

CONFIGURED = False  # set once badk owns logging (CLI, serve, viewer, writer); the SDK never sets it


def configure_logging(level: str, file: TextIO | None = None) -> None:
    global CONFIGURED  # noqa: PLW0603 - one process-wide fact
    CONFIGURED = True
    factory = structlog.PrintLoggerFactory(file=file)  # file None => stdout default
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=factory,
    )
