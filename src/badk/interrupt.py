"""Ctrl-C handling for one run (SDK-4): kill every child, then end the run INCOMPLETE."""

from badk.exec.document import CapsuleStatus, RunDocument, RunStatus, finalise
from badk.exec.trace import TraceLog
from badk.proc import Children
from badk.stderr_log import get_logger

log = get_logger()


def _incomplete(run: RunDocument) -> None:
    for cap in run.agents.values():  # RES-2.4: no capsule stays RUNNING in a closed run
        if cap.status in (CapsuleStatus.RUNNING, CapsuleStatus.BUDGET_EXCEEDED):
            cap.status = CapsuleStatus.INCOMPLETE
            cap.metadata["halt_reason"] = "interrupt"
    finalise(run)
    run.status = RunStatus.INCOMPLETE  # an interrupted run is never COMPLETED or ERROR


def finalise_interrupted(tl: TraceLog, children: Children) -> None:
    """Kill all child process groups, stop the DAG from starting nodes, end a RUNNING run
    INCOMPLETE (open capsules too) and freeze the trace (worker threads may still be unwinding;
    `run_dag` already joined them, bounded). A fault is logged."""
    children.cancel()
    if tl.run.ended_at is None:
        try:
            tl.freeze(_incomplete)
        except Exception as e:
            log.warning("interrupted run could not be finalised", error=repr(e))
