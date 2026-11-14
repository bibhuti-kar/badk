"""Frontmatter `flow` / `final` grammar (design 25.2). Does not import definition.py."""

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from badk.errors import BadkError, Code

STEP_ID: Final = re.compile(r"^[a-z][a-z0-9_]*$")
STEP_KEYS: Final = frozenset({"id", "tool", "args", "agent", "select", "after", "on_error"})
CONTEXT: Final = ("ask", "run_id", "task_id")  # the {{...}} names the run supplies (TOOL-6)
RESERVED_IDS: Final = frozenset({"coordinator", *CONTEXT})  # `coordinator` is a capsule key
_KINDS: Final = ("tool", "agent", "select")


class Step(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    tool: str | None = None
    args: dict[str, Any] = {}
    agent: str | None = None
    select: list[str] | None = None
    after: list[str]
    on_error: Literal["skip", "run"] = "skip"

    @property
    def kind(self) -> Literal["tool", "agent", "select"]:
        return "tool" if self.tool is not None else "agent" if self.agent is not None else "select"


def is_coordinator(name: str) -> bool:
    return name.startswith("coordinator")


def require_coordinator(names: Iterable[str]) -> None:
    """The one 'has any coordinator' guard: coordinator.md or a coordinator-<flow>.md."""
    if not any(is_coordinator(n) for n in names):
        raise BadkError(Code.AGENT_NOT_FOUND, "no coordinator*.md in agents/")


def _bad(path: Path, msg: str) -> BadkError:
    return BadkError(Code.VALIDATION_ERROR, f"{path.name}: {msg}")


def _check_step(raw: object, seen: set[str], path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _bad(path, f"flow step {raw!r} must be a mapping")
    sid = raw.get("id")
    if not isinstance(sid, str) or not STEP_ID.match(sid):
        raise _bad(path, f"flow step id {sid!r} must match {STEP_ID.pattern}")
    if sid in RESERVED_IDS:
        raise _bad(path, f"flow step {sid!r}: id is reserved")
    if sid in seen:
        raise _bad(path, f"flow step {sid!r}: duplicate id")
    seen.add(sid)
    if unknown := sorted(raw.keys() - STEP_KEYS, key=str):
        raise _bad(path, f"flow step {sid!r}: unknown key {unknown[0]!r}")
    _check_kind(raw, sid, path)
    return raw


def _check_kind(raw: dict[str, Any], sid: str, path: Path) -> None:
    if sum(k in raw for k in _KINDS) != 1 or any(raw.get(k, 0) is None for k in _KINDS):
        raise _bad(path, f"flow step {sid!r}: exactly one of tool, agent, select")
    for k in ("tool", "agent"):
        if k in raw and not isinstance(raw[k], str):
            raise _bad(path, f"flow step {sid!r}: {k} must be a string")
    sel = raw.get("select", [])
    if not isinstance(sel, list) or not all(isinstance(x, str) for x in sel):
        raise _bad(path, f"flow step {sid!r}: select must be a list of strings")
    if "args" in raw and not ("tool" in raw and isinstance(raw["args"], dict)):
        raise _bad(path, f"flow step {sid!r}: args only on a tool step, as an object")
    if raw.get("on_error", "skip") not in ("skip", "run"):
        raise _bad(path, f"flow step {sid!r}: on_error must be skip or run")
    after = raw.get("after", [])
    if not isinstance(after, list) or not all(isinstance(x, str) for x in after):
        raise _bad(path, f"flow step {sid!r}: after must be a list of step ids")


def parse_flow(raw: object, path: Path) -> list[Step]:
    if not isinstance(raw, list) or not raw:
        raise _bad(path, "flow must be a non-empty list")
    seen: set[str] = set()
    items = [dict(_check_step(r, seen, path)) for r in raw]
    prev: str | None = None
    for it in items:
        it.setdefault("after", [] if prev is None else [prev])
        prev = it["id"]
        if unknown := sorted(set(it["after"]) - seen):
            raise _bad(path, f"flow step {it['id']!r}: after names unknown {unknown}")
    _no_cycle(items, path)
    return [Step.model_validate(it) for it in items]


def _no_cycle(items: list[dict[str, Any]], path: Path) -> None:
    left = {it["id"]: set(it["after"]) for it in items}
    while ready := [k for k, v in left.items() if not v]:
        for k in ready:
            del left[k]
        for v in left.values():
            v.difference_update(ready)
    if left:
        raise _bad(path, f"flow cycle among steps {sorted(left)}")


def ancestors(flow: Sequence[Step]) -> dict[str, frozenset[str]]:
    """Transitive `after` per step id; `after` may point forward."""
    out: dict[str, frozenset[str]] = {}
    by_id = {s.id: s for s in flow}

    def of(sid: str) -> frozenset[str]:
        if sid not in out:
            out[sid] = frozenset().union(*({p} | of(p) for p in by_id[sid].after))
        return out[sid]

    for s in flow:
        of(s.id)
    return out


def _check_kind_rules(data: dict[str, Any], path: Path) -> None:
    final = data.get("final")
    if final is not None and final not in ("model", "none"):
        raise _bad(path, f"final must be model or none, got {final!r}")
    if is_coordinator(str(data.get("name", ""))):
        for f in ("agents", "always"):
            if data.get(f):
                raise _bad(path, f"a coordinator with a flow cannot have {f}")
    if final == "none" and data.get("output_schema"):
        raise _bad(path, "final none cannot have an output_schema")


def check_fields(data: dict[str, Any], path: Path) -> None:
    """NC-74, PM-35: kind rules for `flow` / `final`; parses `flow` in place."""
    if "flow" not in data:
        if data.get("final") is not None:
            raise _bad(path, "final needs a flow")
        return
    _check_kind_rules(data, path)
    data["flow"] = parse_flow(data["flow"], path)
    data.setdefault("final", "model")
    if data.get("max_parallel") is None:
        data["max_parallel"] = 1


def final_default(data: object) -> object:
    """PM-35 for a hand-built def (SDK): a flow without `final` ends with the model."""
    if isinstance(data, dict) and data.get("flow") and data.get("final") is None:
        return {**data, "final": "model"}
    return data
