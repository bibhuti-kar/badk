from dataclasses import dataclass

from badk.errors import BadkError, Code, redact


@dataclass(frozen=True)
class ModelReply:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


def fail(provider: str, code: Code, why: str) -> BadkError:
    return BadkError(code, redact(f"{provider} provider: {why}"))


def count(value: object, fallback: int) -> int:
    """A provider-reported token count, else the local estimate (bool is not a count)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback
