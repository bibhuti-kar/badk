"""`select` step (design 25.7): one selection call, `expand`, then `run_dag` over the picks."""

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from badk.agents.definition import AgentDef
from badk.agents.flow import Step
from badk.coordinator.dag import expand
from badk.coordinator.dispatcher import run_agent
from badk.coordinator.graph_run import DagCtx, run_dag
from badk.coordinator.select import validate_selection, with_catalog
from badk.exec.document import (
    Call,
    Capsule,
    CapsuleStatus,
    DagNode,
    TraceType,
)
from badk.exec.trace import Entry
from badk.flow.budget import check_after_reply
from badk.flow.steps import append_dag, depends_lines
from badk.tools import envelope
from badk.tools.envelope import Outcome

if TYPE_CHECKING:
    from badk.flow.run import Owner


def _ask_model(o: "Owner", s: Step, view: AgentDef, outcomes: Mapping[str, Outcome]) -> Capsule:
    """The one selection call: `view` carries the step's catalog and schema, the history starts
    empty (the spend still accrues on the owner), and the owner's capsule goes back to RUNNING."""
    schema = json.dumps(view.selection_schema)
    lines = [o.first_input, f"CATALOG:\n{view.catalog}"]
    lines += [f"SELECTION_SCHEMA: {schema}", *depends_lines(s, outcomes)]
    deps = replace(o.deps, result_check=validate_selection(view))
    with o.lock:
        start = o.live().model_copy(deep=True)
        start.active_chat_history = []
        cap = run_agent(view, start, "\n".join([*lines, "PHASE: SELECT"]), deps, True)
        back = cap.model_copy(deep=True)
        back.status, back.error, back.active_chat_history = CapsuleStatus.RUNNING, None, []
        o.tl.append(o.key, back, Entry(None))
    return cap


def _picks(
    o: "Owner", s: Step, view: AgentDef, calls: list[Call], outcomes: Mapping[str, Outcome]
) -> list[DagNode]:
    """Expanded nodes re-keyed `<p>/<key>`; the step's DEPENDS_ON lines go in each input once."""
    nodes = expand(calls, view, o.agents, o.ask)
    key, extra = o.prefix + s.id, depends_lines(s, outcomes)
    out: list[DagNode] = []
    for n in nodes:
        after = [f"{key}/{d.key}" for d in nodes if d.agent in n.depends_on]
        text = "\n".join([n.input, *extra]) if extra else n.input
        out.append(
            n.model_copy(
                update={
                    "key": f"{key}/{n.key}", "input": text, "kind": "agent",
                    "after": after, "parent": key,
                }
            )
        )  # fmt: skip
    return out


def _outcome(picks: list[DagNode], done: Mapping[str, Capsule], prefix: str) -> Outcome:
    """First real error (a pick that ran and failed) over a pick that did not run."""
    body = {n.key[len(prefix) :]: envelope.of_capsule(done[n.key]) for n in picks if n.key in done}
    bad = [
        n
        for n in picks
        if (c := done.get(n.key)) is None or c.status is not CapsuleStatus.COMPLETED
    ]
    if not bad:
        return envelope.ok(body)
    ran = [n for n in bad if n.key in done and done[n.key].status is CapsuleStatus.ERROR]
    n = (ran or bad)[0]
    why = body.get(n.key[len(prefix) :]) or {
        "error_code": "INCOMPLETE",
        "message": f"{n.key} did not run",
    }
    return {
        "status": "error",
        "error_code": why["error_code"],
        "message": why["message"],
        "body": body,
    }


def run_select_step(o: "Owner", s: Step, outcomes: Mapping[str, Outcome]) -> Outcome:
    view = with_catalog(
        o.agent.model_copy(update={"agents": list(s.select or ()), "always": []}), o.agents
    )
    cap = _ask_model(o, s, view, outcomes)
    if cap.status is not CapsuleStatus.RUNNING:
        return envelope.of_capsule(cap)
    key = o.prefix + s.id
    calls = [Call.model_validate(c) for c in cap.metadata["result"]["calls"]]
    picks = _picks(o, s, view, calls, outcomes)

    check_after_reply(o, s, picks)  # PM-39: refused picks never join the DAG
    detail = {
        "decision": "select", "task_id": key, "schema": view.selection_schema,
        "calls": [c.model_dump(mode="json") for c in calls],
    }  # fmt: skip
    with o.lock:
        o.tl.append(o.key, o.live(), Entry(TraceType.PLAN, detail), append_dag(picks))
    assert o.agent.max_parallel is not None
    done = run_dag(picks, DagCtx(o.tl, o.agents, o.agent.max_parallel, o.deps_for))
    return _outcome(picks, done, key + "/")
