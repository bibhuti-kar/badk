"""Flow steps (design 25.7): tool and agent steps; `select` is in pick.py."""

import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from badk.agents.definition import AgentDef
from badk.agents.flow import Step
from badk.coordinator.dispatcher import run_task
from badk.coordinator.select import pins_json
from badk.engine.envelope import ToolCall
from badk.engine.step import DETAIL_CHARS
from badk.errors import BadkError, Code
from badk.exec.document import DagNode, RunDocument, TraceType, dumps
from badk.exec.trace import Entry, Metrics
from badk.flow.template import render
from badk.tools import envelope
from badk.tools.definition import ToolDef
from badk.tools.envelope import Outcome
from badk.tools.execute import bind_run

if TYPE_CHECKING:
    from badk.flow.run import Owner


def append_dag(nodes: Sequence[DagNode]) -> Callable[[RunDocument], None]:
    """The header edit that adds nodes to the run's dag (PM-41: before their first record)."""

    def add(run: RunDocument) -> None:
        run.dag = [*run.dag, *nodes]

    return add


RETRIED = frozenset({Code.TOOL_ERROR, Code.TOOL_TIMEOUT, Code.TOOL_HTTP_ERROR})  # PM-38


def _entry(type_: TraceType, key: str, detail: dict[str, Any], ms: int = 0) -> Entry:
    m: Metrics = {
        "tokens_in": 0,
        "tokens_out": 0,
        "latency_ms": ms,
        "steps": int(type_ is TraceType.TOOL),
    }
    return Entry(type_, detail, m, key)


def _record(o: "Owner", entry: Entry) -> None:
    """Read-then-append under the owner lock: a stale capsule never overwrites a select call's."""
    with o.lock:
        o.tl.append(o.key, o.live(), entry)


def _call(o: "Owner", key: str, tool: ToolDef, args: dict[str, Any]) -> Outcome:
    crashed: list[bool] = []

    def run_tool(agent: AgentDef, call: ToolCall) -> dict[str, Any]:
        try:
            return o.deps.run_tool(agent, call)
        except BadkError:
            raise
        except Exception:  # not a tool failure: never retried
            crashed.append(True)
            raise

    with bind_run(o.tl.run.run_id, key):  # TOOL-6: the step's key, not the owner's
        out = envelope.run(tool, o.agent, args, run_tool)
        if out["error_code"] in {c.value for c in RETRIED} and not crashed:  # the tool started
            detail = {"task_id": key, "code": out["error_code"], "message": out["message"]}
            _record(o, _entry(TraceType.RETRY, key, detail))
            out = envelope.run(tool, o.agent, args, o.deps.run_tool)
    return out


def run_tool_step(o: "Owner", s: Step, outcomes: Mapping[str, Outcome]) -> Outcome:
    """Render, check args, run through the envelope, write one `tool` record (no retry on a
    render or args error: nothing started)."""
    key, t0 = o.prefix + s.id, time.monotonic()
    tool = o.deps.tools(o.agent).get(s.tool or "")
    args: dict[str, Any] = {}
    try:
        if tool is None:
            raise BadkError(Code.TOOL_ERROR, f"{s.id}: tool {s.tool!r} is not available")
        args = render(s.args, o.ask, outcomes, {"run_id": o.tl.run.run_id, "task_id": key})
        tool.validate_args(args)
        out = envelope.check_output(tool, _call(o, key, tool, args))  # once, never retried
    except BadkError as e:
        out = envelope.error(e.code, e.message)
    except ValueError as e:  # JsonSchemaValueException
        out = envelope.error(Code.TOOL_ERROR, f"{s.id}: args: {e}")
    ms = int((time.monotonic() - t0) * 1000)
    detail = {
        "task_id": key, "name": s.tool, "args": args, "ok": out["status"] == "ok",
        "status": out["status"], "error_code": out["error_code"], "message": out["message"],
        "result": dumps(out["body"])[:DETAIL_CHARS],
    }  # fmt: skip
    _record(o, _entry(TraceType.TOOL, key, detail, ms))
    return out


StepRunner = Callable[["Owner", Step, Mapping[str, Outcome]], Outcome]


def step_input(o: "Owner", s: Step, outcomes: Mapping[str, Outcome]) -> str:
    """WF-5 first input over the direct `after` parents; a skipped parent reads `none` (#18)."""
    lines = [f"TASK: {o.ask}"] + ([f"PINS: {pins_json(o.pins)}"] if o.pins else [])
    return "\n".join(lines + depends_lines(s, outcomes)) + "\n"


def depends_lines(s: Step, outcomes: Mapping[str, Outcome]) -> list[str]:
    return [
        f"DEPENDS_ON {a} RESULT: {dumps(outcomes[a]) if a in outcomes else 'none'}" for a in s.after
    ]


def run_agent_step(o: "Owner", s: Step, outcomes: Mapping[str, Outcome]) -> Outcome:
    """The named sub-agent through `run_task`; `depends_on` is not expanded (PM-37)."""
    key = o.prefix + s.id
    agent = o.agents[s.agent or ""]
    node = next(n for n in o.tl.run.dag if n.key == key)  # the node written with the plan
    return envelope.of_capsule(run_task(node, agent, step_input(o, s, outcomes), o.tl, o.deps_for))


KINDS: dict[str, StepRunner] = {"tool": run_tool_step, "agent": run_agent_step}
