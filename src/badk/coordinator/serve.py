"""`badk serve`: consume run requests from RabbitMQ and run them headless (design 10.6, D-3)."""

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Final

import aio_pika
import structlog
from aio_pika.abc import AbstractIncomingMessage, AbstractQueueIterator
from aio_pika.exceptions import AMQPError

from badk import heartbeat
from badk.agents.definition import AgentDef
from badk.config import Settings
from badk.coordinator.redeliver import (
    failed_attempts,
    mark,
    mark_acked,
    parse_request,
    precheck,
    requeue,
    viewer_base,
)
from badk.coordinator.run import RunOptions, make_sinks, new_run
from badk.errors import AckFailed, BadkError, Code
from badk.exec.document import RunDocument
from badk.exec.trace import TraceLog
from badk.persistence.events import RUN_REQUESTS, declare_queues, run_threadsafe
from badk.sdk import execute_run
from badk.stack_check import PERSIST_WAIT_SEC, VIEWER_RETRY_SEC, RunDeferred, wait_visible

log = structlog.get_logger()
SINK_RETRY_WAIT_SEC: Final = 5
ACK_TIMEOUT_SEC: Final = 10
_Hook = Callable[[TraceLog], None]
SignalInstaller = Callable[[Callable[[], None]], None]  # cli registers the handler (SDK-3.1)


async def _requeue_once(message: AbstractIncomingMessage, rid: str) -> bool:
    """Not acked, a real failure: a second one => DLQ, else wait and requeue. Never raises;
    False when the settle failed (the message may still be held: restart the channel)."""
    n = failed_attempts(message)
    if n >= 1:
        return await _settle(message.reject(requeue=False))
    await asyncio.sleep(SINK_RETRY_WAIT_SEC)  # one retry, no hot loop
    return await _settle(requeue(message, n + 1))


def _ack_hook(
    message: AbstractIncomingMessage,
    rid: str,
    loop: asyncio.AbstractEventLoop,
    acked: list[bool],
    base: str | None,
) -> _Hook:
    def on_started(tl: TraceLog) -> None:  # run thread, once "run created" is confirmed
        if base:  # design 22.8: ack only once R is visible; work only once `acked` is
            wait_visible(base, rid, "pending", PERSIST_WAIT_SEC)  # RunDeferred: no ack
        try:  # on failure the ack is cancelled: a late ack must not outlive the ERROR run
            run_threadsafe(message.ack(), loop, ACK_TIMEOUT_SEC)
        except Exception as e:
            raise AckFailed(repr(e)) from e
        acked.append(True)
        if base:
            tl.header_changed(mark_acked)
            wait_visible(base, rid, "acked", None)
        log.info("run_started", run_id=rid)

    return on_started


async def _drive(
    run: RunDocument, opts: RunOptions, settings: Settings, started: _Hook
) -> RunDocument | None:
    rid = run.run_id
    sinks = make_sinks(settings, headless=True)

    def say(s: str) -> None:
        log.error("run_message", run_id=rid, text=s.strip())

    return await asyncio.to_thread(lambda: execute_run(run, sinks, say, opts, started).doc)


async def _handled_by_precheck(
    message: AbstractIncomingMessage, base: str | None, rid: str
) -> bool | None:
    """None: go on and run it. Else the handle() result (False: the skip ack failed)."""
    verdict = await precheck(base, rid) if base else "run"
    if verdict == "defer":
        return await _requeue_after_wait(message, rid)
    if verdict == "skip":
        ok = await _settle(message.ack())
        if ok:
            log.info("run request acked", run_id=rid)  # RES-4.4 latency ends here
        return ok
    return None if verdict == "run" else True


async def handle(
    message: AbstractIncomingMessage,
    settings: Settings,
    agents: Mapping[str, AgentDef],
    base: str | None = None,
) -> bool:
    """One message; never raises. False: a settle (ack, reject, requeue) failed, so the
    channel is suspect and the message may still be held."""
    try:
        rid, text = parse_request(message.body)
    except BadkError as e:
        log.warning("run_request_rejected", error=e.message)
        return await _settle(message.reject(requeue=False))
    if (handled := await _handled_by_precheck(message, base, rid)) is not None:
        return handled
    try:
        run, opts = new_run(text, agents, settings, rid)
    except BadkError as e:  # deterministic (unknown workflow): a redelivery cannot succeed
        log.warning("run_request_rejected", run_id=rid, error=e.message)
        return await _settle(message.reject(requeue=False))
    mark(run, "pending")  # event 1 carries it
    return await _run_request(message, run, opts, settings, base)


async def _run_request(
    message: AbstractIncomingMessage,
    run: RunDocument,
    opts: RunOptions,
    settings: Settings,
    base: str | None,
) -> bool:
    rid = run.run_id
    acked: list[bool] = []
    on_started = _ack_hook(message, rid, asyncio.get_running_loop(), acked, base)
    try:
        done = await _drive(run, opts, settings, on_started)
    except RunDeferred as e:  # nothing acked, no work ran: requeue (never the DLQ path)
        text = f"run request deferred: run not visible in the viewer API run_id={rid}"
        log.warning(text, run_id=rid, code="VIEWER_UNAVAILABLE", error=str(e))
        return await _requeue_after_wait(message, rid)
    except Exception as e:
        log.error("run_failed", run_id=rid, error=repr(e), acked=bool(acked))
        done = None
    if done is None:
        return await _requeue_once(message, rid) if not acked else True
    log.info("run_finished", run_id=rid, status=done.status.value)
    if not acked:  # run is ERROR now; an ack may be on the wire, a 2nd risks a 406: drop channel
        log.error("run_ack_failed", run_id=rid)
    return bool(acked)


async def _requeue_after_wait(message: AbstractIncomingMessage, rid: str) -> bool:
    await asyncio.sleep(VIEWER_RETRY_SEC)  # a deferral is not an attempt: count unchanged
    return await _settle(requeue(message, failed_attempts(message)))


async def _settle(op: Awaitable[None]) -> bool:
    try:
        await asyncio.wait_for(op, ACK_TIMEOUT_SEC)
    except Exception as e:
        log.error("run_request_settle_failed", error=repr(e))
        return False
    return True


async def serve(
    settings: Settings,
    agents: Mapping[str, AgentDef],
    beat: bool = False,
    *,
    install_signals: SignalInstaller | None,
) -> None:
    """Infrastructure failures become coded BadkErrors (cli prints them, exit 1)."""
    try:
        await _serve(settings, agents, beat, install_signals)
    except BadkError:
        raise
    except (AMQPError, OSError) as e:
        raise BadkError(Code.QUEUE_UNAVAILABLE, f"RabbitMQ failure: {e!r}") from e
    except Exception as e:
        log.error("serve_failed", error=repr(e))
        raise BadkError(Code.INTERNAL_ERROR, f"serve failed: {e!r}") from e


async def _consume(
    it: AbstractQueueIterator,
    state: dict[str, bool],
    settings: Settings,
    agents: Mapping[str, AgentDef],
    base: str | None,
) -> None:
    async for message in it:
        state["busy"] = True
        healthy = await handle(message, settings, agents, base)
        state["busy"] = False
        if not healthy:  # exit non-zero: `restart: on-failure` gives a fresh channel
            raise BadkError(Code.QUEUE_UNAVAILABLE, "run request ack failed")
        if state["stopping"]:
            return


def _on_signal(state: dict[str, bool], task: "asyncio.Future[None]") -> None:
    if state["stopping"]:  # second signal: abandon the running run
        log.error("serve_forced_exit")
        os._exit(130)
    state["stopping"] = True  # first: finish the current run, exit 0
    if not state["busy"]:
        task.cancel()


async def _serve(
    settings: Settings,
    agents: Mapping[str, AgentDef],
    beat: bool,
    install_signals: SignalInstaller | None,
) -> None:
    base = viewer_base(settings, beat)  # refuses (VALIDATION_ERROR) before any connection
    conn = await aio_pika.connect_robust(settings.rabbitmq_url)
    async with conn:
        channel = await conn.channel()
        await channel.set_qos(prefetch_count=1)
        queue = (await declare_queues(channel))[RUN_REQUESTS]
        async with queue.iterator() as it:
            state = {"busy": False, "stopping": False}
            task = asyncio.ensure_future(_consume(it, state, settings, agents, base))
            if install_signals:
                install_signals(lambda: _on_signal(state, task))
            log.info("serve_ready")
            # D-28: only the runtime image (`badk serve --heartbeat`) has a probe to feed
            hb = asyncio.create_task(heartbeat.beat(conn) if beat else asyncio.sleep(0))
            try:
                try:
                    await task
                except asyncio.CancelledError:
                    cur = asyncio.current_task()
                    if cur is not None and cur.cancelling():
                        raise  # _serve itself is being cancelled; only the inner cancel is benign
            finally:
                hb.cancel()
            if not state["stopping"]:  # like writer.consume_ended: exit non-zero, compose restarts
                log.error("serve.consume_ended")
                raise BadkError(Code.QUEUE_UNAVAILABLE, "run request consumer ended")
