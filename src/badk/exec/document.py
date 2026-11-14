"""In-memory run document models and helpers (design 4.1)."""

import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)

from badk.errors import Code


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapsuleStatus(StrEnum):
    RUNNING = "RUNNING"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    INCOMPLETE = "INCOMPLETE"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    INCOMPLETE = "INCOMPLETE"


class TraceType(StrEnum):
    PLAN = "plan"
    LLM = "llm"
    TOOL = "tool"
    COMPRESS = "compress"
    RETRY = "retry"
    ERROR = "error"
    RESTART = "restart"
    DONE = "done"


class Message(_M):
    role: Literal["system", "user", "assistant"]
    content: str


class ErrorInfo(_M):
    code: Code
    message: str


class Capsule(_M):
    task_id: str | None
    agent: str
    journal_summary: str
    active_chat_history: list[Message]
    token_spent_so_far: int
    time_started: float
    tries: int
    status: CapsuleStatus
    error: ErrorInfo | None
    metadata: dict[str, Any]


class Call(_M):
    agent: str
    input: str


class DagNode(_M):
    key: str  # "sub-a" or "sub-a#2" (PM-24)
    agent: str
    source: Literal["selected", "always", "dependency", "flow"]
    input: str
    depends_on: list[str]  # agent names, as in the file
    state: Literal["ran", "skipped"] | None = None
    kind: Literal["tool", "agent", "select"] | None = None  # None = classical node (PM-23)
    tool: str | None = None
    after: list[str] = []  # node keys
    parent: str | None = None

    @model_serializer(mode="wrap")
    def _drop_flow(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        out: dict[str, Any] = handler(self)
        if self.kind is None:  # classical documents stay byte-equal (G-11)
            for k in ("kind", "tool", "after", "parent"):
                out.pop(k, None)
        return out


class TraceRecord(_M):
    seq: Annotated[int, Field(ge=1)]
    ts: str
    agent: str
    task_id: str | None
    type: TraceType
    tokens_in: int
    tokens_out: int
    latency_ms: int
    detail: dict[str, Any]


class RunInput(_M):
    raw: str
    slash: str | None = None
    pins: list[str] = Field(default_factory=list)


class Totals(_M):
    tokens: int = 0
    seconds: float = 0
    steps: int = 0


class RunDocument(_M):
    run_id: str
    status: RunStatus
    started_at: str
    ended_at: str | None
    input: RunInput
    workflow: str = "coordinator"
    selection: list[Call] = Field(default_factory=list)
    dag: list[DagNode] = Field(default_factory=list)
    agents: dict[str, Capsule]
    totals: Totals
    trace: list[TraceRecord]

    @field_validator("run_id")
    @classmethod
    def _uuid4(cls, v: str) -> str:
        if uuid.UUID(v).version != 4 or str(uuid.UUID(v)) != v:
            raise ValueError("run_id must be a canonical uuid4 string")
        return v


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_capsule(agent: str, task_id: str | None) -> Capsule:
    return Capsule(
        task_id=task_id,
        agent=agent,
        journal_summary="",
        active_chat_history=[],
        token_spent_so_far=0,
        time_started=0.0,
        tries=0,
        status=CapsuleStatus.RUNNING,
        error=None,
        metadata={},
    )


def create_run(
    text: str,
    slash: str | None = None,
    run_id: str | None = None,
    pins: list[str] | None = None,
    workflow: str = "coordinator",
) -> RunDocument:
    return RunDocument(
        run_id=run_id or str(uuid.uuid4()),
        status=RunStatus.RUNNING,
        started_at=utc_now_iso(),
        ended_at=None,
        input=RunInput(raw=text, slash=slash, pins=pins or []),
        workflow=workflow,
        agents={"coordinator": new_capsule(workflow, None)},
        totals=Totals(),
        trace=[],
    )


def dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def run_status(caps: dict[str, Capsule]) -> RunStatus:
    """Design 20.6: coordinator ERROR -> ERROR; every capsule COMPLETED -> COMPLETED; else
    INCOMPLETE (a sub ERROR or skip with a coordinator answer is partial). Missing coordinator
    counts as INCOMPLETE."""
    coord = caps.get("coordinator")
    if coord and coord.status == CapsuleStatus.ERROR:
        return RunStatus.ERROR
    if coord and all(c.status == CapsuleStatus.COMPLETED for c in caps.values()):
        return RunStatus.COMPLETED
    return RunStatus.INCOMPLETE


def finalise(run: RunDocument) -> None:
    """In-place terminal header (status by the rule, ended_at now); a TraceLog header edit.
    Totals are not touched: TraceLog keeps them live from event metrics (D-7)."""
    run.status, run.ended_at = run_status(run.agents), utc_now_iso()


def render_final(run: RunDocument) -> str:
    """stdout text of a run (design 4.1)."""
    caps = run.agents
    coord = caps.get("coordinator")
    if run.status == RunStatus.ERROR:
        e = coord.error if coord else None
        body = f"{e.code}: {e.message}" if e else f"{Code.INTERNAL_ERROR}: no error detail"
        return f"ERROR {body} [coordinator]"
    a = coord.metadata.get("answer") if coord else None
    if a:  # also a partial run: the answer is printed, the exit code says INCOMPLETE
        return a if isinstance(a, str) else dumps(a)
    if run.status == RunStatus.INCOMPLETE:
        keys = sorted(k for k, c in caps.items() if c.status != CapsuleStatus.COMPLETED)
        if "coordinator" not in caps:
            keys.append("coordinator missing")
        return f"INCOMPLETE: {', '.join(keys)}"
    return ""


def error_capsule(run: RunDocument, code: Code, message: str) -> Capsule:
    """Coordinator capsule copy in ERROR; callers append it with TraceType.ERROR."""
    return fail_capsule(run.agents["coordinator"].model_copy(deep=True), code, message)


def fail_capsule(cap: Capsule, code: Code, message: str) -> Capsule:
    """Put `cap` (in place) into ERROR with `code`/`message`; the one ERROR shape."""
    cap.status = CapsuleStatus.ERROR
    cap.error = ErrorInfo(code=code, message=message)
    return cap
