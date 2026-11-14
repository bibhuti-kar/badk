"""Final step (design 25.10): `final: none` answers with the step results, `model` asks once."""

from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING

from badk.agents.flow import Step
from badk.coordinator.consolidate import result_line, run_final
from badk.coordinator.dispatcher import run_agent
from badk.exec.document import Capsule, CapsuleStatus, TraceType, dumps, finalise
from badk.exec.trace import Entry
from badk.tools.envelope import Outcome

if TYPE_CHECKING:
    from badk.flow.run import Owner


def result_lines(flow: Sequence[Step], outcomes: Mapping[str, Outcome]) -> str:
    """One line per step, flow order; a step without an outcome was skipped (PM-37)."""
    out = []
    for s in flow:
        o = outcomes.get(s.id)
        if o is None:
            out.append(result_line(s.id, "skipped", None, None))
        else:
            out.append(result_line(s.id, o["status"], o["error_code"], o["body"]))
    return "\n".join(out)


def _failed(outcomes: Mapping[str, Outcome], skipped: Collection[str]) -> bool:
    return bool(skipped) or any(v["status"] == "error" for v in outcomes.values())


def finish(o: "Owner", outcomes: Mapping[str, Outcome], skipped: Collection[str]) -> None:
    """The run coordinator's final record."""
    cap = o.live().model_copy(deep=True)
    if o.agent.final == "model":
        ask = o.first_input + "\n" + result_lines(o.agent.flow, outcomes)
        done, entry = run_final(o.agent, cap, ask, o.deps)
    else:
        done, entry = cap, _none_entry(o, cap, outcomes)
        done.status = CapsuleStatus.COMPLETED
    if _failed(outcomes, skipped) and done.status is CapsuleStatus.COMPLETED:
        done.status = CapsuleStatus.INCOMPLETE  # PM-38
    o.tl.append(o.key, done, entry, finalise)


def finish_nested(o: "Owner", outcomes: Mapping[str, Outcome], skipped: Collection[str]) -> Capsule:
    """A nested owner's capsule for `run_task`; INCOMPLETE (PM-38, 25.8) when a step failed."""
    cap = o.live().model_copy(deep=True)
    if o.agent.final == "model":  # run_agent restarts re-send this same input (PM-41)
        ask = o.first_input + "\n" + result_lines(o.agent.flow, outcomes)
        done = run_agent(o.agent, cap, ask, o.deps)
    else:
        cap.metadata["result"] = {s.id: outcomes[s.id] for s in o.agent.flow if s.id in outcomes}
        done = cap
        done.status = CapsuleStatus.COMPLETED
    if _failed(outcomes, skipped) and done.status is CapsuleStatus.COMPLETED:
        done.status = CapsuleStatus.INCOMPLETE
        o.tl.append(o.key, done, Entry(None))  # run_task writes records only for DONE/ERROR
    return done


def _none_entry(o: "Owner", cap: Capsule, outcomes: Mapping[str, Outcome]) -> Entry:
    answer = dumps({s.id: outcomes[s.id] for s in o.agent.flow if s.id in outcomes})
    cap.metadata["answer"] = answer
    return Entry(TraceType.DONE, {"answer": answer})
