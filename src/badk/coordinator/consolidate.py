"""Consolidation (design 20.6, WF-6): the coordinator's last step over every node's outcome.
Lives beside the dispatcher (design names `dispatcher.consolidate`; that file is near its limit)."""

from collections.abc import Mapping, Sequence
from typing import Any

from badk.agents.definition import AgentDef
from badk.coordinator.dispatcher import run_agent
from badk.engine.step import StepDeps
from badk.exec.document import Capsule, CapsuleStatus, DagNode, TraceType, dumps, finalise
from badk.exec.trace import Entry, TraceLog


def result_line(key: str, status: str, code: str | None, body: object) -> str:
    """PM-20/PM-37: `<key> status=… code=<code|none> result=<body JSON|none>`."""
    res = "none" if body is None else dumps(body)
    return f"{key} status={status} code={code or 'none'} result={res}"


def consolidation_input(dag: Sequence[DagNode], results: Mapping[str, Capsule]) -> str:
    """One line per dag node, dag order; a node absent from `results` was skipped (PM-20)."""
    lines = []
    for n in dag:
        c = results.get(n.key)
        if c is None:
            lines.append(result_line(n.key, "skipped", None, None))
            continue
        code = c.error.code.value if c.error else None
        lines.append(result_line(n.key, c.status.value, code, c.metadata.get("result")))
    return "PHASE: CONSOLIDATE\nRESULTS:\n" + ("\n".join(lines) or "none")


def answer_of(result: dict[str, Any]) -> str:
    """The sole string field of the result, else its JSON (D-22)."""
    vals = list(result.values())
    return vals[0] if len(vals) == 1 and isinstance(vals[0], str) else dumps(result)


def run_final(coord: AgentDef, cap: Capsule, ask: str, deps: StepDeps) -> tuple[Capsule, Entry]:
    """Step the coordinator on `ask` (no counter reset, no result_check): the capsule and the
    record that end the run: done with the answer, ERROR, or INCOMPLETE (restarts used up)."""
    done = run_agent(coord, cap, ask, deps)
    if done.status != CapsuleStatus.COMPLETED:
        return done, Entry(TraceType.ERROR if done.status == CapsuleStatus.ERROR else None)
    answer = answer_of(done.metadata.get("result") or {})
    done.metadata["answer"] = answer
    return done, Entry(TraceType.DONE, {"answer": answer})


def consolidate(tl: TraceLog, coord: AgentDef, cap: Capsule, ask: str, deps: StepDeps) -> None:
    done, entry = run_final(coord, cap, ask, deps)
    tl.append("coordinator", done, entry, finalise)
