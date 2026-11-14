"""Load-time time budget check (design 22.3, WF-11): coordinator T must exceed heaviest chain."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from badk.agents.definition import AgentDef, is_coordinator
from badk.errors import BadkError, Code


def weight(a: AgentDef) -> int:
    return a.time_budget_sec * a.max_tries


def graham(total: float, longest: float, m: int) -> float:
    """Graham's list-scheduling bound B = (W - L) / m + L."""
    return (total - longest) / m + longest


def heaviest_chain(agents: Mapping[str, AgentDef], catalog: Sequence[str]) -> tuple[int, list[str]]:
    """Largest summed weight over `depends_on` chains of `catalog`; ties go to first entry.
    Iterative post-order (no recursion limit; the graph is acyclic, checked at load)."""
    memo: dict[str, tuple[int, str | None]] = {}  # node -> (chain weight, heaviest dependency)
    for root in catalog:
        stack = [root]
        while stack:
            n = stack[-1]
            todo = [d for d in agents[n].depends_on if d not in memo]
            if todo:
                stack.extend(reversed(todo))
                continue
            stack.pop()
            w, prev = 0, None
            for d in agents[n].depends_on:
                if memo[d][0] > w:  # strict: ties keep the first entry
                    w, prev = memo[d][0], d
            memo[n] = (w + weight(agents[n]), prev)
    top = max(catalog, key=lambda n: memo[n][0], default=None)  # max keeps the first on ties
    if top is None or memo[top][0] == 0:
        return 0, []
    chain: list[str] = []
    node: str | None = top
    while node is not None:
        chain.append(node)
        node = memo[node][1]
    return memo[top][0], chain[::-1]


def check_time_budgets(
    agents: Mapping[str, AgentDef], origin: Mapping[str, Path], reserve: int
) -> None:
    """Coordinator T must exceed chain weight + `reserve` (PROVIDER_TIMEOUT_SEC)."""
    for c in agents.values():
        if not is_coordinator(c.name) or c.flow:  # flow coordinators: check_flow_budgets (FLOW-8)
            continue
        total, chain = heaviest_chain(agents, c.agents)
        if c.time_budget_sec <= total + reserve:
            raise BadkError(
                Code.VALIDATION_ERROR,
                f"{origin[c.name]}: time_budget_sec {c.time_budget_sec} must be greater than "
                f"{total + reserve} (heaviest chain {' -> '.join(chain)} weight {total}, "
                "time_budget_sec × max_tries summed, plus the consolidation reserve "
                f"{reserve} = PROVIDER_TIMEOUT_SEC)",
            )
