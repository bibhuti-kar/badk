"""Run a coordinator's flow (design 25.1). Steps run through `schedule.run_steps`."""

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from badk.agents.definition import AgentDef
from badk.agents.flow import Step
from badk.coordinator.dispatcher import DepsFor
from badk.coordinator.input import Pin, parse_slash, resolve_pins
from badk.coordinator.run import RunOptions, deps_factory, make_deps
from badk.coordinator.select import request_header
from badk.engine.step import StepDeps
from badk.errors import BadkError, Code, PersistenceError
from badk.exec.document import (
    Capsule,
    DagNode,
    RunDocument,
    TraceType,
    error_capsule,
    finalise,
)
from badk.exec.trace import Entry, TraceLog
from badk.flow.budget import check_start
from badk.flow.env import env
from badk.flow.final import finish, finish_nested
from badk.flow.pick import run_select_step
from badk.flow.schedule import FlowRefused, RunOne, StepEvents, run_steps
from badk.flow.steps import KINDS as _BASE
from badk.flow.steps import append_dag
from badk.flow.walk import model_agents
from badk.router import preflight
from badk.tools.envelope import Outcome

KINDS = {**_BASE, "select": run_select_step}


@dataclass
class Owner:
    key: str  # trace and capsule key: "coordinator"
    prefix: str  # node key prefix of its steps: ""
    agent: AgentDef
    ask: str  # {{ask}}
    deps: StepDeps
    tl: TraceLog
    agents: Mapping[str, AgentDef]
    deps_for: DepsFor
    first_input: str  # REQUEST + PINS: opens the select and final step's opening text (25.10)
    pins: Sequence[Pin] = ()  # resolved @file/@url pins, shown to agent steps (25.15 #22)
    lock: threading.Lock = field(default_factory=threading.Lock)  # model calls + read-then-append
    seed: Capsule | None = None  # a nested owner's capsule until its first record
    started: float = 0.0  # the flow's try start (25.9); select calls may reset the capsule's

    def live(self) -> Capsule:
        c = self.tl.run.agents.get(self.key) or self.seed
        assert c is not None
        return c


def _nodes(o: Owner) -> list[DagNode]:
    return [
        DagNode(
            key=o.prefix + s.id, agent=s.agent or o.agent.name, source="flow", depends_on=[],
            input=o.ask if s.kind == "agent" else json.dumps(s.args), kind=s.kind, tool=s.tool,
            after=[o.prefix + a for a in s.after], parent=o.key if o.prefix else None,
        )
        for s in o.agent.flow
    ]  # fmt: skip


def _plan(
    o: Owner, detail: dict[str, object], edit: Callable[[RunDocument], None],
    cap: Capsule | None = None,
) -> None:  # fmt: skip
    with o.lock:  # read-then-append: a stale capsule never overwrites a select call's
        o.tl.append(o.key, cap or o.live(), Entry(TraceType.PLAN, detail), edit)


def _stamp(o: Owner) -> Capsule:
    """A copy of the owner capsule carrying the try start; the plan record stores it (25.9)."""
    cap, now = o.live().model_copy(deep=True), o.deps.clock()
    cap.metadata.setdefault("try_started", now)
    cap.time_started = cap.time_started or now
    o.started = cap.metadata["try_started"]
    return cap


def _state(key: str, state: str) -> Callable[[RunDocument], None]:
    def edit(run: RunDocument) -> None:
        for n in run.dag:
            if n.key == key:
                n.state = state  # type: ignore[assignment]

    return edit


def _events(o: Owner) -> StepEvents:
    def dispatch(s: Step) -> None:
        key = o.prefix + s.id
        _plan(o, {"decision": "dispatch", "task_id": key}, _state(key, "ran"))

    def skip(s: Step, cause: str) -> None:
        key = o.prefix + s.id
        _plan(o, {"decision": "skip", "task_id": key, "cause": cause}, _state(key, "skipped"))

    return StepEvents(dispatch, skip)


def _runner(o: Owner) -> RunOne:
    return lambda s, outcomes: KINDS[s.kind](o, s, outcomes)


def run_flow(o: Owner) -> tuple[dict[str, Outcome], set[str]]:
    """Nodes first (one `plan` record, PM-41), then the scheduled steps: (outcomes, skipped)."""
    nodes = _nodes(o)
    detail: dict[str, object] = {"decision": "flow"}
    if o.prefix:  # a nested owner: the dag is the run's, its nodes are named by the owner key
        detail["task_id"] = o.key
    else:
        detail["dag"] = [n.model_dump(mode="json") for n in nodes]
    _plan(o, detail, append_dag(nodes), _stamp(o))
    ev = _events(o)
    try:
        check_start(o)  # PM-39: before the first dispatch
    except FlowRefused:
        for s in o.agent.flow:
            ev.skip(s, "refused")
        raise
    cap = o.agent.max_parallel or 1  # the loader defaults a flow agent to 1
    return run_steps(o.agent.flow, _runner(o), cap, ev)


@dataclass(frozen=True)
class Nested:
    """One sub-agent flow handed over by `run_task` (design 25.11)."""

    node: DagNode
    agent: AgentDef
    cap: Capsule
    first_input: str


def run_owner_flow(n: Nested, tl: TraceLog, deps_for: DepsFor, deps: StepDeps) -> Capsule:
    """A sub-agent's flow inside `run_task`: nodes `<key>/<id>`, the final step returns the
    capsule (run_task writes its done/error record). The time budget starts here (25.9)."""
    n.cap.time_started = deps.clock()
    n.cap.metadata["try_started"] = n.cap.time_started
    o = Owner(
        key=n.node.key, prefix=n.node.key + "/", agent=n.agent, ask=n.node.input, deps=deps,
        tl=tl, agents=env().agents, deps_for=deps_for, first_input=n.first_input, seed=n.cap,
    )  # fmt: skip
    return finish_nested(o, *run_flow(o))


def _ask(run: RunDocument) -> str:
    """`run.input.raw` without the leading `/name` token (design 25.11)."""
    raw = run.input.raw.lstrip("﻿").lstrip()
    slash = parse_slash(raw)
    return raw[len(slash) :].lstrip() if slash else raw


def run_flow_coordinator(tl: TraceLog, opts: RunOptions, cwd: Path | None) -> None:
    """The `run_coordinator` branch for a coordinator with a `flow`: no selection call."""
    coord = opts.coordinator
    try:
        preflight(model_agents(coord, opts.agents))  # RT-2.2: only agents that make model calls
        pins = resolve_pins(tl.run.input.pins, coord, cwd or Path.cwd())  # denied ends the run
        deps = make_deps("coordinator", tl, opts.settings, None, coord, cwd, opts.tools)
    except BadkError as e:
        cap = error_capsule(tl.run, e.code, e.message)
        tl.append("coordinator", cap, Entry(TraceType.ERROR), finalise)
        return
    try:
        dfor = deps_factory(tl, opts, cwd)
        first = request_header(tl.run.input, pins)
        o = Owner(
            key="coordinator", prefix="", agent=coord, ask=_ask(tl.run), deps=deps, tl=tl,
            agents=opts.agents, deps_for=dfor, first_input=first, pins=pins,
        )  # fmt: skip
        finish(o, *run_flow(o))
    except PersistenceError:
        raise
    except FlowRefused as e:  # PM-27 shape: one error record, the run ends ERROR
        cap = error_capsule(tl.run, e.code, e.message)
        tl.append(
            "coordinator", cap, Entry(TraceType.ERROR, e.details[0] if e.details else {}), finalise
        )
    except Exception as e:  # a crash ends the run ERROR, never RUNNING (as the classical path)
        code = e.code if isinstance(e, BadkError) else Code.INTERNAL_ERROR
        msg = e.message if isinstance(e, BadkError) else f"{type(e).__name__}: {e}"
        tl.append("coordinator", error_capsule(tl.run, code, msg), Entry(TraceType.ERROR), finalise)
