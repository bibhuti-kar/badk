"""What `run_task` cannot see (design 25.11): the run's agent map, bound once per run."""

from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING

from badk.agents.definition import AgentDef
from badk.errors import BadkError, Code

if TYPE_CHECKING:
    from badk.coordinator.run import RunOptions


@dataclass(frozen=True)
class FlowEnv:
    agents: Mapping[str, AgentDef]
    provider_timeout_sec: int  # P: one model call, the weight of a select call and of F (25.9)


FLOW_ENV: ContextVar[FlowEnv | None] = ContextVar("flow_env", default=None)


def bind_env(opts: "RunOptions") -> Token[FlowEnv | None]:
    """The run's env; the caller resets it with the token when the run ends."""
    return FLOW_ENV.set(FlowEnv(opts.agents, opts.settings.provider_timeout_sec))


def env() -> FlowEnv:
    e = FLOW_ENV.get()
    if e is None:
        raise BadkError(Code.INTERNAL_ERROR, "flow env is not bound (run_coordinator binds it)")
    return e
