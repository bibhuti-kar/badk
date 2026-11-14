"""Flow scheduler and error rule (design 25.8): `after` graph, `max_parallel` threads."""

import contextvars
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

from badk import proc
from badk.agents.flow import Step
from badk.coordinator.graph_run import INTERRUPT_JOIN_SEC, _log_failures, _raise
from badk.errors import BadkError
from badk.stderr_log import get_logger
from badk.tools.envelope import Outcome

log = get_logger()


class FlowRefused(BadkError):
    """A step refused to start (budget, FLOW-8b): unstarted steps are skipped `refused`."""


class StepEvents(NamedTuple):
    """Plan records + node state. `dispatch` runs on the step's worker thread, `skip` on the
    scheduler thread."""

    dispatch: Callable[[Step], None]
    skip: Callable[[Step, str], None]  # (step, cause)


RunOne = Callable[[Step, Mapping[str, Outcome]], Outcome]


class _Sched:
    """Run state. The caller thread alone writes `outcomes`/`skipped`/`done`: workers only queue."""

    def __init__(self, flow: Sequence[Step], run_one: RunOne, cap: int, ev: StepEvents) -> None:
        self.run_one, self.cap, self.ev = run_one, cap, ev
        self.pending, self.running = list(flow), 0
        self.out: queue.Queue[tuple[Step, Outcome | BaseException]] = queue.Queue()
        self.outcomes: dict[str, Outcome] = {}
        self.skipped: set[str] = set()
        self.done: set[str] = set()  # terminal: ran ok/error or skipped
        self.failures: list[BaseException] = []
        self.cancelled: Callable[[], bool] = proc.cancelled

    def _work(self, s: Step, seen: Mapping[str, Outcome]) -> None:
        res: Outcome | BaseException
        try:
            self.ev.dispatch(s)  # after the start is committed: the step always has a record
            res = self.run_one(s, seen)
        except BaseException as e:  # reported to the scheduler, never lost
            res = e
        self.out.put((s, res))

    def _take(self, s: Step, res: Outcome | BaseException) -> None:
        self.running -= 1
        if isinstance(res, BaseException):
            self.failures.append(res)
        else:
            self.outcomes[s.id] = res
            self.done.add(s.id)

    def collect(self, block: bool) -> None:
        while self.running and (block or not self.out.empty()):
            self._take(*self.out.get())
            block = False

    def drain(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while self.running:
            try:
                self._take(*self.out.get(timeout=max(0.0, end - time.monotonic())))
            except queue.Empty:
                return

    def stopped(self) -> bool:
        return bool(self.failures) or self.cancelled()

    def _bad_parent(self, s: Step) -> str | None:
        return next(
            (a for a in s.after if a in self.skipped or self.outcomes[a]["status"] == "error"),
            None,
        )

    def _start(self, s: Step) -> bool:
        self.collect(False)  # a failure already queued must stop the start
        if self.stopped():
            return False
        self.pending.remove(s)
        self.running += 1
        args = (self._work, s, dict(self.outcomes))
        threading.Thread(target=contextvars.copy_context().run, args=args, daemon=True).start()
        return True

    def start_ready(self) -> None:
        """Skip or start every step whose parents are terminal (a skip can unblock a skip)."""
        moved = True
        while moved and not self.stopped():
            moved = False
            for s in list(self.pending):
                if not self.done.issuperset(s.after):
                    continue
                bad = self._bad_parent(s)
                if bad and s.on_error == "skip":  # PM-38: transitive skip
                    self.pending.remove(s)
                    self.skipped.add(s.id)
                    self.done.add(s.id)
                    self.ev.skip(s, bad)
                    moved = True
                elif self.running < self.cap and not self._start(s):
                    return

    def skip_unstarted(self, cause: str) -> None:
        while self.pending:  # popped first: a failing skip is never tried twice
            s = self.pending.pop(0)
            self.skipped.add(s.id)
            self.ev.skip(s, cause)

    def skip_quietly(self, cause: str) -> None:
        try:
            self.skip_unstarted(cause)
        except Exception as e:  # a down broker must not replace the original error
            log.warning("skip_records_failed", error=repr(e))

    def cause(self) -> str:
        if self.cancelled():
            return "cancelled"
        if self.failures:
            refused = isinstance(self.failures[0], FlowRefused)
            return "refused" if refused else "error"
        return "cycle"  # load refuses cycles; nothing ready and nothing running


def run_steps(
    flow: Sequence[Step],
    run_one: RunOne,
    max_parallel: int,
    events: StepEvents,
    cancelled: Callable[[], bool] = proc.cancelled,
) -> tuple[dict[str, Outcome], set[str]]:
    """Returns (outcomes, skipped ids). A raise from `run_one` or `events.dispatch` stops new
    starts, lets running steps finish, skips the unstarted ones (`error` / `refused`) and
    re-raises the first error. Ctrl-C or a scheduler error: cancel children, wait
    INTERRUPT_JOIN_SEC, skip unstarted `cancelled` / `error`, re-raise."""
    s = _Sched(flow, run_one, max_parallel, events)
    s.cancelled = cancelled
    try:
        while True:
            s.start_ready()
            if not s.running:
                break
            s.collect(True)
        if not s.failures:
            s.skip_unstarted(s.cause())
    except BaseException as e:
        if (ch := proc.CURRENT.get()) is not None:
            ch.cancel()
        s.drain(INTERRUPT_JOIN_SEC)
        _log_failures(s.failures)
        s.skip_quietly("error" if isinstance(e, Exception) else "cancelled")
        raise
    if s.failures:  # outside the try: a skip error cannot replace the worker failure
        s.skip_quietly(s.cause())
        _raise(s.failures)  # first failure raised, the rest logged
    return s.outcomes, s.skipped
