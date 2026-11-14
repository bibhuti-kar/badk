"""Agent definition file loader (design 4.2, 4.3)."""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import fastjsonschema
import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from badk.agents.flow import Step, check_fields, final_default
from badk.agents.flow import is_coordinator as is_coordinator
from badk.agents.route import Route, resolve_route
from badk.config import Settings
from badk.engine.tokens import estimate_tokens
from badk.errors import BadkError, Code

_NAME = re.compile(r"^(coordinator(-[a-z0-9][a-z0-9-]*)?|sub-[a-z0-9][a-z0-9-]*)$")


_COORD_ONLY = ("agents", "always", "max_parallel")
_SUB_ONLY = ("use_when", "depends_on")
_MAX_FRONTMATTER_TOKENS = 500


class AgentDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    provider: Literal["openai", "anthropic", "claude_cli", "fake"] | None = None
    model: str | None = None
    route: Route | None = None  # derived by resolve_route, not a frontmatter key
    context_window: int = Field(ge=256)
    compression_threshold: float = Field(0.75, gt=0, le=1)
    token_budget: int = Field(ge=1)
    time_budget_sec: int = Field(ge=1)
    max_tries: int = Field(ge=1)
    permissions: list[str] = []
    workspace: list[str] = []
    agents: list[str] = []
    always: list[str] = []
    use_when: str = ""
    depends_on: list[str] = []
    catalog: str = ""  # derived by load_agents (WF-3), not a frontmatter key
    selection_schema: dict[str, Any] = {}
    output_schema: dict[str, Any] = {}
    max_parallel: int | None = Field(None, ge=1)
    flow: list[Step] = []
    final: Literal["model", "none"] | None = None
    role_instruction: str = ""
    _validator: Callable[[Any], Any] = PrivateAttr()

    _final_default = model_validator(mode="before")(staticmethod(final_default))

    def model_post_init(self, _ctx: object) -> None:
        try:
            self._validator = fastjsonschema.compile(self.output_schema)
        except fastjsonschema.JsonSchemaDefinitionException as e:
            raise BadkError(Code.VALIDATION_ERROR, f"bad output_schema: {e}") from e

    def validate_output(self, value: object) -> None:
        """Raises fastjsonschema.JsonSchemaValueException on mismatch."""
        self._validator(value)


def _split(text: str, label: str = "agent") -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise BadkError(Code.VALIDATION_ERROR, f"{label} file must start with '---' frontmatter")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1 :])
    raise BadkError(Code.VALIDATION_ERROR, f"{label} file frontmatter is not closed with '---'")


def _no_derived(data: dict[str, Any], who: str) -> None:
    if derived := data.keys() & {
        "catalog",
        "selection_schema",
        "route",
    }:  # derived by load_agents (WF-3)
        raise BadkError(Code.AGENT_FIELD_NOT_ALLOWED, f"{who}: {sorted(derived)} are derived")


def _check_name(data: dict[str, Any], path: Path) -> None:
    name = data.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise BadkError(
            Code.AGENT_NAME_INVALID,
            f"agent name {name!r} ({path}): need coordinator[-x] or sub-<name>",
        )
    _no_derived(data, f"{name} ({path})")
    check_fields(data, path)
    coord = is_coordinator(name)
    for key in _SUB_ONLY if coord else _COORD_ONLY:
        if key in data and not (key == "max_parallel" and data.get("flow")):
            kind = "sub-agent" if coord else "coordinator"
            raise BadkError(Code.AGENT_FIELD_NOT_ALLOWED, f"{name} ({path}): {key} is {kind} only")
    if coord:
        data.setdefault("max_parallel", 1)
    elif not isinstance(data.get("use_when"), str) or not data["use_when"].strip():
        raise BadkError(Code.VALIDATION_ERROR, f"{name} ({path}): use_when is required")
    elif len(data["use_when"].strip().splitlines()) != 1:
        raise BadkError(Code.VALIDATION_ERROR, f"{name} ({path}): use_when must be one line")
    ws = data.get("workspace")
    if not isinstance(ws, list) or not ws:
        raise BadkError(Code.VALIDATION_ERROR, f"{name} ({path}): workspace is required")


def _bad(msg: str) -> BadkError:
    return BadkError(Code.VALIDATION_ERROR, msg)


def _check_names(agents: dict[str, AgentDef], coords: list[AgentDef]) -> None:
    refs = [(c.name, "agents", n) for c in coords for n in c.agents]
    refs += [(c.name, "always", n) for c in coords for n in c.always]
    refs += [(a.name, "depends_on", n) for a in agents.values() for n in a.depends_on]
    for owner, field, n in refs:
        if n not in agents:
            raise _bad(f"{owner}: {field} names {n!r}, which has no agent file")
        if is_coordinator(n):
            raise _bad(f"{owner}: {field} names {n!r}, which is not a sub-* agent")
    for c in coords:
        for n in c.always:
            if n not in c.agents:
                raise _bad(f"{c.name}: always names {n!r}, which is not in agents")


def closure(agents: dict[str, AgentDef], names: list[str]) -> set[str]:
    """`names` plus everything they transitively `depends_on`."""
    todo, seen = list(names), set[str]()
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo += agents[n].depends_on
    return seen


def _check_closure(agents: dict[str, AgentDef], c: AgentDef) -> None:
    extra = sorted(closure(agents, c.agents) - set(c.agents))
    if extra:
        raise _bad(f"{c.name}: depends_on closure needs {extra}, which are not in agents")


def _check_cycles(agents: dict[str, AgentDef]) -> None:
    """Iterative DFS with colour marks: `on` = grey (current path), `done` = black."""
    done: set[str] = set()
    for root in agents:
        if root in done:
            continue
        path, on = [root], {root}
        its = [iter(agents[root].depends_on)]
        while path:
            n = next(its[-1], None)
            if n is None:
                last = path.pop()
                on.discard(last)
                done.add(last)
                its.pop()
            elif n in on:
                raise _bad("depends_on cycle: " + " -> ".join([*path[path.index(n) :], n]))
            elif n not in done:
                path.append(n)
                on.add(n)
                its.append(iter(agents[n].depends_on))


def _cross_check(
    agents: dict[str, AgentDef], origin: dict[str, Path], settings: Settings, tools: Path | None
) -> None:
    coords = [a for a in agents.values() if is_coordinator(a.name)]
    _check_names(agents, coords)
    for c in coords:
        _check_closure(agents, c)
    _check_cycles(agents)
    from badk.agents.time_check import check_time_budgets  # noqa: PLC0415 - imports this module

    check_time_budgets(agents, origin, settings.provider_timeout_sec)
    if tools is not None:
        from badk.tools.definition import check_permission_names  # noqa: PLC0415 - imports this module

        check_permission_names(agents, origin, tools)
    from badk.agents.flow_check import check_flows  # noqa: PLC0415 - imports this module

    check_flows(agents, origin, tools)
    from badk.agents.flow_time import check_flow_budgets  # noqa: PLC0415 - imports this module

    check_flow_budgets(agents, origin, settings.provider_timeout_sec, tools)


def load_agents(
    directory: Path, settings: Settings, *, tools: Path | None = None, env_file: Path | None = None
) -> dict[str, AgentDef]:
    """Every `*.md` in `directory` except those starting "_" (design 4.3), keyed by name.

    Cross-file checks (design 20.2): a bad graph raises and nothing is returned. With `tools`,
    every `permissions` name must be a tool file there (NC-72).
    """
    if not directory.is_dir():
        raise BadkError(Code.AGENT_NOT_FOUND, f"agents directory not found: {directory}")
    loaded: dict[str, AgentDef] = {}
    origin: dict[str, Path] = {}
    for p in sorted(directory.glob("*.md")):
        if p.name[0] == "_":
            continue
        a = load_agent(p, settings)
        if a.name in loaded:
            raise _bad(f"duplicate agent name {a.name!r}: {origin[a.name]} and {p}")
        loaded[a.name], origin[a.name] = a, p
    _cross_check(loaded, origin, settings, tools)
    from badk.coordinator.select import with_catalog  # noqa: PLC0415 - select imports this module
    from badk.tools.tool_env import start_check  # noqa: PLC0415 - imports this module

    if tools is not None:
        start_check(loaded, tools, env_file)  # TOOL-5: declared secrets present, registered
    return {n: with_catalog(a, loaded) if is_coordinator(n) else a for n, a in loaded.items()}


def load_agent(path: Path, settings: Settings) -> AgentDef:
    try:
        text = path.read_text()
    except OSError as e:
        raise BadkError(Code.AGENT_NOT_FOUND, f"cannot read agent file {path}: {e}") from e
    front, body = _split(text)
    if estimate_tokens(front) > _MAX_FRONTMATTER_TOKENS:
        raise BadkError(
            Code.AGENT_FRONTMATTER_TOO_LARGE,
            f"{path.name}: frontmatter over {_MAX_FRONTMATTER_TOKENS} tokens",
        )
    try:
        data = yaml.safe_load(front)
    except yaml.YAMLError as e:
        raise BadkError(Code.VALIDATION_ERROR, f"{path.name}: bad YAML: {e}") from e
    if not isinstance(data, dict):
        raise BadkError(Code.VALIDATION_ERROR, f"{path.name}: frontmatter must be a mapping")
    if "role_instruction" in data:
        raise BadkError(Code.VALIDATION_ERROR, f"{path.name}: role_instruction is the body")
    _check_name(data, path)
    resolve_route(data, settings, path)
    data["role_instruction"] = body
    try:
        agent = AgentDef.model_validate(data)
    except ValidationError as e:
        keys = ", ".join(str(err["loc"][0]) if err["loc"] else "?" for err in e.errors())
        raise BadkError(Code.VALIDATION_ERROR, f"{path.name}: invalid: {keys}") from e
    return agent
