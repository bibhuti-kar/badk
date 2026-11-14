"""Flow time checks (design 25.9, PM-39). load refuses `time_budget_sec` <= C + F."""

from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path

import structlog

from badk.agents.definition import AgentDef
from badk.agents.flow import Step, ancestors
from badk.agents.time_check import graham, heaviest_chain, weight
from badk.errors import BadkError, Code
from badk.tools.definition import load_tool

log = structlog.get_logger()


def step_weight(s: Step, agents: Mapping[str, AgentDef], tool_timeout: int, p: int) -> int:
    """tool = timeout x 2; agent = time_budget_sec x max_tries (no flow recursion);
    select = one P + heaviest depends_on chain of its list."""
    if s.tool:
        return tool_timeout * 2
    if s.agent:
        return weight(agents[s.agent])
    if not s.select:
        raise BadkError(Code.INTERNAL_ERROR, f"flow step {s.id!r} has no tool, agent or select")
    return p + heaviest_chain(agents, s.select)[0]


def reserve(owner: AgentDef, p: int) -> int:
    """F: the final model step's reserve."""
    return p if owner.final == "model" else 0


def heaviest_path(flow: Sequence[Step], w: Mapping[str, int]) -> tuple[int, list[str]]:
    """C and its step ids: heaviest `after` path; ties go to the first step."""
    anc = ancestors(flow)
    best: dict[str, tuple[int, str | None]] = {}
    for s in sorted(flow, key=lambda s: len(anc[s.id])):  # ancestors first
        prev = max(s.after, key=lambda d: best[d][0], default=None)  # max keeps the first on ties
        best[s.id] = (w[s.id] + (best[prev][0] if prev else 0), prev)
    top = max((s.id for s in flow), key=lambda i: best[i][0])
    path: list[str] = []
    node: str | None = top
    while node is not None:
        path.append(node)
        node = best[node][1]
    return best[top][0], path[::-1]


def bound(flow: Sequence[Step], w: Mapping[str, int], m: int) -> float:
    """Graham bound of `flow`'s steps (B); 0.0 for an empty list."""
    if not flow:
        return 0.0
    return graham(sum(w[s.id] for s in flow), heaviest_path(flow, w)[0], m)


def unstarted(flow: Sequence[Step], started: Collection[str]) -> list[Step]:
    """The steps not in `started`, `after` cut to the steps that remain (B2 of 25.9)."""
    left = {s.id for s in flow if s.id not in started}
    return [
        s.model_copy(update={"after": [a for a in s.after if a in left]})
        for s in flow
        if s.id in left
    ]


def _timeout(s: Step, tools: Path | None, cache: dict[str, int]) -> int:
    if s.tool is None:
        return 0  # not a tool step: step_weight ignores it
    if tools is None:  # check_flows refuses tool steps without a tools directory
        raise BadkError(Code.INTERNAL_ERROR, f"flow step {s.id!r}: tool step without tools dir")
    if s.tool not in cache:
        cache[s.tool] = load_tool(tools / f"{s.tool}.md").timeout
    return cache[s.tool]


def weights(
    a: AgentDef, agents: Mapping[str, AgentDef], timeout: Callable[[Step], int], p: int
) -> dict[str, int]:
    """Step id -> weight; `timeout` looks up a tool step's tool timeout."""
    return {s.id: step_weight(s, agents, timeout(s), p) for s in a.flow}


def check_flow_budgets(
    agents: Mapping[str, AgentDef], origin: Mapping[str, Path], p: int, tools: Path | None
) -> None:
    """NC-85: refuse `time_budget_sec <= C + F`; WARN when only the run-start bound refuses."""
    timeouts: dict[str, int] = {}  # tool name -> timeout; each tool file loaded once
    for a in agents.values():
        if not a.flow:
            continue
        w = weights(a, agents, lambda s: _timeout(s, tools, timeouts), p)
        c, path = heaviest_path(a.flow, w)
        f = reserve(a, p)
        if a.time_budget_sec <= c + f:
            raise BadkError(
                Code.VALIDATION_ERROR,
                f"{origin[a.name]}: time_budget_sec {a.time_budget_sec} must be greater than "
                f"{c + f} (heaviest chain {' -> '.join(path)} weight {c}, "
                f"plus the final reserve {f})",
            )
        b = bound(a.flow, w, a.max_parallel or 1)
        if a.time_budget_sec <= b + f:
            log.warning(
                "flow_start_bound",
                file=str(origin[a.name]),
                bound=b,
                reserve=f,
                time_budget_sec=a.time_budget_sec,
                note=f"start bound {b} + F {f} exceeds time_budget_sec; "
                "the run-start check may refuse it",
            )
