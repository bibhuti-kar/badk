"""Tool definition files (design 9.1): `tools/<name>.md`, one per tool."""

import copy
import json
import re
import shlex
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, get_args

import fastjsonschema
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    PrivateAttr,
    StringConstraints,
    ValidationError,
    field_validator,
)

from badk.agents.definition import _split
from badk.errors import BadkError, Code
from badk.tools import mcp_config
from badk.tools.schema_ref import remote_ref

if TYPE_CHECKING:
    from badk.agents.definition import AgentDef

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
ToolType = Literal["bash", "python", "http", "mcp"]
NO_CWD: Final = ("http", "mcp")  # tool types with no cwd: no workspace or path-arg check
_TYPES = get_args(ToolType)
VAR: Final = re.compile(r"\{\{(\w+)\}\}")  # the one placeholder grammar (bash, http)
_OPS = frozenset("();<>|&")
_HINT = (
    "bash tools run without a shell; put pipelines, redirects and shell logic in a script file "
    "the tool calls (e.g. sh scripts/x.sh {{a}}, or a script with a shebang and the exec bit) "
    "or use a python tool"
)


def _check_operators(name: str, command: str) -> None:
    """Refuse an UNQUOTED shell operator (a quoted one is plain argv text)."""
    quote, i = "", 0
    command = command.strip()  # a YAML block scalar ends in a newline
    while i < len(command):
        c = command[i]
        if c == "\\" and quote != "'":
            i += 1  # escaped char
        elif quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "\n":
            raise BadkError(
                Code.VALIDATION_ERROR,
                f"{name}: unquoted newline; one command per tool, or put several in a script",
            )
        elif c in _OPS:
            raise BadkError(Code.VALIDATION_ERROR, f"{name}: shell operator {c!r}; {_HINT}")
        i += 1


def _check_token(name: str, tok: str) -> None:
    rest = VAR.sub("", tok)
    if "{{" in rest or "}}" in rest:
        raise BadkError(Code.VALIDATION_ERROR, f"{name}: {tok!r} holds an invalid placeholder")


def split_command(name: str, command: str) -> list[str]:
    """`type: bash` is an argv command, no shell (user decision 2026-11-10: argv only). Split
    once at load; unquoted shell operators are refused. No expansion: `$HOME`, globs, `~` and
    backticks reach the program literally."""
    _check_operators(name, command)
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        raise BadkError(Code.VALIDATION_ERROR, f"{name}: bad command: {e}") from e
    for tok in tokens:
        _check_token(name, tok)
    if not tokens or VAR.search(tokens[0]):
        raise BadkError(Code.VALIDATION_ERROR, f"{name}: command needs a fixed program name")
    return tokens


def _compile(name: str, key: str, schema: dict[str, Any]) -> Callable[[Any], Any]:
    """Any compile failure is VALIDATION_ERROR naming the tool. `output` refuses a remote
    `$ref` (fastjsonschema would fetch it at every load). ponytail: `args` still allows one;
    pending item for the security audit."""
    if key == "output" and (ref := remote_ref(schema)):
        raise BadkError(Code.VALIDATION_ERROR, f"{name}: bad output schema: remote $ref {ref!r}")
    try:
        return fastjsonschema.compile(schema)
    except Exception as e:  # JsonSchemaDefinitionException, or TypeError on malformed shapes
        raise BadkError(Code.VALIDATION_ERROR, f"{name}: bad {key} schema: {e}") from e


ENV_NAME: Final = r"^[A-Z][A-Z0-9_]*$"  # TOOL-5; also the `${NAME}` grammar of mcp.json
RESERVED: Final = "BADK_"  # TOOL-6 owns the prefix
EnvName = Annotated[str, StringConstraints(pattern=ENV_NAME)]
ENV_REF: Final = re.compile(r"\{\{env\.(\w+)\}\}")  # TOOL-5: a secret, in an http header value


def _check_env_refs(t: "ToolDef") -> None:
    """TOOL-5: `{{env.NAME}}` only in a header value string, and the tool's `env` lists NAME."""
    heads = (t.request or {}).get("headers")
    vals = [v for v in heads.values() if isinstance(v, str)] if isinstance(heads, dict) else []
    refs = [n for v in vals for n in ENV_REF.findall(v)]
    if json.dumps(t.request, default=str).count("{{env.") != len(refs):
        raise BadkError(Code.VALIDATION_ERROR, f"{t.name}.md: {{{{env.NAME}}}} only in a header")
    if bad := [n for n in refs if n not in t.env]:
        msg = f"{t.name}.md: header uses {{{{env.{bad[0]}}}}} but env does not list {bad[0]}"
        raise BadkError(Code.VALIDATION_ERROR, msg)
    u = urllib.parse.urlsplit(str((t.request or {}).get("url", "")))
    if refs and (u.scheme not in ("http", "https") or not u.netloc or "{" in u.netloc):
        msg = f"{t.name}.md: a tool with {{{{env.*}}}} needs a fixed http(s) host in url"
        raise BadkError(Code.VALIDATION_ERROR, msg)  # the value goes to that host only


class ToolDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: ToolType
    description: str
    command: str | None = None
    path: str | None = None
    request: dict[str, Any] | None = None
    server: str | None = None  # mcp: the pinned server
    tool: str | None = None  # mcp: the pinned MCP tool
    action: Literal["call", "list"] = "call"  # mcp
    args: dict[str, Any]
    output: dict[str, Any] | None = None  # optional schema for a flow step's `body` (FLOW-9)
    path_args: frozenset[str] = frozenset()
    timeout: int = 30
    env: tuple[EnvName, ...] = ()  # TOOL-5: the .env keys this tool's child (or header) gets
    allow_private_network: bool = False  # http: permit loopback/private/link-local targets
    _validator: Callable[[Any], Any] = PrivateAttr()
    _out: Callable[[Any], Any] | None = PrivateAttr(default=None)
    _argv: list[str] = PrivateAttr(default_factory=list)  # bash: one template per argv element

    @field_validator("env")
    @classmethod
    def _no_badk_env(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if bad := [k for k in v if k.startswith(RESERVED)]:
            raise ValueError(f"{bad[0]}: {RESERVED} is reserved")
        return v

    def model_post_init(self, _ctx: object) -> None:
        if self.type == "bash":  # every way a ToolDef is built is checked
            self._argv = split_command(self.name, self.command or "")
        _check_env_refs(self)
        mcp_config.check_fields(self)
        self._validator = _compile(self.name, "args", self.args)
        if self.output is not None:
            self._out = _compile(self.name, "output", self.output)

    @property
    def argv(self) -> list[str]:
        return self._argv

    def validate_args(self, value: object) -> None:
        """Raises fastjsonschema.JsonSchemaValueException on mismatch."""
        self._validator(value)

    def validate_output(self, body: object) -> None:
        """No-op without an `output` schema; else raises JsonSchemaValueException."""
        if self._out is not None:
            self._out(body)


def _compile_paths(data: dict[str, Any]) -> None:
    """`"type": "path"` properties become `"string"`; their names go to path_args."""
    schema = copy.deepcopy(data.get("args") or {"type": "object"})
    props = schema.get("properties", {}) if isinstance(schema, dict) else None
    if not isinstance(schema, dict) or not isinstance(props, dict):
        raise BadkError(Code.VALIDATION_ERROR, f"{data.get('name')}: args must be an object schema")
    paths = set()
    for key, spec in props.items():
        if isinstance(spec, dict) and spec.get("type") == "path":
            spec["type"] = "string"
            paths.add(key)
    data["args"], data["path_args"] = schema, frozenset(paths)


def load_tool(path: Path) -> ToolDef:
    try:
        text = path.read_text()
    except OSError as e:
        raise BadkError(Code.VALIDATION_ERROR, f"cannot read tool file {path}: {e}") from e
    front, _ = _split(text, "tool")
    try:
        data = yaml.safe_load(front)
    except yaml.YAMLError as e:
        raise BadkError(Code.VALIDATION_ERROR, f"{path.name}: bad YAML: {e}") from e
    if not isinstance(data, dict):
        raise BadkError(Code.VALIDATION_ERROR, f"{path.name}: frontmatter must be a mapping")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME.match(name) or name != path.stem:
        raise BadkError(Code.VALIDATION_ERROR, f"{path.name}: name {name!r} must equal the stem")
    if data.get("type") not in _TYPES:
        raise BadkError(
            Code.TOOL_TYPE_INVALID, f"{path.name}: type {data.get('type')!r} not in {_TYPES}"
        )
    _compile_paths(data)
    try:
        return ToolDef.model_validate(data)
    except ValidationError as e:
        keys = ", ".join(str(err["loc"][0]) if err["loc"] else "?" for err in e.errors())
        raise BadkError(Code.VALIDATION_ERROR, f"{path.name}: invalid: {keys}") from e


def load_tools(directory: Path, names: Iterable[str]) -> dict[str, ToolDef]:
    """Only `names` (an agent's permissions) that have a file; "_" templates, unknown names skip."""
    found = (directory / f"{n}.md" for n in sorted(set(names)) if n[:1] != "_")
    tools = (load_tool(p) for p in found if p.is_file())
    return {t.name: t for t in tools}


def tools_dir(cwd: Path | None) -> Path:
    """The run's tool files: `<run cwd>/tools` (process cwd when the run has none)."""
    return (cwd or Path.cwd()) / "tools"


def validate_all(directory: Path) -> None:
    """Startup check (design 5.3 exit 1): every non-template file must load."""
    load_tools(directory, (p.stem for p in directory.glob("*.md")))  # skips "_" templates


def check_permission_names(
    agents: Mapping[str, "AgentDef"], origin: Mapping[str, Path], tools: Path
) -> None:
    """NC-72: every permissions name is a non-template tool file in `tools`."""
    for a in agents.values():
        for n in a.permissions:
            if n[:1] == "_":
                why = "starts with '_' (a template, never a tool)"
            elif not _NAME.match(n):  # no "/", "..", upper case: never a path outside `tools`
                why = "is not a tool name ([a-z][a-z0-9_]*)"
            elif not (tools / f"{n}.md").is_file():
                why = f"has no tool file {tools.name}/{n}.md"
            else:
                continue
            msg = f"{origin[a.name]}: permissions name {n!r} {why}"
            raise BadkError(Code.VALIDATION_ERROR, msg)
