"""Budget guard rails (design 7.6). Pure: the clock arrives as `now`; no module state."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from badk.agents.definition import AgentDef
from badk.exec.document import Capsule, CapsuleStatus, TraceType
from badk.exec.trace import Metrics, metrics_of
from badk.stderr_log import get_logger

log = get_logger()

if TYPE_CHECKING:  # runtime import would cycle: router -> engine.compress -> budget
    from badk.router.reply import ModelReply

Emit = Callable[[Capsule, TraceType | None, dict[str, Any], Metrics], int | None]


def reply_metrics(reply: "ModelReply", steps: int) -> Metrics:
    """Metrics of one model call (steps 1 for a step, 0 for a compression call)."""
    return Metrics(
        tokens_in=reply.tokens_in,
        tokens_out=reply.tokens_out,
        latency_ms=reply.latency_ms,
        steps=steps,
    )


def seconds_exceeded(agent: AgentDef, capsule: Capsule, now: float) -> bool:
    """The time budget alone (tools cost no tokens, so the tool loop checks only this)."""
    md = capsule.metadata
    return "try_started" in md and now - md["try_started"] >= agent.time_budget_sec


def exceeded(agent: AgentDef, capsule: Capsule, estimate: int | None, now: float) -> str | None:
    """Return the reason the next model call must not happen ("tokens", "seconds", "context"),
    or None. Per-try state lives in `capsule.metadata` (`try_tokens`, `try_started`);
    `estimate` is the payload about to be sent (None skips the context check)."""
    md = capsule.metadata
    if md.get("try_tokens", 0) >= agent.token_budget:
        return "tokens"
    if seconds_exceeded(agent, capsule, now):
        return "seconds"
    if estimate is not None and estimate > agent.context_window:
        return "context"
    return None


def halt(c: Capsule, reason: str, emit: Emit) -> Capsule:
    """BUDGET_EXCEEDED: persist the capsule (no trace record), log the reason (design 7.2)."""
    c.status = CapsuleStatus.BUDGET_EXCEEDED
    c.metadata["halt_reason"] = reason
    emit(c.model_copy(deep=True), None, {}, metrics_of(None))
    log.info("BUDGET_EXCEEDED", reason=reason, agent=c.agent)
    return c
