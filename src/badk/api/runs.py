"""Viewer API: run documents straight from PostgreSQL (design 6.1)."""

import uuid
from typing import Any, Final

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from badk.errors import BadkError, Code
from badk.exec.document import utc_now_iso
from badk.persistence import store
from badk.persistence.events import close_quietly, open_queue, publish_request

router = APIRouter(prefix="/api/v1")
MAX_INPUT_CHARS: Final = 100_000


class RunRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # no `agent` key: the coordinator is always first
    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)

    @field_validator("input")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():  # serve would reject it after we answered 202
            raise ValueError("input must be non-empty text")
        return v  # published as sent, not stripped


@router.post("/runs", status_code=202)
async def post_run(request: Request, body: RunRequestIn) -> dict[str, str]:
    """Accepted, not started: the client polls GET /runs/{run_id}."""
    state = request.app.state
    async with state.publish_lock:  # one publish (or reconnect) at a time on the shared channel
        if state.channel is None:  # broker was down at startup or earlier: try once more
            state.conn, state.channel = await open_queue(state.settings.rabbitmq_url)
        if state.channel is None:
            raise BadkError(Code.QUEUE_UNAVAILABLE, "RabbitMQ is not connected")
        run_id = str(uuid.uuid4())
        msg = {"v": 1, "run_id": run_id, "input": body.input, "received_at": utc_now_iso()}
        try:
            await publish_request(state.channel, msg)
        except BadkError:  # channel may be dead for good: drop it so the next POST reopens
            conn, state.conn, state.channel = state.conn, None, None
            await close_quietly(conn)
            raise
    return {"run_id": run_id}


@router.get("/runs")
async def list_runs_route(
    request: Request,
    page: int = Query(1, ge=1, le=1_000_000),
    per_page: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    return await store.list_runs_page(request.app.state.engine, page, per_page)


@router.get("/runs/{run_id}")
async def get_run_route(request: Request, run_id: str) -> Response:
    doc = await store.get_run_or_404(request.app.state.engine, run_id)
    return Response(doc, media_type="application/json")
