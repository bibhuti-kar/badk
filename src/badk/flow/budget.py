"""Flow run-time time checks (design 25.9, NC-86): refuse when the remaining time is too short."""

from typing import TYPE_CHECKING

from badk.agents.flow import Step
from badk.agents.flow_time import bound, reserve, unstarted, weights
from badk.coordinator.dag import time_bound
from badk.errors import Code
from badk.exec.document import DagNode
from badk.flow.env import env
from badk.flow.schedule import FlowRefused
from badk.stderr_log import get_logger

if TYPE_CHECKING:
    from badk.flow.run import Owner


def _weights(o: "Owner") -> dict[str, int]:
    tools = o.deps.tools(o.agent)
    return weights(
        o.agent, o.agents, lambda s: tools[s.tool].timeout if s.tool else 0,
        env().provider_timeout_sec,
    )  # fmt: skip


def _refuse_unless_room(o: "Owner", b: float, f: int) -> None:
    """R <= bound + F refuses (unrounded floats); `o.started` is the flow's try start."""
    left = o.agent.time_budget_sec - (o.deps.clock() - o.started)
    if left > b + f:
        return
    code = Code.TIME_BUDGET_INSUFFICIENT
    msg = (
        f"remaining time {left:.1f} s does not exceed the flow time bound {b:.1f} s "
        f"plus the final reserve {f:.1f} s"
    )
    detail = {"code": code.value, "bound": round(b, 1), "reserve": f, "remaining": round(left, 1)}
    get_logger().warning(
        "time_budget_insufficient", code=code.value, run_id=o.tl.run.run_id,
        bound=b, reserve=f, remaining=left,
    )  # fmt: skip
    raise FlowRefused(code, msg, [detail])


def check_start(o: "Owner") -> None:
    """Flow start (o.started is stamped): refuses when R <= B + F."""
    b = bound(o.agent.flow, _weights(o), o.agent.max_parallel or 1)
    _refuse_unless_room(o, b, reserve(o.agent, env().provider_timeout_sec))


def check_after_reply(o: "Owner", s: Step, picks: list[DagNode]) -> None:
    """After a select reply, before the picks join the DAG: refuses when
    R <= B1 (picks) + B2 (steps not started) + F."""
    m = o.agent.max_parallel or 1
    with o.lock:  # one pass: key -> state
        states = {n.key: n.state for n in o.tl.run.dag}
    started = {x.id for x in o.agent.flow if states.get(o.prefix + x.id) is not None} | {s.id}
    b2 = bound(unstarted(o.agent.flow, started), _weights(o), m)
    f = reserve(o.agent, env().provider_timeout_sec)
    _refuse_unless_room(o, time_bound(picks, o.agents, m) + b2, f)
