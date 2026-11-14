"""Tool executor (design 9.2): bash, python; http lives in tools/http.py."""

import copy
import errno
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import IO, Any, Final

import fastjsonschema

from badk.agents.definition import AgentDef
from badk.engine.envelope import ToolCall
from badk.errors import BadkError, Code, redact, redact_cut
from badk.proc import run_group
from badk.tools.definition import NO_CWD, VAR, ToolDef, load_tools
from badk.tools.guard import check, tool_cwd
from badk.tools.tool_env import mask, values_for

ENV_ALLOW: Final = (  # nothing else (DATABASE_URL, *_API_KEY, ...) leaks to tool children
    ("PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM")
    + ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
    + ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
)
RUN_IDS: ContextVar[tuple[str, str] | None] = ContextVar("badk_run_ids", default=None)
_EXEC_ERRNO: Final = (errno.ENOENT, errno.EACCES, errno.ENOEXEC)
STDERR_TAIL: Final = 2000  # chars of stderr kept in an error message
MAX_OUTPUT_BYTES: Final = 100_000  # per stream; step cuts again to the context window


def lookup(name: str, args: Mapping[str, Any]) -> object:
    """`{{name}}`'s JSON value; an undefined name is TOOL_ERROR (nothing runs)."""
    if name not in args:
        raise BadkError(Code.TOOL_ERROR, f"undefined template variable {{{{{name}}}}}")
    return args[name]


def to_text(label: str, v: object) -> str:
    """A placeholder value as text; NaN/Infinity are TOOL_ERROR on every path."""
    try:
        return v if isinstance(v, str) else json.dumps(v, allow_nan=False)
    except ValueError:
        raise BadkError(Code.TOOL_ERROR, f"{label}: non-finite number") from None


def render(template: str, args: Mapping[str, Any], quote: Callable[[str], str]) -> str:
    """Substitute `{{x}}`; an undefined name is TOOL_ERROR. Values pass `quote`."""

    def sub(m: re.Match[str]) -> str:
        v = lookup(m[1], args)
        return quote(to_text(m[0], v))

    return VAR.sub(sub, template)


def _render_token(tok: str, args: Mapping[str, Any]) -> str:
    """One argv element: plain-text substitution, so a value cannot become two elements or code.
    TOOL_ERROR: a NUL byte; a rendered token starting "-" when its template does not (option
    injection; `--n={{n}}` is fine)."""

    def sub(m: re.Match[str]) -> str:
        return to_text(m[0], lookup(m[1], args))

    out = VAR.sub(sub, tok)
    try:
        out.encode()
    except UnicodeEncodeError:
        raise BadkError(Code.TOOL_ERROR, f"invalid character in argument: {out[:40]!r}") from None
    if "\0" in out:
        raise BadkError(Code.TOOL_ERROR, f"NUL byte in argument: {out[:40]!r}")
    if out.startswith("-") and not tok.startswith("-"):
        raise BadkError(Code.TOOL_ERROR, f"argument may not start with '-': {out[:40]!r}")
    return out


@contextmanager
def bind_run(run_id: str, task_id: str) -> Iterator[None]:
    """Tool children started inside get BADK_RUN_ID / BADK_TASK_ID (TOOL-6). The outermost
    binding wins: a flow step's key beats the owner agent's key that make_deps would bind.
    A nested bind of another run is a bug (it would link external rows to the wrong run)."""
    if (cur := RUN_IDS.get()) is not None:
        if cur[0] != run_id:
            raise BadkError(Code.INTERNAL_ERROR, f"bind_run: run {run_id} inside run {cur[0]}")
        yield
        return
    token = RUN_IDS.set((run_id, task_id))
    try:
        yield
    finally:
        RUN_IDS.reset(token)


def _child_env(tool: ToolDef) -> dict[str, str]:
    """The one place a tool child's env is built: ENV_ALLOW, the keys the tool file lists now
    (TOOL-5; values from the start check), the run ids."""
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    vals = values_for(tool.name)
    if miss := [k for k in tool.env if k not in vals]:  # tool file edited after start
        raise BadkError(Code.TOOL_ERROR, f"{tool.name}: env {miss[0]} was not loaded at start")
    env |= {k: vals[k] for k in tool.env}
    if ids := RUN_IDS.get():
        env |= {"BADK_RUN_ID": ids[0], "BADK_TASK_ID": ids[1]}
    return env


def _run_group(
    tool: ToolDef, argv: list[str], stdin: IO[bytes] | int, cwd: Path
) -> tuple[int, str, str, bool]:
    """Run `argv` in its own session: (exit, stdout, stderr, truncated). Timeout kills the group."""
    env = _child_env(tool)
    try:
        code, so, se, cut = run_group(
            argv, stdin=stdin, cwd=cwd, timeout=tool.timeout, env=env, cap=MAX_OUTPUT_BYTES
        )
    except subprocess.TimeoutExpired:
        raise BadkError(Code.TOOL_TIMEOUT, f"{tool.name}: over {tool.timeout} s") from None
    except (OSError, ValueError) as e:
        prog = isinstance(e, OSError) and e.filename == argv[0] and e.errno in _EXEC_ERRNO
        why = f"cannot start {argv[0]!r}: {e}" if prog else f"spawn failed: {e}"
        raise BadkError(Code.TOOL_ERROR, f"{tool.name}: {why}") from e
    return (code, redact_cut(so), redact_cut(se), True) if cut else (code, so, se, False)


def _bash(tool: ToolDef, args: Mapping[str, Any], cwd: Path, root: Path) -> dict[str, Any]:
    argv = [_render_token(t, args) for t in tool.argv]  # no shell, no `bash -c`
    if "/" in argv[0] and not os.path.isabs(argv[0]):  # a tool file path is root-relative (WF-8)
        argv[0] = str(root / argv[0])
    code, so, se, cut = _run_group(tool, argv, subprocess.DEVNULL, cwd)
    return {"exit_code": code, "stdout": so, "stderr": se, "truncated": cut}


def _python(tool: ToolDef, args: Mapping[str, Any], cwd: Path, root: Path) -> dict[str, Any]:
    """Script gets args as JSON on stdin; stdout must be one JSON object."""
    payload = to_text(f"{tool.name} args", args).encode()
    with tempfile.TemporaryFile() as stdin:
        stdin.write(payload)
        stdin.seek(0)
        code, so, se, cut = _run_group(
            tool, [sys.executable, str(root / (tool.path or ""))], stdin, cwd
        )
    if code != 0:
        raise BadkError(Code.TOOL_ERROR, f"{tool.name}: exit {code}: {redact(se)[-STDERR_TAIL:]}")
    if cut:  # a cut JSON document is no document
        raise BadkError(Code.TOOL_ERROR, f"{tool.name}: output over {MAX_OUTPUT_BYTES} bytes")
    try:
        out = json.loads(so)
    except ValueError as e:
        raise BadkError(Code.TOOL_ERROR, f"{tool.name}: stdout is not JSON: {e}") from e
    if not isinstance(out, dict):
        raise BadkError(Code.TOOL_ERROR, f"{tool.name}: stdout JSON is not an object")
    return out


def execute(
    call: ToolCall,
    tool: ToolDef,
    agent: AgentDef,
    cwd: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """`_execute`, then every declared secret value (any tool's) becomes `***` in the result.
    Error text is masked by BadkError itself (start_check registers the values)."""
    out: dict[str, Any] = mask(_execute(call, tool, agent, cwd, root))
    return out


def _execute(
    call: ToolCall,
    tool: ToolDef,
    agent: AgentDef,
    cwd: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Validate args (fills schema defaults), then run. Nothing runs on a mismatch.
    guard.check validates too (it needs the final args for path checks); the repeat is
    deliberate defense in depth for direct callers. `cwd` is where the tool runs, `root` the
    project root that tool file paths resolve against (default `cwd`)."""
    try:
        tool.validate_args(call.args)
    except fastjsonschema.JsonSchemaValueException as e:
        raise BadkError(Code.TOOL_ERROR, f"{tool.name}: args invalid: {e.message}") from e
    if tool.type == "bash":
        return _bash(tool, call.args, cwd or Path.cwd(), root or cwd or Path.cwd())
    if tool.type == "python":
        return _python(tool, call.args, cwd or Path.cwd(), root or cwd or Path.cwd())
    if tool.type == "mcp":
        from badk.tools.mcp_pool import execute_mcp  # lazy: pulls in the mcp SDK

        return execute_mcp(call, tool)
    from badk.tools.http import execute_http  # http imports render from here

    return execute_http(call, tool)


def permitted_tools(agent: AgentDef, directory: Path) -> dict[str, ToolDef]:
    return load_tools(directory, agent.permissions)


def run_tool(
    tools: Mapping[str, ToolDef],
    agent: AgentDef,
    call: ToolCall,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """StepDeps.run_tool over the snapshot `make_deps` loaded: guard.check (TOOL-3), then execute.
    Step stays pure (effects only via StepDeps); the guard sits here, ahead of every execute.
    The guard rewrites path args on a copy, so the trace keeps the model's call."""
    c = call.model_copy(update={"args": copy.deepcopy(call.args)})
    root = cwd or Path.cwd()
    tool = tools.get(c.name)
    where = tool_cwd(agent, root) if tool is None or tool.type not in NO_CWD else None
    tool = check(agent, c, tool, root, where)  # NO_CWD tools have no cwd (guard skips them)
    return execute(c, tool, agent, where, root)
