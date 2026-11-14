"""Structured output envelope (design 7.5, D-21). Pure: no router/tools/persistence imports."""

import re
from collections.abc import Callable
from typing import Any, Final

from fastjsonschema import JsonSchemaValueException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from badk.errors import BadkError, Code

_FENCE: Final = re.compile(r"```[A-Za-z0-9]*\s*(.*?)\s*```", re.DOTALL)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    args: dict[str, Any]


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    done: bool
    tool_calls: list[ToolCall]
    result: dict[str, Any]
    notes: str = Field("", max_length=300)  # optional: a model may drop it on a long reply


def parse(
    text: str,
    schema_validator: Callable[[Any], Any],
    result_check: Callable[[dict[str, Any]], object] | None = None,
) -> Envelope:
    """Parse a raw model reply; `result` is schema-checked only when done (D-21)."""
    m = None if text.lstrip().startswith("{") else _FENCE.search(text)
    try:
        env = Envelope.model_validate_json(m.group(1) if m else text)
    except ValidationError as e:
        why = "; ".join(
            f"{'.'.join(map(str, x['loc'])) or 'reply'}: {x['msg']}" for x in e.errors()
        )
        raise BadkError(Code.OUTPUT_INVALID, f"bad envelope: {why}") from e
    if result_check and not env.tool_calls:  # a checked phase (selection, WF-3): done is false
        if env.done:
            raise BadkError(Code.OUTPUT_INVALID, "reply must have done false in this phase")
        result_check(env.result)  # raises its own code
    elif env.done and not env.tool_calls:  # tool_calls win over done (design 7.2)
        try:
            schema_validator(env.result)
        except JsonSchemaValueException as e:
            raise BadkError(
                Code.OUTPUT_INVALID, f"result does not match schema: {e.message}"
            ) from e
    return env
