"""Flow args templating (design 25.4, PM-35): `{{ask}}` and `{{<step>.<path>}}`. Pure."""

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final, TypeGuard

from badk.agents.flow import CONTEXT
from badk.errors import BadkError, Code
from badk.tools.envelope import Outcome
from badk.tools.execute import to_text

PLACEHOLDER: Final = re.compile(r"\{\{([a-z][a-z0-9_]*(?:\.[A-Za-z0-9_#-]+)*)\}\}")
ANY: Final = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)  # every {{...}}; not PLACEHOLDER = bad syntax


def _fail(msg: str) -> BadkError:
    return BadkError(Code.TOOL_ERROR, msg)


def _is_seq(v: object) -> TypeGuard[Sequence[object]]:
    return isinstance(v, Sequence) and not isinstance(v, str | bytes | bytearray)


def _walk(path: str, outcomes: Mapping[str, Outcome]) -> object:
    root, *segs = path.split(".")
    if root not in outcomes:
        raise _fail(f"{path}: not in the result of {root}")
    cur: object = outcomes[root]
    for seg in segs:
        if isinstance(cur, Mapping) and seg in cur:
            cur = cur[seg]
        elif _is_seq(cur) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            raise _fail(f"{path}: not in the result of {root}")
    return cur


def _json(path: str, value: object) -> str:
    try:
        return json.dumps(value, allow_nan=False)
    except RecursionError:
        raise _fail(f"{path}: value nests too deeply") from None
    except TypeError:
        raise _fail(f"{path}: value is not JSON-encodable") from None
    except ValueError as e:
        what = "is circular" if "ircular" in str(e) else "has a non-finite number"
        raise _fail(f"{path}: value {what}") from None


def _value(path: str, ctx: Mapping[str, str], outcomes: Mapping[str, Outcome]) -> object:
    """`ctx` is the context: ask, run_id, task_id."""
    return ctx[path] if path in ctx else _walk(path, outcomes)


def _leaf(s: str, ctx: Mapping[str, str], outcomes: Mapping[str, Outcome]) -> object:
    if m := PLACEHOLDER.fullmatch(s):  # whole leaf keeps its JSON type
        v = _value(m[1], ctx, outcomes)
        return json.loads(_json(m[1], v))  # a copy; NaN/Infinity refused

    def sub(m: re.Match[str]) -> str:
        v = _value(m[1], ctx, outcomes)
        return v if isinstance(v, str) else (_json(m[1], v), to_text(m[0], v))[1]

    return PLACEHOLDER.sub(sub, s)


def _render(node: object, ctx: Mapping[str, str], outcomes: Mapping[str, Outcome]) -> object:
    if isinstance(node, str):
        return _leaf(node, ctx, outcomes)
    if isinstance(node, Mapping):
        return {k: _render(v, ctx, outcomes) for k, v in node.items()}  # keys never rendered
    if _is_seq(node):
        return [_render(v, ctx, outcomes) for v in node]
    return node


def render(
    args: Mapping[str, Any],
    ask: str,
    outcomes: Mapping[str, Outcome],
    ids: Mapping[str, str],
) -> dict[str, Any]:
    """Args with every placeholder filled; a bad path is TOOL_ERROR naming it (NC-81).
    `ids` is the run's {run_id, task_id} (TOOL-6); never from the model."""
    try:
        out = _render(args, {"ask": ask, **ids}, outcomes)
    except RecursionError:
        raise _fail("args nest too deeply to render") from None
    assert isinstance(out, dict)
    return out


def _strings(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for v in node.values():  # keys are never rendered
            yield from _strings(v)
    elif _is_seq(node):
        for v in node:
            yield from _strings(v)


def _leaf_refs(text: str) -> list[str]:
    found = [m[1] for m in PLACEHOLDER.finditer(text)]
    rest = PLACEHOLDER.sub("", text)
    if "{{" in rest or "}}" in rest:
        bad = ANY.search(rest)
        raise ValueError(f"{(bad[0] if bad else text)!r}: malformed placeholder")
    if ctx := next((r for r in found if r.split(".")[0] in CONTEXT and "." in r), None):
        raise ValueError(f"{{{{{ctx}}}}}: {ctx.split('.')[0]} takes no path")
    return found


# ponytail: no brace escape, same as tool files; add one if a real tool needs literal {{
def refs(args: Mapping[str, Any]) -> list[str]:
    """Each placeholder path in every string leaf (`ask` or `<id>.<path>`). ValueError names
    the first malformed one and why."""
    try:
        return [r for t in _strings(args) for r in _leaf_refs(t)]
    except RecursionError:
        raise ValueError("args nest too deeply to check") from None
