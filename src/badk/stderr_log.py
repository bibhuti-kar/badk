"""Logging for SDK-path modules (SDK-4.1). When badk configured logging itself (CLI, serve,
viewer, writer) this is the normal global structlog logger. Embedded in a host program it is a
private stderr logger with badk's own fixed processors: no global config is read or written, so
a host's stdlib-style processors cannot break it and nothing reaches stdout."""

import sys
from contextvars import ContextVar
from typing import cast

import structlog
from structlog.typing import FilteringBoundLogger

from badk import logging_setup


class _Stderr:
    """Resolves `sys.stderr` at write time, so a host that swaps it is followed."""

    def write(self, s: str) -> int:
        return sys.stderr.write(s)

    def flush(self) -> None:
        sys.stderr.flush()


# method name -> rank, as structlog's add_log_level names them (msg=info, exception/failure=error)
_RANK = {
    "debug": 10,
    "info": 20,
    "msg": 20,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "exception": 40,
    "failure": 40,
    "critical": 50,
    "fatal": 50,
}
# per run context (threads and tasks copy it), so concurrent runs with different LOG_LEVEL differ
_level: ContextVar[str] = ContextVar("badk_sdk_level", default="warning")  # design 20.9 default


def set_level(level: str) -> None:
    """Set at the start of `badk.run` from the loaded Settings; the filter never reads files."""
    _level.set(level.lower())


def _level_filter(
    _l: object, method: str, ed: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """SDK-4.1: drop events below the level `badk.run` resolved."""
    if _RANK.get(method, 20) < _RANK[_level.get()]:
        raise structlog.DropEvent
    return ed


_STDERR = _Stderr()
_SDK_LOG = structlog.wrap_logger(
    structlog.PrintLogger(_STDERR),  # type: ignore[arg-type]
    processors=[
        _level_filter,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
)


class _Log:
    """Picks the target per call: configure_logging may run after this module is imported."""

    def __getattr__(self, name: str) -> object:
        target = structlog.get_logger() if logging_setup.CONFIGURED else _SDK_LOG
        return getattr(target, name)


def get_logger() -> FilteringBoundLogger:
    return cast(FilteringBoundLogger, _Log())
