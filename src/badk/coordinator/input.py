"""Run input parsing (design 10.2)."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from badk.agents.definition import AgentDef
from badk.errors import BadkError, Code
from badk.tools.guard import check_path, read_bounded

_PIN = re.compile(r"(?<!\S)@(file|url):(\S+)")
_PIN_LIKE = re.compile(r"@[a-z]+:\S+")  # any `@kind:ref` word is text, even a removed kind
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_TRAIL = ".,;:!?)]}'\""
_SLASH = re.compile(r"/[A-Za-z][A-Za-z0-9_-]*(?=\s|$)")


def is_file_arg(arg: str) -> bool:
    """Design 6.2 one-word rule: True when the argument names a file (it must then exist)."""
    if any(c.isspace() for c in arg):
        return False
    if Path(arg).is_file():
        return True
    if _SLASH.fullmatch(arg.rstrip(_TRAIL)) or _URL.match(arg) or _PIN_LIKE.fullmatch(arg):
        return False
    return "/" in arg or arg.endswith((".md", ".txt"))


@dataclass(frozen=True)
class Pin:
    kind: Literal["file", "url"]
    ref: str
    content: str | None = None


def parse_slash(text: str) -> str | None:
    """Leading `/word` token, slash included; BOM and leading whitespace are skipped."""
    m = _SLASH.match(text.lstrip("﻿").lstrip())
    return m.group(0) if m else None


def pin_strings(text: str) -> list[str]:
    """`<kind>:<ref>` per annotation; pure text scan, no I/O (stored in input.pins)."""
    found = ((k, r.rstrip(_TRAIL)) for k, r in _PIN.findall(text))
    return [f"{k}:{r}" for k, r in found if r]


def coordinator_for(slash: str | None, agents: Mapping[str, AgentDef]) -> str:
    """WF-2: leading `/name` selects `coordinator-<name>`, else `coordinator`; a name that is not
    a loaded file (unknown, malformed, a sub-agent) is AGENT_NOT_FOUND naming the file."""
    name = f"coordinator-{slash[1:]}" if slash else "coordinator"
    if name not in agents:
        raise BadkError(Code.AGENT_NOT_FOUND, f"workflow agent not found: agents/{name}.md")
    return name


def budgets(a: AgentDef) -> dict[str, Any]:
    return {
        "context_window": a.context_window,
        "token_budget": a.token_budget,
        "time_budget_sec": a.time_budget_sec,
    }


def resolve_pins(pins: Sequence[str], coordinator: AgentDef, cwd: Path) -> list[Pin]:
    """Design 10.2 over run.input.pins. WORKSPACE_DENIED before any read; url never fetched."""
    out: list[Pin] = []
    for item in pins:
        kind, ref = item.split(":", 1)
        k = cast(Literal["file", "url"], kind)
        if k == "file":
            path = check_path(ref, coordinator.workspace, cwd)
            out.append(Pin(k, ref, read_bounded(path, coordinator.context_window, ref)))
        else:
            out.append(Pin(k, ref))
    return out
