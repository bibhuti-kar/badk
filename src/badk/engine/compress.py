"""History compression (design 7.7). Pure: the model call arrives through StepDeps."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from badk.agents.definition import AgentDef
from badk.engine.budget import exceeded, halt, reply_metrics
from badk.engine.envelope import parse
from badk.engine.payload import GLOBAL_SYSTEM_PREFIX, build
from badk.engine.tokens import estimate_messages
from badk.errors import BadkError, Code
from badk.exec.document import Capsule, Message, TraceType, fail_capsule
from badk.exec.trace import Metrics
from badk.tools.definition import ToolDef

if TYPE_CHECKING:  # step and router import this module
    from badk.engine.step import StepDeps

COMPRESS_INSTRUCTION: Final[str] = (
    "Compress HISTORY into a new journal. Keep data values, errors, exact parameters. "
    'Drop filler. result = {"journal": str}.'
)


def _journal(result: object) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("journal"), str):
        raise BadkError(Code.OUTPUT_INVALID, 'compression result must be {"journal": str}')


def compression_messages(capsule: Capsule) -> list[Message]:
    history = "\n".join(f"{m.role}: {m.content}" for m in capsule.active_chat_history)
    system = f"{COMPRESS_INSTRUCTION}\n\nJOURNAL:\n{capsule.journal_summary}"
    return [
        Message(role="system", content=GLOBAL_SYSTEM_PREFIX),
        Message(role="system", content=system),
        Message(role="user", content=history),
    ]


def _can_help(agent: AgentDef, capsule: Capsule, tools: Mapping[str, ToolDef]) -> int | None:
    """The full payload estimate when compressing can help (history exists, the fixed part is
    under the threshold, the whole is over it); else None. `tools`: the real payload (7.7)."""
    limit = agent.compression_threshold * agent.context_window
    fixed = capsule.model_copy(update={"active_chat_history": []})
    if not capsule.active_chat_history or estimate_messages(build(agent, fixed, tools)) >= limit:
        return None
    full = estimate_messages(build(agent, capsule, tools))
    return full if full >= limit else None


_TOOL = "TOOL_RESULT "
_CUR, _LAST = "CURRENT INPUT:\n", "\nLATEST TOOL_RESULT:\n"


def _carry(history: list[Message]) -> tuple[str, str | None]:
    """The agent's real input (latest non-tool user turn, or the one an earlier marker carries)
    and the latest tool result if it is the last message."""
    tool = history[-1].content if history[-1].content.startswith(_TOOL) else None
    for m in reversed(history):
        if m.role == "user" and not m.content.startswith(_TOOL):
            return m.content, tool
        if m.role == "system" and m.content.startswith("COMPRESSED:") and _CUR in m.content:
            return m.content.split(_CUR, 1)[1].split(_LAST, 1)[0], tool
    return "", tool


def _marker(seq: int | None, current_input: str, tool: str | None = None) -> Message:
    """PM-14: one system-role marker. It carries the current input verbatim, and the latest
    tool result when the history ends with one."""
    text = f"COMPRESSED: history moved to journal. trace seq={'none' if seq is None else seq}."
    body = f"{text}\n{_CUR}{current_input}" + (f"{_LAST}{tool}" if tool else "")
    return Message(role="system", content=body)


def _parse_journal(text: str) -> str:
    env = parse(text, _journal)
    if not env.done or env.tool_calls:
        raise BadkError(Code.OUTPUT_INVALID, "compression reply: not done or has tool_calls")
    _journal(env.result)  # parse skips the schema when tool_calls are present
    return str(env.result["journal"])


def _apply(  # noqa: PLR0913 - one private call site; all six are needed
    agent: AgentDef,
    c: Capsule,
    before: int,
    metrics: Metrics,
    deps: "StepDeps",
    tools: Mapping[str, ToolDef],
) -> Capsule:
    current, tool = _carry(c.active_chat_history)
    c.active_chat_history = [_marker(None, current, tool)]
    # ponytail: the seq is unknown until emit returns; the snapshot and `after_tokens` use "none".
    detail = {
        "before_tokens": before,
        "after_tokens": estimate_messages(build(agent, c, tools)),
    }
    seq = deps.emit(c.model_copy(deep=True), TraceType.COMPRESS, detail, metrics)
    c.active_chat_history = [_marker(seq, current, tool)]
    return c


def maybe_compress(
    agent: AgentDef,
    capsule: Capsule,
    deps: "StepDeps",
    tools: Mapping[str, ToolDef] | None = None,
) -> Capsule:
    tools = deps.tools(agent) if tools is None else tools
    before = _can_help(agent, capsule, tools)
    if before is None:
        return capsule
    c = capsule.model_copy(deep=True)
    msgs = compression_messages(c)
    if reason := exceeded(agent, c, estimate_messages(msgs), deps.clock()):
        return halt(c, reason, deps.emit)
    try:
        reply = deps.complete(agent, msgs)
    except BadkError as e:
        return fail_capsule(c, e.code, e.message)
    spent = reply.tokens_in + reply.tokens_out
    c.token_spent_so_far += spent
    c.metadata["try_tokens"] = c.metadata.get("try_tokens", 0) + spent
    m = reply_metrics(reply, 0)
    try:
        journal = _parse_journal(reply.text)
    except BadkError as e:
        detail = {"before_tokens": before, "error": e.message}
        deps.emit(c.model_copy(deep=True), TraceType.COMPRESS, detail, m)
        return fail_capsule(c, e.code, e.message)
    c.journal_summary = journal
    return _apply(agent, c, before, m, deps, tools)
