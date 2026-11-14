"""Tool result envelope (design 25.5, PM-36): one shape for every flow tool step."""

import json
from collections.abc import Callable
from typing import Any, Literal, TypedDict

import fastjsonschema

from badk.agents.definition import AgentDef
from badk.engine.envelope import ToolCall
from badk.errors import BadkError, Code, redact
from badk.exec.document import Capsule, CapsuleStatus
from badk.tools.definition import ToolDef
from badk.tools.execute import STDERR_TAIL


class Outcome(TypedDict):
    status: Literal["ok", "error"]
    error_code: str | None
    message: str | None
    body: Any


def ok(body: object) -> Outcome:
    return {"status": "ok", "error_code": None, "message": None, "body": body}


def error(code: Code, message: str, body: object = None) -> Outcome:
    return {"status": "error", "error_code": code.value, "message": message, "body": body}


def _no_const(name: str) -> None:
    raise ValueError(name)  # NaN / Infinity are not JSON


def _parsed(resp: dict[str, Any]) -> dict[str, Any]:
    """An http response with its inner `body` parsed when it is valid JSON."""
    if resp.get("truncated"):  # a cut document is not parsed (as execute._python)
        return resp
    try:
        return resp | {"body": json.loads(resp["body"], parse_constant=_no_const)}
    except (ValueError, TypeError, RecursionError):
        return resp


def _bash(tool: ToolDef, res: dict[str, Any]) -> Outcome:
    if res["exit_code"] == 0:
        return ok(res)
    tail = redact(res["stderr"].strip()[-STDERR_TAIL:])
    return error(Code.TOOL_ERROR, f"{tool.name}: exit {res['exit_code']}: {tail}", res)


def _failed(e: BadkError) -> Outcome:
    if e.code is Code.TOOL_HTTP_ERROR:
        if e.details:
            return error(e.code, e.message, _parsed(e.details[0]))
        return error(Code.TOOL_ERROR, e.message)  # no response (PM-36)
    return error(e.code, e.message)


def run(
    tool: ToolDef,
    owner: AgentDef,
    args: dict[str, Any],
    run_tool: Callable[[AgentDef, ToolCall], dict[str, Any]],
) -> Outcome:
    """Call the owner's StepDeps.run_tool (guard + execute, unchanged) and map the result."""
    try:
        res = run_tool(owner, ToolCall(name=tool.name, args=args))
    except BadkError as e:
        return _failed(e)
    except Exception as e:  # FLOW-4b always gets an Outcome
        return error(Code.TOOL_ERROR, redact(f"{tool.name}: {type(e).__name__}: {e}"))
    if tool.type == "bash":
        return _bash(tool, res)
    return ok(_parsed(res) if tool.type == "http" else res)


def check_output(tool: ToolDef, out: Outcome) -> Outcome:
    """An `ok` body that fails the tool's `output` schema becomes TOOL_ERROR naming the field
    (NC-87). Flow steps only; the classical loop never calls this (NG-48)."""
    if out["status"] != "ok":
        return out
    try:
        tool.validate_output(out["body"])
    except fastjsonschema.JsonSchemaValueException as e:
        return error(Code.TOOL_ERROR, redact(f"{tool.name}: output: {e.message}"), out["body"])
    return out


def of_capsule(cap: Capsule) -> Outcome:
    """Agent step outcome (PM-37): COMPLETED ok; ERROR the capsule code; else `INCOMPLETE`."""
    body = cap.metadata.get("result")
    if cap.status is CapsuleStatus.COMPLETED:
        return ok(body)
    if cap.status is CapsuleStatus.ERROR:
        if cap.error is None:  # an ERROR capsule always has one; never report it as INCOMPLETE
            return error(Code.INTERNAL_ERROR, f"{cap.agent} ended ERROR without an error", body)
        return error(cap.error.code, cap.error.message, body)
    # FLOW-6.5(b): the literal `INCOMPLETE` is not a Code member (PM-37 reads the capsule status)
    msg = f"{cap.agent} ended {cap.status.value}"
    return {"status": "error", "error_code": "INCOMPLETE", "message": msg, "body": body}
