"""One agent step (design 7.1/7.2, ENG-1). Pure: every effect arrives through `StepDeps`.

The model call, the tool loop and the budget guard run in one loop until the reply has no
`tool_calls`. `done: true` -> COMPLETED; `done: false` -> RUNNING (the dispatcher feeds
"CONTINUE"). A model or tool failure is retried once (RES-1), then the capsule goes to ERROR.
`step` emits `llm` and `tool` records; the caller emits `done`/`error` (it owns plan ordering).
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import count
from typing import Any

from badk.agents.definition import AgentDef
from badk.engine.budget import Emit, exceeded, halt, reply_metrics, seconds_exceeded
from badk.engine.compress import maybe_compress
from badk.engine.envelope import Envelope, ToolCall, parse
from badk.engine.payload import build
from badk.engine.retry import once
from badk.engine.tokens import estimate_messages
from badk.errors import BadkError, Code, PersistenceError
from badk.exec.document import Capsule, CapsuleStatus, Message, TraceType, fail_capsule
from badk.exec.trace import Metrics, metrics_of
from badk.router import ModelReply
from badk.tools.definition import ToolDef

DETAIL_CHARS = 4000  # design 7.2: tool `result` in trace detail


def no_tool_executor(agent: AgentDef, call: ToolCall) -> dict[str, Any]:
    """Pure default: no effects here. `make_deps` binds `tools.execute.run_tool`."""
    raise BadkError(Code.TOOL_ERROR, f"no tool executor: cannot run {call.name}")


def no_tools(agent: AgentDef) -> Mapping[str, ToolDef]:
    return {}


@dataclass(frozen=True)
class StepDeps:
    complete: Callable[[AgentDef, list[Message]], ModelReply]
    emit: Emit
    clock: Callable[[], float]
    result_check: Callable[[dict[str, Any]], object] | None = None
    run_tool: Callable[[AgentDef, ToolCall], dict[str, Any]] = no_tool_executor  # guard + execute
    tools: Callable[[AgentDef], Mapping[str, ToolDef]] = no_tools  # TOOLS block of the payload


class _Halt(Exception):
    """Raised by `on_retry` when the budget forbids the second attempt (RES-1 x ENG-4)."""

    def __init__(self, reason: str, error: BadkError) -> None:
        self.reason = reason
        self.error = error


def _retried(
    agent: AgentDef, c: Capsule, e: BadkError, deps: StepDeps, estimate: int | None
) -> None:
    """Model `on_retry`: the budget guard of a first call also gates the retry."""
    if reason := exceeded(agent, c, estimate, deps.clock()):
        raise _Halt(reason, e)
    _note_retry(c, e, deps)


def _tool_retried(agent: AgentDef, c: Capsule, e: BadkError, deps: StepDeps) -> None:
    """Tool `on_retry`: tools cost no tokens, so only the time budget gates the retry."""
    if seconds_exceeded(agent, c, deps.clock()):
        raise _Halt("seconds", e)
    _note_retry(c, e, deps)


def _note_retry(c: Capsule, e: BadkError, deps: StepDeps) -> None:
    detail = {"code": e.code.value, "message": e.message, "attempt": 1}
    deps.emit(c.model_copy(deep=True), TraceType.RETRY, detail, metrics_of(None))


def _attempt(agent: AgentDef, c: Capsule, msgs: list[Message], deps: StepDeps, n: int) -> Envelope:
    reply = deps.complete(agent, msgs)
    spent = reply.tokens_in + reply.tokens_out
    c.token_spent_so_far += spent
    c.metadata["try_tokens"] += spent
    m = reply_metrics(reply, 1)
    detail: dict[str, Any] = {
        "provider": agent.provider,
        "model": agent.model,
        "attempt": n,
        "estimate": estimate_messages(msgs),
    }
    try:
        env = parse(reply.text, agent.validate_output, deps.result_check)
    except BadkError as e:
        deps.emit(c.model_copy(deep=True), TraceType.LLM, detail | {"error": e.message}, m)
        raise
    c.active_chat_history.append(Message(role="assistant", content=reply.text))
    c.metadata["result"] = env.result
    detail |= {"done": env.done, "n_tool_calls": len(env.tool_calls), "notes": env.notes}
    deps.emit(c.model_copy(deep=True), TraceType.LLM, detail, m)
    return env


def _model_call(
    agent: AgentDef, c: Capsule, msgs: list[Message], estimate: int, deps: StepDeps
) -> Envelope | BadkError | str:
    """Model call + parse under `retry.once` (RES-1). Tokens always counted (also on a bad
    reply), one `llm` record per attempt. The final failure is returned; a budget reason (str)
    when the guard forbids the retry; a PersistenceError (from an emit) propagates."""
    n = count(1)
    try:
        return once(
            lambda: _attempt(agent, c, msgs, deps, next(n)),
            lambda e: _retried(agent, c, e, deps, estimate),
        )
    except _Halt as h:
        return h.reason
    except PersistenceError:
        raise
    except BadkError as e:
        return e


def _exec(
    agent: AgentDef, c: Capsule, call: ToolCall, deps: StepDeps
) -> dict[str, Any] | BadkError | _Halt:
    """The `_Halt` (reason + failed error) when the time budget forbids the retry."""
    try:
        return once(
            lambda: deps.run_tool(agent, call),
            lambda e: _tool_retried(agent, c, e, deps),
        )
    except _Halt as h:
        return h
    except PersistenceError:
        raise
    except BadkError as e:
        return e
    except Exception as e:  # noqa: BLE001 - any tool failure ends the capsule in ERROR
        return BadkError(Code.TOOL_ERROR, f"{type(e).__name__}: {e}")


def _halt_seconds(c: Capsule, skipped: int, deps: StepDeps, reason: str = "seconds") -> Capsule:
    for _ in range(skipped):
        note = "TOOL_RESULT not run: time budget exceeded"
        c.active_chat_history.append(Message(role="user", content=note))
    return halt(c, reason, deps.emit)


def _run_tools(
    agent: AgentDef, c: Capsule, calls: list[ToolCall], deps: StepDeps
) -> Capsule | None:
    """None when every call ran. Otherwise the ended capsule: ERROR on a failed tool, or
    BUDGET_EXCEEDED when the time budget trips between calls (skipped calls get a note)."""
    for n, call in enumerate(calls):
        if seconds_exceeded(agent, c, deps.clock()):
            return _halt_seconds(c, len(calls) - n, deps)
        t0 = deps.clock()
        out = _exec(agent, c, call, deps)
        if isinstance(out, _Halt):  # time budget tripped before the retry
            note = f"TOOL_RESULT {call.name} failed ({out.error.code.value}: {out.error.message})"
            note += "; not retried: time budget exceeded"
            c.active_chat_history.append(Message(role="user", content=note))
            return _halt_seconds(c, len(calls) - n - 1, deps, out.reason)
        ms = int((deps.clock() - t0) * 1000)
        tm = Metrics(tokens_in=0, tokens_out=0, latency_ms=ms, steps=1)
        detail: dict[str, Any] = {"name": call.name, "args": call.args}
        if isinstance(out, BadkError):
            detail |= {"ok": False, "error": {"code": out.code.value, "message": out.message}}
            deps.emit(c.model_copy(deep=True), TraceType.TOOL, detail, tm)
            return fail_capsule(c, out.code, out.message)
        text = json.dumps({"name": call.name, "result": out}, default=str)
        cut = text[: agent.context_window]  # D-26
        c.active_chat_history.append(Message(role="user", content="TOOL_RESULT " + cut))
        detail |= {"ok": True, "result": text[:DETAIL_CHARS], "truncated": len(cut) < len(text)}
        deps.emit(c.model_copy(deep=True), TraceType.TOOL, detail, tm)
    return None


def _begin(capsule: Capsule, deps: StepDeps) -> Capsule:
    c = capsule.model_copy(deep=True)
    now = deps.clock()
    if c.time_started == 0:
        c.time_started = now
    c.metadata.setdefault("try_started", now)
    c.metadata.setdefault("try_tokens", 0)
    return c


def _turn(agent: AgentDef, c: Capsule, deps: StepDeps) -> tuple[Capsule, bool]:
    """One pass of the loop: compress, budget guard, model call, tools. (capsule, finished)."""
    tools = deps.tools(agent)
    c = maybe_compress(agent, c, deps, tools)  # ENG-3: budget-checked, may halt or fail
    if c.status != CapsuleStatus.RUNNING:
        return c, True
    msgs = build(agent, c, tools)
    estimate = estimate_messages(msgs)
    if reason := exceeded(agent, c, estimate, deps.clock()):
        return halt(c, reason, deps.emit), True  # before every model call (ENG-4.4)
    env = _model_call(agent, c, msgs, estimate, deps)
    if isinstance(env, str):  # budget tripped before the retry: same halt as before a call
        return halt(c, env, deps.emit), True
    if isinstance(env, BadkError):
        return fail_capsule(c, env.code, env.message), True
    if not env.tool_calls:
        if env.done:
            c.status = CapsuleStatus.COMPLETED
        return c, True
    ended = _run_tools(agent, c, env.tool_calls, deps)
    return (ended, True) if ended else (c, False)


def step(agent: AgentDef, capsule: Capsule, user_input: str, deps: StepDeps) -> Capsule:
    c = _begin(capsule, deps)
    if reason := exceeded(agent, c, None, deps.clock()):  # ENG-4.1: before the input is added
        return halt(c, reason, deps.emit)
    c.active_chat_history.append(Message(role="user", content=user_input))
    finished = False
    while not finished:
        c, finished = _turn(agent, c, deps)
    return c
