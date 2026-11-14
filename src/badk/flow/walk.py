"""Which agents of a flow make a model call (design 25.11): the preflight set."""

from collections.abc import Iterator, Mapping

from badk.agents.definition import AgentDef, closure
from badk.errors import BadkError, Code


def _reached(agents: dict[str, AgentDef], a: AgentDef) -> Iterator[str]:
    """Names an owner's agent steps and select lists reach (select + depends_on closure)."""
    for s in a.flow:
        for n in [s.agent, *(s.select or ())]:
            if n and n not in agents:  # an SDK caller's hand-built map
                raise BadkError(
                    Code.AGENT_NOT_FOUND, f"{a.name} step {s.id}: agent {n!r} is not loaded"
                )
        if s.agent:
            yield s.agent
        if s.select:
            yield from closure(agents, list(s.select))


def model_agents(owner: AgentDef, agents: Mapping[str, AgentDef]) -> dict[str, AgentDef]:
    """Owner and every agent reachable through agent/select steps, kept when it can make a
    model call: no `flow`, a select step, or `final: model`. A tool-only `final: none` flow
    agent is walked through, not kept."""
    pool = dict(agents)
    seen = {owner.name: owner}
    todo = [owner]
    while todo:
        for n in _reached(pool, todo.pop()):
            if n not in seen:
                seen[n] = pool[n]
                todo.append(pool[n])
    return {
        n: a
        for n, a in seen.items()
        if not a.flow or a.final == "model" or any(s.kind == "select" for s in a.flow)
    }
