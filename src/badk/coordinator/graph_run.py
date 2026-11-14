"""Graph runtime (design 20.5, WF-5): `run_dag` schedules DagNodes over `dispatcher.run_task`."""

import contextvars
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, cast

from badk import proc
from badk.agents.definition import AgentDef
from badk.coordinator.dispatcher import DepsFor, run_task
from badk.exec.document import Capsule, CapsuleStatus, DagNode, RunDocument, TraceType, dumps
from badk.exec.trace import Entry, TraceLog
from badk.stderr_log import get_logger

log = get_logger()

INTERRUPT_JOIN_SEC: Final = 2  # design 20.9: bounded wait for node threads after Ctrl-C


def node_input(node: DagNode, done: Sequence[tuple[str, Capsule]]) -> str:
    """WF-5 first input: the node's input, then one line per completed dependency instance."""
    lines = [f"TASK: {node.input}"]
    lines += [f"DEPENDS_ON {k} RESULT: {dumps(c.metadata.get('result') or {})}" for k, c in done]
    return "\n".join(lines) + "\n"


@dataclass
class DagCtx:
    """What `run_dag` needs. `cancelled` is the extension-point SDK-4 sets (`children.cancelled`):
    once true no new node starts."""

    tl: TraceLog
    agents: Mapping[str, AgentDef]
    max_parallel: int
    deps_for: DepsFor
    cancelled: Callable[[], bool] = proc.cancelled
    nodes: dict[str, DagNode] = field(default_factory=dict, init=False)  # set by run_dag
    deps: dict[str, list[str]] = field(default_factory=dict, init=False)


Nodes = dict[str, DagNode]


def _mark(
    nodes: Nodes, key: str, state: Literal["ran", "skipped"]
) -> Callable[[RunDocument], None]:
    def edit(_run: RunDocument) -> None:
        nodes[key].state = state

    return edit


def _skip(ctx: DagCtx, node: DagNode, cause: str) -> None:
    detail = {"decision": "skip", "task_id": node.key, "agent": node.agent, "cause": cause}
    tl, entry = ctx.tl, Entry(TraceType.PLAN, detail)
    tl.append(
        "coordinator", tl.run.agents["coordinator"], entry, _mark(ctx.nodes, node.key, "skipped")
    )


def _work(
    node: DagNode,
    first: str,
    ctx: DagCtx,
    out: "queue.Queue[tuple[str, Capsule | BaseException]]",
) -> None:
    """Runs one node and queues its result; the scheduler counts slots from the results it
    collects, so no slot is free before the scheduler has seen the completion."""
    res: Capsule | BaseException
    try:
        detail = {
            "decision": "dispatch",
            "agent": node.agent,
            "task_id": node.key,
            "thread": threading.current_thread().name,
        }
        coord = ctx.tl.run.agents["coordinator"]
        ctx.tl.append(
            "coordinator", coord, Entry(TraceType.PLAN, detail), _mark(ctx.nodes, node.key, "ran")
        )
        res = run_task(node, ctx.agents[node.agent], first, ctx.tl, ctx.deps_for)
    except BaseException as e:  # persistence failure: reported to the scheduler, not lost
        res = e
    out.put((node.key, res))


def _advance(
    ctx: DagCtx,
    pending: list[DagNode],
    done: dict[str, Capsule | None],
) -> list[tuple[DagNode, str]]:
    """Skip or collect every pending node whose dependencies are all terminal, until nothing
    moves (a skip can unblock a further skip). Returns the ready (node, first input) pairs."""
    ready: list[tuple[DagNode, str]] = []
    moved = True
    while moved:
        moved = False
        for n in list(pending):
            keys = ctx.deps[n.key]
            if any(k not in done for k in keys):
                continue
            pending.remove(n)
            moved = True
            bad = [k for k in keys if (c := done[k]) is None or c.status != CapsuleStatus.COMPLETED]
            if bad:
                done[n.key] = None
                _skip(ctx, n, bad[0])
            else:
                ready.append((n, node_input(n, [(k, cast(Capsule, done[k])) for k in keys])))
    return ready


def _log_failures(failures: Sequence[BaseException]) -> None:
    for extra in failures:  # RunInterrupted is the cancel working, not a failure
        if isinstance(extra, proc.RunInterrupted):
            log.debug("dispatch_worker_interrupted")
        else:
            log.error("dispatch_worker_failed", error=repr(extra))


def _raise(failures: list[BaseException]) -> None:
    if failures:
        _log_failures(failures[1:])  # only the first is re-raised: keep the rest visible
        raise failures[0]


class _Sched:
    """run_dag's state: the caller thread is the only one that touches it."""

    def __init__(self, dag: Sequence[DagNode], ctx: DagCtx) -> None:
        self.ctx = ctx
        self.out: queue.Queue[tuple[str, Capsule | BaseException]] = queue.Queue()
        self.done: dict[str, Capsule | None] = {}  # None = skipped
        self.pending, self.running = list(dag), 0
        self.queued: list[tuple[DagNode, str]] = []
        self.failures: list[BaseException] = []

    def _take(self, key: str, res: Capsule | BaseException) -> None:
        self.running -= 1
        if isinstance(res, BaseException):
            self.failures.append(res)
        else:
            self.done[key] = res

    def collect(self, block: bool) -> None:
        while self.running and (block or not self.out.empty()):
            self._take(*self.out.get())
            block = False

    def drain(self, timeout: float | None) -> None:
        """Wait for every running node to report (all of them, or until `timeout` seconds)."""
        end = None if timeout is None else time.monotonic() + timeout
        while self.running:
            left = None if end is None else max(0.0, end - time.monotonic())
            try:
                self._take(*self.out.get(timeout=left))
            except queue.Empty:
                return

    def stopped(self) -> bool:
        return bool(self.failures) or self.ctx.cancelled()

    def start_ready(self) -> None:
        self.queued += _advance(self.ctx, self.pending, self.done)
        while self.queued and self.running < self.ctx.max_parallel:
            self.collect(False)  # a failure already queued must stop the start
            if self.stopped():
                return
            n, first = self.queued.pop(0)
            args = (n, first, self.ctx, self.out)
            threading.Thread(
                target=contextvars.copy_context().run,
                args=(_work, *args),
                name=f"badk-{n.key}",
                daemon=True,
            ).start()
            self.running += 1


def run_dag(dag: Sequence[DagNode], ctx: DagCtx) -> dict[str, Capsule]:
    """Run the graph (design 20.5): one daemon thread per running node, a `max_parallel` slot
    taken before it starts. A node is ready when every instance of each depends_on agent is
    terminal and COMPLETED; one that cannot ever be is skipped (transitively) with its first
    non-COMPLETED dependency as cause. Nodes never started (cycle, cancel) get a skip record
    too. Returns {key: terminal capsule} of nodes that ran. A persistence error stops dispatch,
    lets running nodes finish, and propagates. Any other scheduler error or Ctrl-C cancels the
    children, waits at most INTERRUPT_JOIN_SEC, records skips for unstarted nodes and re-raises."""
    ctx.nodes = {n.key: n for n in ctx.tl.run.dag}
    ctx.deps = {n.key: [d.key for d in dag if d.agent in n.depends_on] for n in dag}
    s = _Sched(dag, ctx)
    try:
        while True:
            if not s.stopped():
                s.start_ready()
            if not s.running:
                break
            s.collect(True)
    except BaseException as e:
        interrupted = not isinstance(e, Exception)  # Ctrl-C vs scheduler error: same handling
        if (ch := proc.CURRENT.get()) is not None:
            ch.cancel()
        s.drain(INTERRUPT_JOIN_SEC)  # bounded: a stuck node cannot hold the original error back
        _log_failures(s.failures)
        _skip_unstarted_quietly(s, "cancelled" if interrupted else "error")
        raise
    _raise(s.failures)
    _skip_unstarted(s, "cancelled" if ctx.cancelled() else "cycle")
    return {k: c for k, c in s.done.items() if c is not None}


def _skip_unstarted_quietly(s: _Sched, cause: str) -> None:
    try:
        _skip_unstarted(s, cause)
    except Exception as e:  # a down broker must not replace the original error
        log.warning("skip_records_failed", error=repr(e))


def _skip_unstarted(s: _Sched, cause: str) -> None:
    ctx = s.ctx
    for n in [q[0] for q in s.queued] + s.pending:  # never started: say why in the trace
        s.done[n.key] = None
        _skip(ctx, n, cause)
