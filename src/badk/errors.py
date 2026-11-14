"""Closed error-code set and the one exception type (design 13)."""

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Any


class Code(StrEnum):
    AGENT_FRONTMATTER_TOO_LARGE = "AGENT_FRONTMATTER_TOO_LARGE"
    AGENT_NAME_INVALID = "AGENT_NAME_INVALID"
    TIME_BUDGET_INSUFFICIENT = "TIME_BUDGET_INSUFFICIENT"
    AGENT_FIELD_NOT_ALLOWED = "AGENT_FIELD_NOT_ALLOWED"
    PROVIDER_NOT_ALLOWED = "PROVIDER_NOT_ALLOWED"
    TOOL_TYPE_INVALID = "TOOL_TYPE_INVALID"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INPUT_FILE_NOT_FOUND = "INPUT_FILE_NOT_FOUND"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    WORKSPACE_DENIED = "WORKSPACE_DENIED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_ERROR = "TOOL_ERROR"
    TOOL_HTTP_ERROR = "TOOL_HTTP_ERROR"
    QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"
    VIEWER_UNAVAILABLE = "VIEWER_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class BadkError(Exception):
    """Raised for bad input or infrastructure faults: code, message, details, run_id, answer."""

    def __init__(
        self, code: Code, message: str, details: list[dict[str, Any]] | None = None
    ) -> None:
        message = redact(message)  # choke point: no BadkError can carry a secret or URL password
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or []
        self.run_id: str | None = None
        self.answer: str | None = None  # final text of a run that failed mid-run (SDK raises)

    def envelope(self) -> dict[str, Any]:
        return {
            "error": {"code": self.code.value, "message": self.message, "details": self.details}
        }


class AckFailed(BadkError):
    """`badk serve` could not ack the run request after "run created" (D-3)."""

    def __init__(self, message: str) -> None:
        super().__init__(Code.QUEUE_UNAVAILABLE, message)


class PersistenceError(BadkError):
    """A persistence sink failed inside an emit. Never retried, never a task ERROR (RES-1)."""


_SECRETS: set[str] = set()
_URL_CREDS = re.compile(r"(?<=://)\S*@")  # all userinfo, up to the last `@` before the host
MIN_SECRET = 8  # shorter values (placeholder keys like `ollama`) would garble messages
_MIN_PART = 6  # a cut edge keeps at most 5 chars of a secret visible


def register_secret(value: str) -> None:
    """Remember a secret (the LLM key, a tool `env:` value) so `redact` strips it; never
    forgotten, so a later start never unmasks an earlier run's values."""
    if len(value) >= MIN_SECRET:
        _SECRETS.add(value)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Strip registered/given secrets and `user:pass@` URL credentials from error text."""
    for s in sorted({*_SECRETS, *secrets}, key=len, reverse=True):
        text = text.replace(s, "***") if s else text
    return _URL_CREDS.sub("***@", text)


def redact_cut(text: str) -> str:
    """`redact` for a text that a byte cap cut at its end: the first part of a secret left
    there is masked too. Only for cut text (a whole text could end like a secret starts);
    cut your own text only after `redact`."""
    text = redact(text)
    for s in _SECRETS.copy():
        tail = next((k for k in range(len(s) - 1, _MIN_PART - 1, -1) if text.endswith(s[:k])), 0)
        text = text[: len(text) - tail] + "***" * bool(tail)
    return text
