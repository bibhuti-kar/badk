"""Budget-path restart (design 7.9, RES-2). Pure over the capsule; clock and emit come in `deps`."""

from badk.agents.definition import AgentDef
from badk.engine.step import StepDeps
from badk.engine.tokens import estimate_tokens
from badk.exec.document import Capsule, CapsuleStatus, Message, TraceType
from badk.exec.trace import metrics_of
from badk.stderr_log import get_logger

log = get_logger()


def from_journal(agent: AgentDef, capsule: Capsule, deps: StepDeps) -> Capsule:
    """Restart a BUDGET_EXCEEDED capsule: fresh history and per-try window, journal and total
    spend kept. `max_tries` counts every try, the first included; when none is left the
    capsule is INCOMPLETE (no call)."""
    c = capsule.model_copy(deep=True)
    # a context halt before any spend this try: a fresh try rebuilds the same payload
    futile = c.metadata.get("halt_reason") == "context" and not c.metadata.get("try_tokens")
    if c.tries + 1 >= agent.max_tries or futile:
        c.status = CapsuleStatus.INCOMPLETE
        deps.emit(c.model_copy(deep=True), None, {}, metrics_of(None))
        reason = "context" if futile else "max_tries"
        log.info("INCOMPLETE", reason=reason, agent=c.agent)
        return c
    c.tries += 1
    c.metadata.pop("halt_reason", None)  # explains a halt; stale once the try restarts
    c.active_chat_history = [
        Message(
            role="system",
            content=f"RESTARTED: try {c.tries + 1} of {agent.max_tries} from journal.",
        )
    ]
    c.metadata["try_tokens"] = 0
    c.metadata["try_started"] = deps.clock()
    c.status = CapsuleStatus.RUNNING
    journal = estimate_tokens(c.journal_summary)
    detail = {"tries": c.tries, "journal_tokens": journal}
    deps.emit(c.model_copy(deep=True), TraceType.RESTART, detail, metrics_of(None))
    return c
