"""Selection (design 20.4, WF-3): the coordinator's first step picks `calls` from a catalog."""

import json
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from typing import Any

import fastjsonschema

from badk.agents.definition import AgentDef
from badk.coordinator.input import Pin
from badk.errors import BadkError, Code
from badk.exec.document import RunInput, dumps


def catalog_text(coord: AgentDef, agents: Mapping[str, AgentDef]) -> str:
    """One line per catalog agent, in `agents` order: byte-stable across runs (PM-22)."""
    if missing := [n for n in coord.agents if n not in agents]:
        raise BadkError(Code.VALIDATION_ERROR, f"{coord.name}: agents not found: {missing}")
    return "\n".join(
        f"{n}: {agents[n].use_when} (depends_on: {', '.join(agents[n].depends_on) or 'none'})"
        for n in coord.agents
    )


def selection_schema(names: Sequence[str]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "object",
        "required": ["agent", "input"],
        "additionalProperties": False,
        "properties": {"agent": {"enum": list(names)}, "input": {"type": "string"}},
    }
    calls: dict[str, Any] = {"type": "array", "items": item}
    if not names:  # an empty enum is not valid JSON Schema: no call can name an agent
        del item["properties"]["agent"]["enum"]
        calls["maxItems"] = 0
    return {
        "type": "object",
        "required": ["calls"],
        "additionalProperties": False,
        "properties": {"calls": calls},
    }


def with_catalog(coord: AgentDef, agents: Mapping[str, AgentDef]) -> AgentDef:
    return coord.model_copy(
        update={
            "catalog": catalog_text(coord, agents),
            "selection_schema": selection_schema(coord.agents),
        }
    )


def pins_json(pins: Sequence[Pin]) -> str:
    """Resolved pins as the model sees them (selection and flow agent steps, 25.15 #22)."""
    return dumps([{"kind": p.kind, "ref": p.ref, "content": p.content} for p in pins])


def request_header(input_: RunInput, pins: Sequence[Pin]) -> str:
    """The opening of every model input of a run's owner (selection, flow select, final)."""
    return f"REQUEST: {input_.raw}\nPINS: {pins_json(pins)}"


def selection_input(input_: RunInput, pins: list[Pin]) -> str:
    return request_header(input_, pins) + "\nPHASE: SELECT"


@lru_cache(maxsize=32)
def _compile(schema_json: str) -> Callable[[Any], Any]:
    return fastjsonschema.compile(json.loads(schema_json))


def validate_selection(coord: AgentDef) -> Callable[[dict[str, Any]], object]:
    """The `StepDeps.result_check` of the select phase; raises OUTPUT_INVALID."""
    check = _compile(json.dumps(coord.selection_schema, sort_keys=True))

    def validate(result: dict[str, Any]) -> None:
        try:
            check(result)
        except fastjsonschema.JsonSchemaValueException as e:
            msg = f"selection does not match schema: {e.message}"
            raise BadkError(Code.OUTPUT_INVALID, msg) from e

    return validate
