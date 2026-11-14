"""Graph expansion (design 20.5, WF-4): always + selection + depends_on closure -> DagNode list."""

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Literal

from badk.agents.definition import AgentDef
from badk.agents.time_check import graham, heaviest_chain, weight
from badk.exec.document import Call, DagNode

Source = Literal["selected", "always", "dependency"]


def expand(
    calls: Sequence[Call], coord: AgentDef, agents: Mapping[str, AgentDef], raw: str
) -> list[DagNode]:
    """Pure. Selected nodes (name#i when an agent is picked n > 1 times), then `always`, then the
    BFS closure of depends_on; ordered by catalog index, then instance number."""
    n = Counter(c.agent for c in calls)
    seen: Counter[str] = Counter()
    nodes: list[DagNode] = []

    def add(name: str, source: Source, text: str, key: str) -> None:
        deps = list(agents[name].depends_on)
        nodes.append(DagNode(key=key, agent=name, source=source, input=text, depends_on=deps))

    for c in calls:
        seen[c.agent] += 1
        add(
            c.agent,
            "selected",
            c.input,
            f"{c.agent}#{seen[c.agent]}" if n[c.agent] > 1 else c.agent,
        )
    for name in coord.always:
        if name not in n:
            add(name, "always", raw, name)
            n[name] = 1
    for node in nodes:  # grows while iterating: BFS
        for d in node.depends_on:
            if d not in n:
                add(d, "dependency", raw, d)
                n[d] = 1
    order = {name: i for i, name in enumerate(agents)}
    return sorted(nodes, key=lambda x: order[x.agent])  # stable: instances stay in call order


def time_bound(dag: Sequence[DagNode], agents: Mapping[str, AgentDef], max_parallel: int) -> float:
    """WF-12, Graham's list-scheduling bound B = (W - L) / m + L over the expanded `dag`.
    W sums node weights (instances counted); L is the heaviest depends_on chain."""
    total = sum(weight(agents[n.agent]) for n in dag)
    longest = heaviest_chain(agents, list(dict.fromkeys(n.agent for n in dag)))[0]
    return graham(total, longest, max_parallel)
