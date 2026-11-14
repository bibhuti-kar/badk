"""Dispatcher (design 20.5): `run_dag` runs the graph, one thread per running node, at most
`max_parallel` at once; `run_agent`/`run_task` drive one node to a terminal capsule."""

from collections.abc import Callable

from badk.agents.definition import AgentDef
from badk.engine.restart import from_journal
from badk.engine.step import StepDeps, step
from badk.errors import BadkError, Code, PersistenceError
from badk.exec.document import (
    Capsule,
    CapsuleStatus,
    DagNode,
    TraceType,
    fail_capsule,
    new_capsule,
)
from badk.exec.trace import Entry, TraceLog
from badk.proc import cancelled
from badk.stderr_log import get_logger

log = get_logger()

DepsFor = Callable[[str, AgentDef], StepDeps]  # (capsule key, effective agent) -> deps


def run_agent(
    agent: AgentDef,
    capsule: Capsule,
    first_input: str,
    deps: StepDeps,
    stop_when_running: bool = False,
) -> Capsule:
    """Step until a terminal capsule; a RUNNING step is followed by input "CONTINUE". A budget
    halt restarts from the journal (RES-2) and re-sends `first_input`, until all `max_tries`
    tries (the first included) are used up (INCOMPLETE). Outcomes: COMPLETED, ERROR, INCOMPLETE."""
    if cancelled():
        return _cancel(capsule)
    c = step(agent, capsule, first_input, deps)
    while c.status in (CapsuleStatus.RUNNING, CapsuleStatus.BUDGET_EXCEEDED):
        if cancelled():  # SDK-4: the run was interrupted: no new model call
            return _cancel(c)
        if c.status == CapsuleStatus.BUDGET_EXCEEDED:
            c = from_journal(agent, c, deps)
            if c.status == CapsuleStatus.RUNNING:
                c = step(agent, c, first_input, deps)
        elif stop_when_running:  # WF-3: a valid selection leaves the capsule RUNNING
            return c
        else:
            c = step(agent, c, "CONTINUE", deps)
    return c


def _cancel(c: Capsule) -> Capsule:
    c = c.model_copy(deep=True)
    c.status = CapsuleStatus.INCOMPLETE
    return c


def run_task(
    node: DagNode,
    agent: AgentDef,
    first_input: str,
    tl: TraceLog,
    deps_for: DepsFor,
) -> Capsule:
    """One sub-agent to a terminal capsule. A coded deps error keeps its code, any other crash
    becomes ERROR INTERNAL_ERROR (emitted); a PersistenceError always propagates."""
    key = node.key  # budgets come from the agent file only (WF-5.4)
    cap = new_capsule(agent.name, key)
    if agent.route is not None:  # a hand-built AgentDef has none: the viewer shows a dash
        cap.metadata["route"] = agent.route.model_dump()
    try:
        deps = deps_for(key, agent)
    except Exception as e:  # a BadkError (e.g. a tool file broke after startup) keeps its code
        return _crash(tl, key, cap, e)
    try:
        if agent.flow:  # design 25.11: a sub-agent's flow runs in place of its model loop
            from badk.flow.run import Nested, run_owner_flow  # noqa: PLC0415 - flow imports this

            cap = run_owner_flow(Nested(node, agent, cap, first_input), tl, deps_for, deps)
        else:
            cap = run_agent(agent, cap, first_input, deps)
    except PersistenceError:
        raise
    except Exception as e:  # coded failure outside the step's own handling: this task's ERROR
        return _crash(tl, key, cap, e)
    kind = {CapsuleStatus.COMPLETED: TraceType.DONE, CapsuleStatus.ERROR: TraceType.ERROR}
    if cap.status in kind:  # INCOMPLETE: from_journal already emitted it
        tl.append(key, cap, Entry(kind[cap.status]))
    return cap


def _crash(tl: TraceLog, key: str, fresh: Capsule, e: Exception) -> Capsule:
    """ERROR the task on its last emitted capsule (keeps spend/history), else the fresh one."""
    code = e.code if isinstance(e, BadkError) else Code.INTERNAL_ERROR
    message = e.message if isinstance(e, BadkError) else f"{type(e).__name__}: {e}"
    cap = tl.run.agents.get(key, fresh).model_copy(deep=True)
    fail_capsule(cap, code, message)
    detail = e.details[0] if isinstance(e, BadkError) and e.details else {}
    tl.append(key, cap, Entry(TraceType.ERROR, detail))
    return cap
