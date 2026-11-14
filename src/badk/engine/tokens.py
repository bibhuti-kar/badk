"""O(1) token estimator (design 7.4). No tokenizer import (C-7, NG-10)."""

from collections.abc import Sequence

from badk.exec.document import Message


def estimate_tokens(text: str) -> int:
    # ponytail: chars/4 under-counts CJK text; swap the divisor per agent if non-Latin appears.
    return (len(text) + 3) // 4


def estimate_messages(messages: Sequence[Message]) -> int:
    """Design 7.4: sum of content estimates plus 4 per message."""
    return sum(estimate_tokens(m.content) + 4 for m in messages)
