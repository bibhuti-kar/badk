"""Library entry point (design 20.7): `badk.run(ask, ...)` runs one ask and returns the result."""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from badk import proc
from badk.agents.definition import AgentDef, load_agents
from badk.config import load_settings
from badk.coordinator.run import RunOptions, make_sinks, new_run, run_coordinator
from badk.errors import AckFailed, BadkError, Code, PersistenceError, redact
from badk.exec.document import (
    RunDocument,
    TraceType,
    error_capsule,
    fail_capsule,
    finalise,
    render_final,
)
from badk.exec.trace import Entry, Sink, TraceLog
from badk.interrupt import finalise_interrupted
from badk.progress import ProgressLog
from badk.stack_check import RunDeferred, stack_check
from badk.stderr_log import get_logger, set_level
from badk.tools.definition import NO_CWD, validate_all
from badk.tools.execute import permitted_tools
from badk.tools.guard import tool_cwd

log = get_logger()
_stack_check = stack_check  # tests patch this name
_Say = Callable[[str], object]


@dataclass(frozen=True)
class RunResult:
    """A finished run: id, status string, answer text, first error (or None), document, url."""

    run_id: str
    status: str
    answer: str
    error: BadkError | None
    document: dict[str, Any]
    url: str


def _check_workspaces(agents: Mapping[str, AgentDef], tdir: Path, root: Path) -> None:
    """The check tool time makes (guard.tool_cwd), made early; NO_CWD-only agents have none."""
    for a in agents.values():
        tools = permitted_tools(a, tdir)
        if tools and all(t.type in NO_CWD for t in tools.values()):
            continue
        try:
            tool_cwd(a, root)
        except BadkError as e:
            if e.code != Code.TOOL_ERROR:  # a secret folder keeps its tool-time code
                raise
            msg = f"{a.name}: workspace folder {a.workspace[0]} does not exist"
            raise BadkError(Code.VALIDATION_ERROR, msg) from e


def _error_of(doc: RunDocument) -> BadkError | None:
    """The coordinator's error, else the first ERROR node in document order."""
    caps = doc.agents
    for key in ("coordinator", *caps):
        e = key in caps and caps[key].error
        if e:
            err = BadkError(e.code, e.message)
            err.run_id = doc.run_id
            return err
    return None


def run(  # noqa: PLR0913 - the public keyword surface (design 20.7)
    ask: str,
    *,
    workflow: str | None = None,
    agents_dir: str | os.PathLike[str] = "agents",
    tools_dir: str | os.PathLike[str] | None = None,
    settings: Mapping[str, str] | None = None,
    progress_log: bool = False,
    on_run_id: Callable[[str], None] | None = None,
) -> RunResult:
    """Run `ask` against the agents in `agents_dir` and return once the run is terminal.

    The project root is the parent of `agents_dir`: `.env`, `tools/`, `progress/` and each
    agent's `workspace[0]` resolve against it, never the process cwd. `settings` replaces
    existing env keys (e.g. `{"LLM_MODEL": "b"}`) for this call only. `workflow="name"` picks
    the file `coordinator-name`. `on_run_id` is called once the run exists."""
    adir = Path(agents_dir).resolve()
    root = adir.parent
    conf = load_settings(root / ".env", overrides=settings or {})
    set_level(conf.log_level if "log_level" in conf.model_fields_set else "WARNING")
    tdir = Path(tools_dir).resolve() if tools_dir else root / "tools"
    agents = load_agents(adir, conf, tools=tdir, env_file=root / ".env")
    validate_all(tdir)
    _check_workspaces(agents, tdir, root)  # design 20.7 prepare check: before any run exists
    doc, opts = new_run(ask, agents, conf, workflow=workflow)
    opts = replace(opts, tools=tdir)  # the run loads the tools it validated
    _stack_check(conf)
    sinks = make_sinks(conf, False)
    if progress_log:
        sinks.append(ProgressLog(conf.log_level, root / "progress"))
    started = (lambda tl: on_run_id(tl.run.run_id)) if on_run_id else (lambda tl: None)
    out = execute_run(doc, sinks, lambda _: None, opts, started, root)
    if out.error:  # infrastructure loss or startup failure is raised, never returned
        if out.doc:
            out.error.answer = render_final(out.doc)
        raise out.error
    done = out.doc
    assert done is not None
    err = _error_of(done)
    return RunResult(
        done.run_id,
        done.status.value,
        render_final(done),
        err,
        done.model_dump(mode="json"),
        f"{conf.viewer_url}/runs/{done.run_id}",
    )


@dataclass(frozen=True)
class Outcome:
    """`execute_run` result: the run (None = startup failure) and the persistence/startup error."""

    doc: RunDocument | None
    error: BadkError | None = None


def execute_run(  # noqa: PLR0913 - the one run flow shared by the SDK, the CLI and serve
    run: RunDocument,
    sinks: list[Sink],
    say: _Say,
    opts: RunOptions,
    started: Callable[[TraceLog], object] = lambda tl: None,
    root: Path | None = None,
) -> Outcome:
    """Announce, execute, close sinks. `say` still gets the CLI text; `error` has run_id set."""
    errs: list[BadkError] = []
    say = _redacting(say)  # choke point: nothing printed can carry a secret
    tl = TraceLog(run, sinks, opts.settings.trace_enabled)
    children, token = proc.begin()
    try:
        if not _announce(tl, say, errs):
            return Outcome(None, errs[0])
        try:
            started(tl)
        except RunDeferred:  # design 22.8: no error record, no finalise; serve requeues
            raise
        except Exception as e:  # never leave the run RUNNING: ERROR, never executed
            return _start_failed(tl, say, e, errs)
        doc = _execute(tl, say, opts, root, errs)
        return Outcome(doc, errs[0] if errs else None)
    except BaseException as e:
        if not isinstance(e, Exception):  # SDK-3.5: Ctrl-C at any point leaves the run INCOMPLETE
            finalise_interrupted(tl, children)
        raise
    finally:
        proc.end(children, token)
        for sink in sinks:
            try:
                sink.close()
            except Exception as e:  # a close fault never masks the run result
                log.warning("sink close failed", error=str(e))


def _start_failed(tl: TraceLog, say: _Say, e: Exception, errs: list[BadkError]) -> Outcome:
    code, msg = _start_failure(e)
    if isinstance(e, PersistenceError):  # the sink IS the failure: never re-publish
        return Outcome(_publisher_down(tl, say, BadkError(code, msg), errs), errs[0])
    errs.append(_with_id(BadkError(code, msg), tl.run.run_id))
    return Outcome(_fail_run(tl, say, code, msg, errs), errs[0])


def _start_failure(e: Exception) -> tuple[Code, str]:
    if isinstance(e, AckFailed):  # D-3: the ack failed after "run created"
        return Code.QUEUE_UNAVAILABLE, f"run request ack failed: {e}"
    if isinstance(e, PersistenceError):  # design 22.8 step 3: acked, but the mark was lost
        return (
            Code.QUEUE_UNAVAILABLE,
            f"run request acked but the 'acked' mark was not persisted: {e}",
        )
    return Code.INTERNAL_ERROR, f"on_run_id failed: {e!r}"


def _redacting(say: _Say) -> _Say:
    return lambda text: say(redact(text))


def _with_id(e: BadkError, run_id: str) -> BadkError:
    e.run_id = run_id
    return e


def _announce(tl: TraceLog, say: _Say, errs: list[BadkError]) -> bool:
    try:
        tl.header_changed()  # event 1; only the persistence sink's error raises
    except Exception as e:  # design 6.2: startup failure, run_id suffix for traceability
        bad = e if isinstance(e, BadkError) else BadkError(Code.INTERNAL_ERROR, str(e))
        code, msg = bad.code, bad.message
        errs.append(_with_id(bad, tl.run.run_id))
        say(f"{code.value}: {msg} run_id={tl.run.run_id}\n")
        return False
    return True


def _execute(
    tl: TraceLog, say: _Say, opts: RunOptions, root: Path | None, errs: list[BadkError]
) -> RunDocument:
    try:
        run_coordinator(tl, opts, root)  # mutates the run only through tl, live
        if tl.run.ended_at is None:  # safety net, design 10.1: RUNNING at exit counts INCOMPLETE
            tl.header_changed(finalise)
    except Exception as e:  # design 10.1 / 11.1: run exists => ERROR INTERNAL_ERROR
        if isinstance(e, PersistenceError):
            return _publisher_down(tl, say, e, errs)  # the sink IS the failure: never re-publish
        return _fail_run(tl, say, Code.INTERNAL_ERROR, f"{type(e).__name__}: {e}", errs)
    return tl.run


def _fail_run(tl: TraceLog, say: _Say, code: Code, msg: str, errs: list[BadkError]) -> RunDocument:
    msg = redact(msg)  # choke point: nothing secret reaches the run doc or the viewer
    try:
        tl.append("coordinator", error_capsule(tl.run, code, msg), Entry(TraceType.ERROR), finalise)
    except BadkError as e2:
        if e2.code == Code.QUEUE_UNAVAILABLE:
            return _publisher_down(tl, say, e2, errs)
        errs.append(_with_id(BadkError(Code.INTERNAL_ERROR, redact(str(e2))), tl.run.run_id))
        say(f"{Code.INTERNAL_ERROR.value}: {e2} run_id={tl.run.run_id}\n")
    except Exception as e2:
        errs.append(_with_id(BadkError(Code.INTERNAL_ERROR, redact(str(e2))), tl.run.run_id))
        say(f"{Code.INTERNAL_ERROR.value}: {e2} run_id={tl.run.run_id}\n")
    return tl.run


def _publisher_down(tl: TraceLog, say: _Say, e: BadkError, errs: list[BadkError]) -> RunDocument:
    """Design 11.1: the persistence sink failed mid-run. Mark the run ERROR in memory only."""
    errs.append(_with_id(e, tl.run.run_id))
    fail_capsule(tl.run.agents["coordinator"], e.code, e.message)
    finalise(tl.run)
    say(f"{e.code.value}: {e.message} run_id={tl.run.run_id}\n")
    return tl.run
