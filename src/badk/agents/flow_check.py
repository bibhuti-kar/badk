"""Cross-file flow checks at every start (design 25.3; FLOW-2)."""

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from badk.agents.definition import closure
from badk.agents.flow import CONTEXT, Step, ancestors, is_coordinator
from badk.errors import BadkError, Code
from badk.flow.template import refs
from badk.tools.definition import load_tool

if TYPE_CHECKING:
    from badk.agents.definition import AgentDef

FLOW_MAX_DEPTH: Final = 4  # PM-40: agents with a flow on one nesting path


def _bad(where: Path, step: Step, why: str) -> BadkError:
    return BadkError(Code.VALIDATION_ERROR, f"{where}: flow step {step.id!r}: {why}")


_SCALARS: Final = {"string", "integer", "number", "boolean", "null"}
_UNDECIDED: Final = {
    "$ref", "anyOf", "oneOf", "allOf", "not", "if", "then", "else",
    "dependentSchemas", "unevaluatedProperties",
}  # fmt: skip


def _scalar(schema: dict[str, Any]) -> bool:
    t = schema.get("type")
    return t is not None and set(t if isinstance(t, list) else [t]) <= _SCALARS


def _key_path(schema: dict[str, Any], seg: str, rest: list[str]) -> bool:
    props = schema.get("properties") or {}
    if seg in props:
        return _has_path(props[seg], rest)
    for pat, sub in (schema.get("patternProperties") or {}).items():
        if re.search(pat, seg):
            return _has_path(sub, rest)
    extra = schema.get("additionalProperties")
    return extra is True or (isinstance(extra, dict) and _has_path(extra, rest))


def _has_path(schema: object, segs: list[str]) -> bool:
    """Can `segs` exist under `schema`? Reading (design 25.15 #21): `properties` without
    `additionalProperties` means the listed names are the reference surface, so a typo fails
    at load. ponytail: keywords in _UNDECIDED, and nodes with no properties/items, cannot be
    decided here, so they accept; add real resolution if needed."""
    if schema is False:
        return False
    if not segs or not isinstance(schema, dict) or _UNDECIDED & schema.keys():
        return True
    if _scalar(schema):
        return False
    seg, rest = segs[0], segs[1:]
    if {"properties", "patternProperties", "additionalProperties"} & schema.keys():
        return _key_path(schema, seg, rest)
    if "items" in schema or schema.get("type") == "array":
        return seg.isdigit() and _has_path(schema.get("items"), rest)
    return True


SchemaOf = Callable[[str], dict[str, Any] | None]


def _check_paths(s: Step, where: Path, used: list[str], schema_of: SchemaOf) -> None:
    for r in used:
        step, _, path = r.partition(".")
        segs = path.split(".")
        if segs[0] == "body" and (schema := schema_of(step)) is not None:
            if not _has_path(schema, segs[1:]):
                raise _bad(where, s, f"'{{{{{r}}}}}': not in the output schema of {step!r}")


def _check_refs(s: Step, where: Path, before: frozenset[str], schema_of: SchemaOf) -> None:
    try:
        used = refs(s.args)
    except ValueError as e:
        raise _bad(where, s, str(e)) from None
    for r in used:
        if r not in CONTEXT and r.split(".")[0] not in before:
            raise _bad(where, s, f"'{{{{{r}}}}}': names a step that is not an ancestor")
    _check_paths(s, where, used, schema_of)


def _check_tool(a: "AgentDef", s: Step, where: Path, tools: Path | None) -> None:
    # tool file and '_' names are already proven by check_permission_names (NC-72)
    n = s.tool or ""
    if tools is None:
        raise _bad(where, s, f"tool {n!r}: tool steps need the tools directory")
    if n not in a.permissions:
        raise _bad(where, s, f"tool {n!r} is not in permissions")


def _check_agent_name(agents: "Mapping[str, AgentDef]", s: Step, where: Path, n: str) -> None:
    if n not in agents:
        raise _bad(where, s, f"names {n!r}, which has no agent file")
    if is_coordinator(n):
        raise _bad(where, s, f"names {n!r}, which is a coordinator")


def _check_select(agents: "Mapping[str, AgentDef]", s: Step, where: Path) -> None:
    names = s.select or []
    if not names:
        raise _bad(where, s, "select list is empty")
    for n in names:
        _check_agent_name(agents, s, where, n)
    if dup := next((n for n in names if names.count(n) > 1), None):
        raise _bad(where, s, f"select lists {dup!r} twice")
    if out := sorted(closure(dict(agents), names) - set(names)):
        raise _bad(where, s, f"select needs {out[0]!r} (depends_on closure), not in the list")


def _edges(a: "AgentDef") -> list[str]:
    return [n for s in a.flow for n in ([s.agent] if s.agent else s.select or [])]


def _too_deep(path: list[str], n: str, height: dict[str, int], best: dict[str, str]) -> None:
    """`path` is above `n`; the longest flow chain through `n` is len(path) + height[n]."""
    if len(path) + height[n] > FLOW_MAX_DEPTH:
        chain = [*path, n]
        while chain[-1] in best:
            chain.append(best[chain[-1]])
        raise BadkError(
            Code.VALIDATION_ERROR,
            f"flow nesting deeper than limit {FLOW_MAX_DEPTH}: {' -> '.join(chain)}",
        )


def _kids(agents: "Mapping[str, AgentDef]", n: str) -> list[str]:
    return [k for k in _edges(agents[n]) if agents[k].flow]


def _walk(agents: "Mapping[str, AgentDef]") -> None:
    """One DFS over flow edges (like definition._check_cycles): cycle, then depth (PM-40).

    `height[n]` = flow agents on the longest chain starting at n; `best[n]` its next node."""
    height: dict[str, int] = {}
    best: dict[str, str] = {}
    for root in agents:
        if root in height or not agents[root].flow:
            continue
        path, its = [root], [iter(_kids(agents, root))]
        while path:
            n = next(its[-1], None)
            if n is None:
                top = path.pop()
                its.pop()
                if kid := max(_kids(agents, top), key=lambda k: height[k], default=None):
                    best[top] = kid
                height[top] = 1 + (height[kid] if kid else 0)
                _too_deep(path, top, height, best)
            elif n in path:
                raise BadkError(
                    Code.VALIDATION_ERROR,
                    f"flow cycle: {' -> '.join([*path[path.index(n) :], n])}",
                )
            elif n in height:
                _too_deep(path, n, height, best)
            else:
                path.append(n)
                its.append(iter(_kids(agents, n)))


def _schema_reader(
    a: "AgentDef", tools: Path | None, cache: dict[Path, dict[str, Any] | None]
) -> SchemaOf:
    """step id -> its tool's `output` schema. One cache serves every agent in a check_flows
    call. A tool file that fails to load raises its own error (never hidden)."""
    files = {s.id: tools / f"{s.tool}.md" for s in a.flow if s.tool and tools}

    def schema_of(step: str) -> dict[str, Any] | None:
        f = files.get(step)
        if f is None or not f.is_file():
            return None  # not a tool step; a missing file is _check_tool's refusal
        if f not in cache:
            cache[f] = load_tool(f).output
        return cache[f]

    return schema_of


def check_flows(
    agents: "Mapping[str, AgentDef]", origin: Mapping[str, Path], tools: Path | None
) -> None:
    """NC-76..NC-79. Raises VALIDATION_ERROR naming file, step id and name; else returns."""
    cache: dict[Path, dict[str, Any] | None] = {}
    for a in agents.values():
        before = ancestors(a.flow)
        schema_of = _schema_reader(a, tools, cache)
        for s in a.flow:
            where = origin[a.name]
            if s.tool is not None:
                _check_tool(a, s, where, tools)
                _check_refs(s, where, before[s.id], schema_of)
            elif s.agent is not None:
                _check_agent_name(agents, s, where, s.agent)
            else:
                _check_select(agents, s, where)
    _walk(agents)
