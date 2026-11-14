"""Run entry (design 10.1): `run_coordinator` steps the coordinator definition (COORD-1)."""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial, wraps
from pathlib import Path
from typing import Any

from badk.agents.definition import AgentDef
from badk.agents.route import with_routes
from badk.config import Settings
from badk.coordinator.consolidate import consolidate, consolidation_input
from badk.coordinator.dag import expand, time_bound
from badk.coordinator.dispatcher import DepsFor, run_agent
from badk.coordinator.graph_run import DagCtx, run_dag
from badk.coordinator.input import coordinator_for, parse_slash, pin_strings, resolve_pins
from badk.coordinator.select import selection_input, validate_selection, with_catalog
from badk.engine.envelope import ToolCall
from badk.engine.step import StepDeps
from badk.errors import BadkError, Code
from badk.exec.document import (
    Call,
    Capsule,
    CapsuleStatus,
    DagNode,
    RunDocument,
    TraceType,
    create_run,
    error_capsule,
    fail_capsule,
    finalise,
)
from badk.exec.trace import Entry, Metrics, Sink, TraceLog
from badk.flow.env import FLOW_ENV, bind_env
from badk.persistence.events import EventPublisher
from badk.proc import RunInterrupted, cancelled
from badk.progress import ProgressLog
from badk.router import complete, preflight
from badk.stderr_log import get_logger
from badk.tools.definition import tools_dir
from badk.tools.execute import bind_run, permitted_tools, run_tool


def _cancel_point[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    """Design 20.9: after an interrupt a model or tool call raises instead of going out."""

    @wraps(f)
    def wrapped(*a: P.args, **k: P.kwargs) -> R:
        if cancelled():
            raise RunInterrupted
        return f(*a, **k)

    return wrapped


def make_sinks(settings: Settings, headless: bool, extra: Sequence[Sink] = ()) -> list[Sink]:
    """Design 10.1/11.1: persistence sink first, then ProgressLog if headless, then extra."""
    sinks: list[Sink] = [EventPublisher(settings)]
    if headless:
        sinks.append(ProgressLog(settings.log_level))
    return [*sinks, *extra]


@dataclass(frozen=True)
class RunOptions:
    """What a run needs besides its document and sinks; built only by `new_run`."""

    coordinator: AgentDef
    agents: Mapping[str, AgentDef]
    settings: Settings
    tools: Path | None = None  # tool files dir; None = `<run cwd>/tools`


def new_run(
    text: str,
    agents: Mapping[str, AgentDef],
    settings: Settings,
    run_id: str | None = None,
    workflow: str | None = None,
) -> tuple[RunDocument, RunOptions]:
    """WF-2: the one place a run is created. `workflow` ("research") is the SDK's spelling of a
    leading `/research`; the ask is then `text`. Raises AGENT_NOT_FOUND before any run exists."""
    if workflow is None:
        slash = parse_slash(text)
    else:  # same document as the slash spelling; a malformed name is never in `agents`
        slash, text = f"/{workflow}", f"/{workflow} {text}"
    name = coordinator_for(slash, agents)
    agents = with_routes(agents, agents[name])  # RT-8b: sub-agents without a route inherit
    coord = agents[name]
    run = create_run(text, slash, run_id, pin_strings(text), name)
    if coord.route is not None:
        run.agents["coordinator"].metadata["route"] = coord.route.model_dump()
    return run, RunOptions(coord, agents, settings)


def make_deps(  # noqa: PLR0913 - one binding point; cwd is the run's root (TOOL-3)
    agent_key: str,
    tl: TraceLog,
    settings: Settings,
    result_check: Callable[[dict[str, Any]], object] | None = None,
    agent: AgentDef | None = None,
    cwd: Path | None = None,
    tools_from: Path | None = None,
) -> StepDeps:
    """Design 10.1: bind the router to `settings` and emit to the agent's key. `agent`'s permitted
    tools load once here: the payload and the executor share that snapshot (stable TOOLS block)."""
    tools = permitted_tools(agent, tools_from or tools_dir(cwd)) if agent else {}

    def run_bound(agent_: AgentDef, call: ToolCall) -> dict[str, Any]:
        with bind_run(tl.run.run_id, agent_key):  # TOOL-6: BADK_RUN_ID / BADK_TASK_ID
            return run_tool(tools, agent_, call, cwd=cwd)

    def emit(
        cap: Capsule, type_: TraceType | None, detail: dict[str, Any], m: Metrics
    ) -> int | None:
        return tl.append(agent_key, cap, Entry(type_, detail, m))

    return StepDeps(
        _cancel_point(partial(complete, settings=settings)),
        emit,
        time.time,
        result_check,
        _cancel_point(run_bound),
        lambda _a: tools,
    )


def deps_factory(tl: TraceLog, opts: RunOptions, cwd: Path | None) -> DepsFor:
    """`deps_for` of `run_task`/`run_dag`: one agent's deps under its own trace key."""

    def deps_for(key: str, agent: AgentDef) -> StepDeps:
        return make_deps(key, tl, opts.settings, None, agent, cwd, opts.tools)

    return deps_for


def run_coordinator(tl: TraceLog, opts: RunOptions, cwd: Path | None = None) -> None:
    """Resolve pins (design 10.2; a denied pin is a coordinator ERROR on the started run), then
    SELECT (WF-3): the coordinator's first step picks `calls` from its catalog and stays RUNNING.
    The calls become a graph run by `run_dag`; a CONSOLIDATE step then completes the run.
    Persistence errors propagate; model and selection errors end the run in ERROR."""
    token = bind_env(opts)  # every run: a flow sub-agent in a classical catalog reads it (FLOW-7.3)
    try:
        _run_coordinator(tl, opts, cwd)
    finally:
        FLOW_ENV.reset(token)


def _run_coordinator(tl: TraceLog, opts: RunOptions, cwd: Path | None) -> None:
    run = tl.run
    coord = opts.coordinator
    if coord.flow:  # design 25.1: a flow replaces the selection step
        from badk.flow.run import run_flow_coordinator  # noqa: PLC0415 - imports this module

        return run_flow_coordinator(tl, opts, cwd)
    try:
        if not coord.selection_schema:  # an AgentDef built outside load_agents
            coord = with_catalog(coord, opts.agents)
        preflight({**opts.agents, coord.name: coord})  # RT-2.2: before any step
        pins = resolve_pins(run.input.pins, coord, cwd or Path.cwd())
        deps = make_deps(
            "coordinator", tl, opts.settings, validate_selection(coord), coord, cwd, opts.tools
        )
    except BadkError as e:  # also a tool file that broke after startup validation
        _fail(tl, e.code, e.message)
        return
    first = selection_input(run.input, pins)
    cap = run_agent(coord, run.agents["coordinator"], first, deps, stop_when_running=True)
    if cap.status == CapsuleStatus.RUNNING:
        _dispatch(tl, cap, replace(opts, coordinator=coord), cwd)
    elif cap.status == CapsuleStatus.ERROR:
        tl.append("coordinator", cap, Entry(TraceType.ERROR), finalise)
    else:  # INCOMPLETE (restarts used up, RES-2): capsule-only record, run ends INCOMPLETE
        tl.append("coordinator", cap, Entry(None), finalise)


def _dispatch(tl: TraceLog, cap: Capsule, opts: RunOptions, cwd: Path | None) -> None:
    """Run the selected calls, then consolidate over every node's outcome (WF-6); the run's
    terminal status comes from `run_status` in the last header edit (RES-3)."""
    calls = [Call.model_validate(c) for c in cap.metadata["result"]["calls"]]
    dag = expand(calls, opts.coordinator, opts.agents, tl.run.input.raw)
    record_graph(tl, calls, dag)
    assert opts.coordinator.max_parallel is not None
    bound = time_bound(dag, opts.agents, opts.coordinator.max_parallel)
    left = opts.coordinator.time_budget_sec - (time.time() - cap.metadata["try_started"])
    reserve = opts.settings.provider_timeout_sec  # consolidation is one more coordinator call
    if left <= bound + reserve:  # WF-12: Graham's bound; remaining time of the current try
        _refuse_time(tl, cap, bound, reserve, left)
        return

    ctx = DagCtx(tl, opts.agents, opts.coordinator.max_parallel, deps_factory(tl, opts, cwd))
    results = run_dag(dag, ctx)
    deps = make_deps("coordinator", tl, opts.settings, None, opts.coordinator, cwd, opts.tools)
    consolidate(tl, opts.coordinator, cap, consolidation_input(dag, results), deps)


def _refuse_time(tl: TraceLog, cap: Capsule, bound: float, reserve: int, left: float) -> None:
    """WF-12.2: end the run ERROR before any sub-agent starts; no retry (outside `step`)."""
    msg = (
        f"remaining time {left:.1f} s does not exceed the graph time bound {bound:.1f} s "
        "((W - L) / max_parallel + L, weight = time_budget_sec x max_tries) "
        f"plus the consolidation reserve {reserve:.1f} s (PROVIDER_TIMEOUT_SEC)"
    )
    code = Code.TIME_BUDGET_INSUFFICIENT
    detail = {
        "code": code.value,
        "bound": round(bound, 1),
        "reserve": reserve,
        "remaining": round(left, 1),
    }
    failed = fail_capsule(cap.model_copy(deep=True), code, msg)
    tl.append("coordinator", failed, Entry(TraceType.ERROR, detail), finalise)
    get_logger().warning(
        "time_budget_insufficient", code=code.value, run_id=tl.run.run_id,
        bound=bound, reserve=reserve, remaining=left,
    )  # fmt: skip


def _fail(tl: TraceLog, code: Code, message: str) -> None:
    cap = error_capsule(tl.run, code, message)
    tl.append("coordinator", cap, Entry(TraceType.ERROR), finalise)


def record_graph(tl: TraceLog, calls: list[Call], dag: list[DagNode]) -> int | None:
    """WF-4.4: one header edit sets `selection` and `dag`, with one `plan` trace record."""
    cap = tl.run.agents["coordinator"]
    detail = {
        "selection": [c.model_dump(mode="json") for c in calls],
        "dag": [n.model_dump(mode="json") for n in dag],
    }

    def set_graph(run: RunDocument) -> None:
        run.selection, run.dag = calls, dag

    return tl.append("coordinator", cap, Entry(TraceType.PLAN, detail), set_graph)
