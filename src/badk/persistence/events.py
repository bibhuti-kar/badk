"""Queue names, declaration (design 5.1) and the host's EventPublisher (design 11.3)."""

import asyncio
import json
import threading
from collections.abc import Coroutine
from typing import Any, Final

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractQueue,
    AbstractRobustConnection,
)

from badk.config import Settings
from badk.errors import BadkError, Code
from badk.stderr_log import get_logger

log = get_logger()
CONFIRM_TIMEOUT_SEC: Final = 5

EVENTS = "badk.events"
RUN_REQUESTS = "badk.run_requests"


async def declare_queues(channel: AbstractChannel) -> dict[str, AbstractQueue]:
    """Declare both queues and their DLQs; idempotent, call on every connect."""
    out: dict[str, AbstractQueue] = {}
    for name in (EVENTS, RUN_REQUESTS):
        await channel.declare_queue(f"{name}.dlq", durable=True)
        out[name] = await channel.declare_queue(
            name,
            durable=True,
            arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": f"{name}.dlq"},
        )
    return out


async def _publish_persistent(
    channel: AbstractChannel, body: bytes, message_id: str, routing_key: str
) -> None:
    """The one persistent, confirmed publish recipe (channel confirms are on)."""
    await channel.default_exchange.publish(
        aio_pika.Message(
            body,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
            message_id=message_id,
        ),
        routing_key=routing_key,
    )


async def publish_request(channel: AbstractChannel, request: dict[str, Any]) -> None:
    """Confirmed, persistent run request (design 6.1); any failure is QUEUE_UNAVAILABLE."""
    try:
        async with asyncio.timeout(CONFIRM_TIMEOUT_SEC):
            await _publish_persistent(
                channel, json.dumps(request).encode(), str(request["run_id"]), RUN_REQUESTS
            )
    except Exception as e:
        log.error("run_request_publish_failed", error_type=type(e).__name__)  # no URL, no repr
        raise BadkError(Code.QUEUE_UNAVAILABLE, "run request queue unavailable") from e


async def open_queue(
    url: str | None,
) -> tuple[AbstractRobustConnection | None, AbstractChannel | None]:
    """One connection + one confirm channel. A broker that is down degrades POST to 503 (the
    viewer stays up); the next POST retries once. Never leaks a half-open connection."""
    if not url:
        return None, None
    conn: AbstractRobustConnection | None = None
    try:
        async with asyncio.timeout(CONFIRM_TIMEOUT_SEC):
            conn = await aio_pika.connect_robust(url)
            channel = await conn.channel()
            await declare_queues(channel)
    except Exception as e:
        log.error("queue_unavailable", error_type=type(e).__name__)
        await close_quietly(conn)
        return None, None
    return conn, channel


async def close_quietly(conn: AbstractConnection | None) -> None:
    """Bounded best-effort close: a broken connection must never block or raise."""
    if conn is None:
        return
    try:
        await asyncio.wait_for(conn.close(), CONFIRM_TIMEOUT_SEC)
    except Exception:  # noqa: S110 - best effort, the connection is already broken
        pass


def run_threadsafe(
    coro: Coroutine[Any, Any, object], loop: asyncio.AbstractEventLoop, timeout: float
) -> None:
    """Run `coro` on `loop` from a worker thread and wait; on any failure cancel it, then raise."""
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        fut.result(timeout)
    except BaseException:
        fut.cancel()
        raise


class EventPublisher:
    """Persistence sink (PIPE-1): one persistent, confirmed message per TraceLog event.

    A private loop thread owns the connection, which opens on the first event so a broker
    failure on `run created` is a startup failure. TraceLog calls sinks serially, in eid order.
    """

    def __init__(self, settings: Settings) -> None:
        self._url = settings.rabbitmq_url
        self._conn: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._loop: asyncio.AbstractEventLoop | None = None  # started on the first publish
        self._thread: threading.Thread | None = None
        self._failed: BadkError | None = None  # sticky: after one QUEUE_UNAVAILABLE, no more I/O

    def _start(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._thread.start()
        return self._loop

    def __call__(self, event: dict[str, Any]) -> None:
        if self._failed is not None:  # the run already ended ERROR: never publish again
            raise self._failed
        if not self._url:
            raise BadkError(Code.VALIDATION_ERROR, "RABBITMQ_URL is required to publish events")
        try:  # a bad event is a host bug: coded, no reconnect, no retry
            body, message_id = json.dumps(event).encode(), str(event["eid"])
        except (TypeError, ValueError, KeyError) as e:
            raise BadkError(Code.INTERNAL_ERROR, f"event is not publishable: {e!r}") from e
        try:
            for attempt in (1, 2):  # reconnect once, re-publish once
                try:
                    self._run(self._publish(body, message_id))
                    return
                except Exception:
                    self._run(self._reset())
                    if attempt == 2:
                        raise
        except Exception as e:
            self._failed = BadkError(Code.QUEUE_UNAVAILABLE, f"event publish failed: {e!r}")
            raise self._failed from e

    def _run(self, coro: Coroutine[Any, Any, None]) -> None:
        # a late publish must not overwrite the retry's connection or double-send: cancelled
        run_threadsafe(coro, self._start(), CONFIRM_TIMEOUT_SEC * 2)

    async def _publish(self, body: bytes, message_id: str) -> None:
        async with asyncio.timeout(CONFIRM_TIMEOUT_SEC):
            assert self._url
            if self._conn is None or self._conn.is_closed:
                await self._reset()
                self._conn = await aio_pika.connect(self._url)
            if self._channel is None or self._channel.is_closed:
                self._channel = await self._conn.channel()  # publisher confirms are on
                await declare_queues(self._channel)
            await _publish_persistent(self._channel, body, message_id, EVENTS)

    async def _reset(self) -> None:
        conn, self._conn, self._channel = self._conn, None, None
        if conn is not None:
            try:
                await asyncio.wait_for(conn.close(), CONFIRM_TIMEOUT_SEC)  # may hang
            except Exception:  # noqa: S110 - the connection is already broken (or hung)
                pass

    def close(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return  # never published: nothing to release
        try:
            self._run(self._reset())
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(CONFIRM_TIMEOUT_SEC)
            if not thread.is_alive():
                loop.close()
