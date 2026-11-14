"""RES-4 pre-check for `badk serve` (design 22.8): one run per run request."""

import asyncio
import json
import os
import uuid
from typing import Literal

import structlog
from aio_pika.abc import AbstractIncomingMessage
from pamqp.commands import Basic

from badk.config import Settings
from badk.errors import BadkError, Code
from badk.exec.document import RunDocument
from badk.persistence.events import RUN_REQUESTS
from badk.stack_check import ViewerUnreachable, prior_run

log = structlog.get_logger()


ATTEMPTS = "x-badk-attempts"  # real failed attempts so far; carried on the message, not in memory


def failed_attempts(message: AbstractIncomingMessage) -> int:
    n = (message.headers or {}).get(ATTEMPTS)
    return n if isinstance(n, int) and n > 0 else 0


async def requeue(message: AbstractIncomingMessage, failed: int) -> None:
    """Publish the same body with the attempt count, then ack the original. A crash between the
    two leaves a duplicate, which the precheck skips (design 22.8). Raises on any failure."""
    props = Basic.Properties(
        headers={**(message.headers or {}), ATTEMPTS: failed},
        delivery_mode=2,
        content_type=message.content_type,
        message_id=message.message_id,
    )
    await message.channel.basic_publish(message.body, routing_key=RUN_REQUESTS, properties=props)
    await message.ack()


def mark(run: RunDocument, value: str) -> None:
    run.agents["coordinator"].metadata["request"] = value


def mark_acked(run: RunDocument) -> None:
    mark(run, "acked")


def viewer_base(settings: Settings, beat: bool) -> str | None:
    """Only the runtime container (beat) reads VIEWER_URL; the host uses settings (decision 21)."""
    if not beat:
        return settings.viewer_url
    if url := os.environ.get("VIEWER_URL"):  # compose sets it on `runtime`
        return url
    raise BadkError(Code.VALIDATION_ERROR, "VIEWER_URL must be set for the runtime (beat mode)")


async def precheck(base: str, rid: str) -> Literal["run", "skip", "defer"]:
    """new or resumable -> run; known run -> skip (ack); viewer unreachable -> defer (requeue)."""
    try:
        prior = await asyncio.to_thread(prior_run, base, rid)
    except ViewerUnreachable:
        log.warning(
            f"run request deferred: viewer API unreachable run_id={rid} url={base}",
            run_id=rid,
            url=base,
            code="VIEWER_UNAVAILABLE",
        )
        return "defer"
    if prior is None:
        return "run"
    if prior == ("RUNNING", "pending"):
        log.info(f"run request resumed run_id={rid}", run_id=rid)
        return "run"
    log.warning(
        f"duplicate run request skipped run_id={rid} status={prior[0]}", run_id=rid, status=prior[0]
    )
    return "skip"


def parse_request(body: bytes) -> tuple[str, str]:
    """Design 5.3, strict: returns (canonical run_id, input) or raises VALIDATION_ERROR."""
    try:
        msg = json.loads(body)
        if not isinstance(msg, dict) or type(msg.get("v")) is not int or msg["v"] != 1:
            raise ValueError("v must be 1")
        rid, text = msg.get("run_id"), msg.get("input")
        if not isinstance(rid, str):
            raise ValueError("run_id must be a uuid")
        rid = str(uuid.UUID(rid))
        if not isinstance(text, str) or not text.strip():
            raise ValueError("input must be non-empty text")
    except Exception as e:  # JSONDecodeError, UUID ValueError, RecursionError on deep nesting...
        raise BadkError(Code.VALIDATION_ERROR, f"bad run request: {e!r}") from e
    return rid, text
