"""DB writer service (design 11.4): consume step events, apply them, ack after commit."""

import asyncio
import json
import sys
import uuid
from datetime import datetime
from typing import Any

import aio_pika
import structlog
from aio_pika.abc import AbstractIncomingMessage
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from badk import heartbeat
from badk.config import load_settings
from badk.errors import BadkError, Code
from badk.logging_setup import configure_logging
from badk.persistence import store
from badk.persistence.db import ensure_schema, make_engine
from badk.persistence.events import EVENTS, declare_queues

log = structlog.get_logger()
_METRICS = ("tokens_in", "tokens_out", "latency_ms", "steps")
PREFETCH = 100
DB_TIMEOUT_S = 30  # a hung DB call times out, the writer exits and compose restarts it
# SQLSTATE classes that mean "this event's data is bad", not "the DB is unhealthy"
# data exception, integrity violation, program limit exceeded (54000 huge trace); on a
# redelivered message only these go to the DLQ: None/unknown/unhealthy-DB errors retry
_BAD_DATA_CLASSES = ("22", "23", "54")


def _num(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def parse_event(body: bytes) -> dict[str, Any]:
    """Decode and check the shape store.apply reads; any problem raises (ValueError/TypeError)."""
    event = json.loads(body)
    if not isinstance(event, dict) or event.get("v") != 1:
        raise ValueError("unsupported event version")
    uuid.UUID(event["run_id"])
    hdr, m, trace, key = (
        event["header"],
        event["metrics"],
        event.get("trace"),
        event.get("agent_key"),
    )
    ok = (
        isinstance(event["eid"], int) and not isinstance(event["eid"], bool)
        and isinstance(hdr, dict) and isinstance(hdr["status"], str)
        and isinstance(m, dict) and all(_num(m[k]) for k in _METRICS)
        and isinstance(event["logs"], list)
        and (trace is None or (isinstance(trace, dict) and isinstance(trace["seq"], int)))
        and (key is None or (isinstance(key, str) and isinstance(event["capsule"], dict)))
    )  # fmt: skip
    if not ok:
        raise TypeError("event field has the wrong type")
    datetime.fromisoformat(hdr["started_at"])
    return event


async def on_message(message: AbstractIncomingMessage, engine: AsyncEngine) -> None:
    """Apply one event and ack. Bad data goes to the DLQ; anything else propagates unacked."""
    try:
        event = parse_event(message.body)
    except (ValueError, KeyError, TypeError, AttributeError) as e:  # JSONDecodeError, bad uuid
        log.error("writer.bad_event", error=repr(e))
        await message.reject(requeue=False)
        return
    try:
        async with asyncio.timeout(DB_TIMEOUT_S), engine.begin() as conn:
            await store.apply(conn, event)
    except DBAPIError as e:  # asyncpg: server class-22/23 errors and client encode errors alike
        state = str(getattr(e.orig, "sqlstate", None))
        if state[:2] not in _BAD_DATA_CLASSES or (state[:2] == "54" and not message.redelivered):
            raise  # exit unacked: retried after restart; only a repeat limit error goes below
        log.error("writer.bad_event", error=str(e.orig))
        await message.reject(requeue=False)
        return
    await message.ack()


async def main() -> None:
    settings = load_settings()
    if not settings.database_url or not settings.rabbitmq_url:
        raise BadkError(Code.VALIDATION_ERROR, "DATABASE_URL and RABBITMQ_URL are required")
    configure_logging(settings.log_level)
    engine = make_engine(settings)
    try:
        await ensure_schema(engine)
        async with await aio_pika.connect_robust(settings.rabbitmq_url) as conn:
            channel = await conn.channel()
            await channel.set_qos(prefetch_count=PREFETCH)
            queues = await declare_queues(channel)
            beat = asyncio.create_task(heartbeat.beat(conn))
            try:
                async with queues[EVENTS].iterator() as it:
                    async for message in it:
                        await on_message(message, engine)
            finally:
                beat.cancel()
    finally:
        await engine.dispose()


def run() -> None:
    configure_logging("INFO", sys.stderr)  # startup failures reach stderr; main() reconfigures
    try:
        asyncio.run(main())
        # the consume loop only ends when aio-pika gives up restoring the channel: not a clean stop
        log.error("writer.consume_ended")
        sys.exit(1)
    except BadkError as e:
        log.error("writer.start_failed", code=e.code.value, error=e.message)
        sys.exit(1)


if __name__ == "__main__":
    run()
