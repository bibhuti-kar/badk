"""TraceLog (design 5.4, D-14): the one owner of a run's document; `append` is the only mutation."""

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from badk.errors import BadkError, Code, PersistenceError
from badk.exec.document import Capsule, RunDocument, TraceRecord, TraceType, utc_now_iso
from badk.stderr_log import get_logger

log = get_logger()
HeaderEdit = Callable[[RunDocument], None]  # runs under the TraceLog lock


class Metrics(TypedDict):
    """Complete event metrics; store.apply reads every key, so no producer may omit one."""

    tokens_in: int
    tokens_out: int
    latency_ms: int
    steps: int


def metrics_of(t: TraceRecord | None) -> Metrics:
    """Event metrics of a trace record (steps 1 only for model/tool calls, design 4.4)."""
    return {
        "tokens_in": t.tokens_in if t else 0,
        "tokens_out": t.tokens_out if t else 0,
        "latency_ms": t.latency_ms if t else 0,
        "steps": int(t is not None and t.type in (TraceType.LLM, TraceType.TOOL)),
    }


def run_event(
    run: RunDocument,
    eid: int,
    trace: TraceRecord | None = None,
    metrics: Metrics | None = None,
    agent_key: str = "coordinator",
) -> dict[str, Any]:
    """Step event (design 5.2): run header, agent capsule, optional trace record.

    Explicit metrics win; else derived from the record.
    """
    return {
        "v": 1,
        "run_id": run.run_id,
        "eid": eid,
        "ts": utc_now_iso(),
        "header": {
            "status": run.status,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "input": run.input.model_dump(mode="json"),
            "workflow": run.workflow,
            "selection": [c.model_dump(mode="json") for c in run.selection],
            "dag": [n.model_dump(mode="json") for n in run.dag],
        },
        "totals": run.totals.model_dump(mode="json"),
        "agent_key": agent_key,
        "capsule": run.agents[agent_key].model_dump(mode="json"),
        "trace": trace.model_dump(mode="json") if trace else None,
        "metrics": metrics if metrics is not None else metrics_of(trace),
        "logs": [],
    }


class Sink(Protocol):
    """Event consumer; the one definition (coordinator and CLI import it from here)."""

    def __call__(self, event: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Entry:
    """The trace-record part of an append: type (None = capsule-only), detail, metrics."""

    type: TraceType | None
    detail: dict[str, Any] = field(default_factory=dict)
    metrics: Metrics = field(default_factory=lambda: metrics_of(None))
    task_id: str | None = None  # overrides the capsule's (flow tool steps, design 25.7)


class TraceLog:
    def __init__(self, run: RunDocument, sinks: Sequence[Sink], trace_enabled: bool) -> None:
        self.run = run
        self._sinks = sinks
        self._enabled = trace_enabled
        # Guards eid, seq and every mutation of self.run. Invariant: sinks are called serially in
        # eid order UNDER it, so no sink is entered by two threads. A sink must not block on
        # another thread and must not call back into the TraceLog.
        self._lock = threading.Lock()
        self.frozen = False  # set by freeze(): later appends are dropped
        self._eid = 0
        self._ms = 0  # unrounded latency sum; totals.seconds = round(_ms / 1000, 3) (D-7)
        self._seq = len(run.trace)

    def append(
        self,
        agent_key: str,
        capsule: Capsule,
        entry: Entry,
        header: HeaderEdit | None = None,
    ) -> int | None:
        """Returns the trace seq, or None (capsule-only event or TRACE_ENABLED=false).

        `header` edits the run header (selection/dag, status, ended_at) in the same locked step."""
        with self._lock:
            if self.frozen:  # SDK-4: a worker thread outliving an interrupted run
                return None
            type_, detail, metrics = entry.type, entry.detail, entry.metrics
            tin, tout, ms = metrics["tokens_in"], metrics["tokens_out"], metrics["latency_ms"]
            steps = metrics["steps"]  # a partial dict raises here, before eid/seq move
            self._eid += 1
            rec: TraceRecord | None = None
            if type_ is not None and self._enabled:
                self._seq += 1
                rec = TraceRecord(
                    seq=self._seq,
                    ts=utc_now_iso(),
                    agent=capsule.agent,
                    task_id=entry.task_id or capsule.task_id,
                    type=type_,
                    tokens_in=tin,
                    tokens_out=tout,
                    latency_ms=ms,
                    detail=detail,
                )
                self.run.trace.append(rec)
            self.run.agents[agent_key] = capsule
            t = self.run.totals
            t.tokens += tin + tout
            self._ms += ms
            t.seconds = round(self._ms / 1000, 3)
            t.steps += steps
            if header:
                header(self.run)
            self._emit(run_event(self.run, self._eid, rec, metrics, agent_key))
        return rec.seq if rec else None

    def header_changed(self, header: HeaderEdit | None = None) -> None:
        """Coordinator-only event (header changed, e.g. status/ended_at): no trace record."""
        with self._lock:
            if not self.frozen:
                self._header_event(header)

    def freeze(self, header: HeaderEdit) -> None:
        """Apply the final header edit and drop every later append (no entry after ended_at)."""
        with self._lock:
            if not self.frozen:
                self.frozen = True
                self._header_event(header)

    def _header_event(self, header: HeaderEdit | None) -> None:
        """Caller holds the lock."""
        self._eid += 1
        if header:
            header(self.run)
        self._emit(run_event(self.run, self._eid))

    def _emit(self, event: dict[str, Any]) -> None:
        """Persistence sink first: if it fails no other sink sees the event and the error raises."""
        for i, sink in enumerate(self._sinks):
            try:
                sink(event)
            except Exception as e:
                if i == 0:
                    if isinstance(e, BadkError):
                        raise PersistenceError(e.code, e.message, e.details) from e
                    raise PersistenceError(Code.INTERNAL_ERROR, f"{type(e).__name__}: {e}") from e
                log.warning("sink failed", error=str(e))  # design 11.1: never fails the run
