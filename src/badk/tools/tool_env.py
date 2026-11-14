"""TOOL-5: secrets a tool declares with `env: [NAME]`. Read once per start, never per call."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from badk.agents.definition import AgentDef
from badk.config import read_env_keys
from badk.errors import MIN_SECRET, BadkError, Code, redact, register_secret
from badk.tools import mcp_config
from badk.tools.definition import ENV_REF, ToolDef, load_tools

# ponytail: process-wide by tool name, replaced whole by the last start. One process that
# runs two projects with a same-named keyed tool gives project A's tool B's value (sent to
# A's host). Multi-project use is deferred (user, M7 Brake 2); key by project root then.
_BY_TOOL: dict[str, dict[str, str]] = {}


def _check(tool: ToolDef, vals: Mapping[str, str], where: str) -> None:
    from badk.tools.execute import ENV_ALLOW  # noqa: PLC0415 - execute imports this module

    for key in tool.env:
        if key in ENV_ALLOW:
            why = f"env {key}: every tool child gets it already; it is no secret"
        elif len(vals.get(key, "")) < MIN_SECRET:
            why = f"env {key} is missing, empty or under {MIN_SECRET} chars"
        else:
            continue
        raise BadkError(Code.VALIDATION_ERROR, f"{where}/{tool.name}.md: {why}")


def start_check(agents: Mapping[str, AgentDef], tools: Path, env_file: Path | None) -> None:
    """Every key a permitted tool declares is present and >= 8 chars, else VALIDATION_ERROR."""
    loaded = load_tools(tools, {n for a in agents.values() for n in a.permissions}).values()
    mcp_config.start_check(loaded, tools, env_file)  # MCP-1a/2: mcp.json, pins, keys
    for v in mcp_config.mask_values(tools):
        register_secret(v)  # MCP-2.4: toolkit keys, even with no mcp tool permitted
    keyed = [t for t in loaded if t.env]
    vals = read_env_keys({k for t in keyed for k in t.env}, env_file) if keyed else {}
    for t in keyed:
        _check(t, vals, tools.name)
    for v in vals.values():
        register_secret(v)  # before any child can get it
    global _BY_TOOL  # noqa: PLW0603 - one rebind, so a concurrent reader never sees it half-built
    _BY_TOOL = {t.name: {k: vals[k] for k in t.env} for t in keyed}


def values_for(tool: str) -> dict[str, str]:
    return dict(_BY_TOOL.get(tool, {}))


def fill(tool: str, text: str, rest: Callable[[str], str]) -> str:
    """`{{env.NAME}}` takes the tool's value; `rest` renders the text between. One pass, so
    neither a value nor a rendered model arg is rendered again (TOOL-5.6)."""
    parts = ENV_REF.split(text)  # text, name, text, ..., text
    vals = _BY_TOOL.get(tool, {})
    if missing := [p for p in parts[1::2] if p not in vals]:
        raise BadkError(Code.TOOL_ERROR, f"{tool}: env {missing[0]} was not loaded at start")
    return "".join(vals[p] if i % 2 else rest(p) for i, p in enumerate(parts))


def mask(o: Any) -> Any:  # noqa: ANN401 - walks any JSON value
    """Every registered secret (any tool's, the LLM key) becomes `***` in every string, keys
    too. A byte cap's cut edge is masked where the cap cuts (`errors.redact_cut`)."""
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, dict):
        return {mask(k): mask(v) for k, v in o.items()}
    if isinstance(o, list):
        return [mask(v) for v in o]
    return o
