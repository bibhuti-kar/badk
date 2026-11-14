"""badk: stateless, budget-bounded, cache-aligned multi-agent development kit."""

from badk.errors import BadkError
from badk.sdk import RunResult, run

__version__ = "0.1.0"
__all__ = ["BadkError", "RunResult", "run"]
