"""MCP-1a: `type: mcp` tool field checks and `<tools>/mcp.json` (design 28.1)."""

import contextlib
import copy
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from badk.config import default_env_file, parse_env_file
from badk.errors import MIN_SECRET, BadkError, Code, register_secret

if TYPE_CHECKING:
    from badk.tools.definition import ToolDef

_SPECS: dict[str, dict[str, Any]] = {}  # process-wide, see start_check
_MCP_ONLY = ("server", "tool")
_NOT_MCP = ("command", "path", "request")
_REF = re.compile(r"\$(\$?)\{([^}]*)\}")  # `$${X}` stays a literal `${X}`


def _bad(where: str, why: str) -> BadkError:
    return BadkError(Code.VALIDATION_ERROR, f"{where}: {why}")


def _not_mcp(t: "ToolDef", f: str) -> None:
    from badk.tools.definition import NO_CWD  # noqa: PLC0415 - definition imports this module

    if t.type in NO_CWD and t.path_args:  # no workspace check on these types
        raise _bad(f, f"{sorted(t.path_args)[0]}: type path is not allowed on this tool type")
    if t.type != "mcp" and (bad := [k for k in (*_MCP_ONLY, "action") if k in t.model_fields_set]):
        raise _bad(f, f"{bad[0]} is only for type: mcp")


def check_fields(t: "ToolDef") -> None:
    """Load checks for the mcp fields (MCP-1.1, 1.9); VALIDATION_ERROR naming the file."""
    f = f"{t.name}.md"
    _not_mcp(t, f)
    if t.type != "mcp":
        return
    if bad := [k for k in (*_NOT_MCP, "env") if getattr(t, k)]:
        raise _bad(f, f"{bad[0]} is not allowed on an mcp tool")
    if bad := [k for k in _MCP_ONLY if getattr(t, k) == ""]:
        raise _bad(f, f"{bad[0]} pin is empty")
    if t.tool and not t.server:
        raise _bad(f, "tool needs a server pin")
    if t.action == "list" and t.tool:
        raise _bad(f, "action: list takes no tool")
    props = t.args.get("properties", {})
    need = [("server", t.server is None), ("tool", t.tool is None and t.action == "call")]
    if miss := [k for k, req in need if req and k not in props]:
        raise _bad(f, f"{miss[0]} is neither pinned nor in args.properties")


def _list_str(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _dict_str(v: object) -> bool:
    return isinstance(v, dict) and _list_str(list(v.values()))


def _spec_ok(spec: object) -> bool:
    """Exactly one of a non-empty `command` (args, env, cwd) or a non-empty `url` (headers)."""
    if not isinstance(spec, dict) or ("command" in spec) == ("url" in spec):
        return False
    if "url" in spec:
        return (
            bool(spec["url"])
            and isinstance(spec["url"], str)
            and _dict_str(spec.get("headers", {}))
        )
    return (
        bool(spec["command"])
        and isinstance(spec["command"], str)
        and _list_str(spec.get("args", []))
        and _dict_str(spec.get("env", {}))
        and isinstance(spec.get("cwd", ""), str)
    )


def _read(tools_dir: Path) -> dict[str, dict[str, Any]]:
    where = f"{tools_dir.name}/mcp.json"
    try:
        data = json.loads((tools_dir / "mcp.json").read_text())
    except (OSError, ValueError) as e:
        raise _bad(where, f"cannot read: {e}") from e
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise _bad(where, "needs an mcpServers object")
    if bad := [n for n, s in servers.items() if not _spec_ok(s)]:
        why = "a command (args: [str], env: {str: str}, cwd: str) or a url (headers: {str: str})"
        raise _bad(where, f"server {bad[0]!r} needs {why}, not both")
    return servers


def mask_values(tools_dir: Path) -> set[str]:
    """Each `<tools>/secrets.env` value in every form a toolkit reader may hold (raw after `=`,
    unquoted, the `.env` parse), for masking (MCP-2.4). Never refuses a start."""
    try:
        text = (tools_dir / "secrets.env").read_text()
    except (OSError, UnicodeDecodeError):
        return set()  # absent, or unreadable to badk and so to its tool children
    lines = [ln for ln in text.splitlines() if "=" in ln and not ln.lstrip().startswith("#")]
    raw = {ln.partition("=")[2].strip() for ln in lines}
    out = raw | {v[1:-1] for v in raw if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'"}
    with contextlib.suppress(BadkError):
        out |= set(parse_env_file(tools_dir / "secrets.env").values())
    return out


def _refs(o: object) -> set[str]:
    if isinstance(o, str):
        return {n for esc, n in _REF.findall(o) if not esc}
    if isinstance(o, dict):
        return {n for v in o.values() for n in _refs(v)}
    if isinstance(o, list):
        return {n for v in o for n in _refs(v)}
    return set()


def _fill(o: Any, vals: dict[str, str]) -> Any:  # noqa: ANN401 - walks any JSON value
    if isinstance(o, str):
        return _REF.sub(lambda m: "${" + m[2] + "}" if m[1] else vals[m[2]], o)
    if isinstance(o, dict):
        return {k: _fill(v, vals) for k, v in o.items()}
    if isinstance(o, list):
        return [_fill(v, vals) for v in o]
    return o


def _check_names(where: str, need: dict[str, set[str]]) -> None:
    """NAME follows the `env:` rules (MCP-2.2): the grammar, not BADK_, not an ENV_ALLOW key."""
    from badk.tools.definition import ENV_NAME, RESERVED  # noqa: PLC0415 - imports this module
    from badk.tools.execute import ENV_ALLOW  # noqa: PLC0415 - same

    for s, names in need.items():
        bad = [n for n in names if not re.fullmatch(ENV_NAME, n) or n.startswith(RESERVED)]
        if bad := sorted(bad + [n for n in names if n in ENV_ALLOW]):
            raise _bad(where, f"server {s!r}: ${{{bad[0]}}} is not a valid key name (env: rules)")


def _keys(where: str, need: dict[str, set[str]], tools: Path, env: Path | None) -> dict[str, str]:
    """Read every reachable `${NAME}` once per start: the first non-empty of the process env,
    `.env`, `<tools>/secrets.env` (MCP-2.1); each >= 8 chars, else VALIDATION_ERROR."""
    _check_names(where, need)
    if not (names := {n for ns in need.values() for n in ns}):
        return {}
    dot, kit = parse_env_file(env or default_env_file()), parse_env_file(tools / "secrets.env")
    vals = {n: next((src[n] for src in (os.environ, dot, kit) if src.get(n)), "") for n in names}
    for s, ns in need.items():
        if miss := [n for n in sorted(ns) if len(vals[n]) < MIN_SECRET]:
            why = f"key {miss[0]} is missing, empty or under {MIN_SECRET} chars"
            raise _bad(where, f"server {s!r}: {why}")
    for v in vals.values():
        register_secret(v)  # before any run
    return vals


def start_check(tools: Iterable["ToolDef"], tools_dir: Path, env_file: Path | None = None) -> None:
    """Only when a permitted tool is `type: mcp`: read and check mcp.json, check the pins, fill
    `${NAME}` keys in the reachable servers (MCP-2); the others are dropped unchecked."""
    if not (mcp := [t for t in tools if t.type == "mcp"]):
        return  # a start with no mcp tool leaves other starts' servers alone
    specs = _read(tools_dir)
    for t in mcp:
        if t.server and t.server not in specs:
            raise _bad(f"{tools_dir.name}/{t.name}.md", f"server {t.server!r} is not in mcp.json")
    where = f"{tools_dir.name}/mcp.json"
    names = set(specs) if any(not t.server for t in mcp) else {t.server for t in mcp if t.server}
    reach = {n: specs[n] for n in sorted(names)}
    vals = _keys(where, {n: _refs(s) for n, s in reach.items()}, tools_dir, env_file)
    root = (env_file or default_env_file()).resolve().parent  # the project: where its .env is
    got = {n: _rooted(where, n, _fill(s, vals), root) for n, s in reach.items()}
    # ponytail: replaced by each start that permits an mcp tool (a start with none keeps it): a
    # removed server is gone; two projects in one process get the last start's servers.
    global _SPECS  # noqa: PLW0603 - one rebind, so a concurrent reader never sees it half-built
    _SPECS = got


def _rooted(where: str, name: str, spec: dict[str, Any], root: Path) -> dict[str, Any]:
    """A stdio server starts in the project folder; a relative `cwd` starts there too."""
    if "command" in spec:
        spec["cwd"] = str(root / (spec.get("cwd") or ""))
        if not Path(spec["cwd"]).is_dir():  # MCP-1.11: a spec fault stops the start
            raise _bad(where, f"server {name!r}: cwd {spec['cwd']} is not a folder")
    return spec


def spec_for(server: str) -> dict[str, Any]:
    if server not in _SPECS:
        raise BadkError(
            Code.TOOL_ERROR, f"mcp server {server!r} is not in mcp.json: {sorted(_SPECS)}"
        )
    return copy.deepcopy(_SPECS[server])
