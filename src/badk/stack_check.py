"""Design 20.8: broker (a real AMQP login), then viewer, each within 4 s."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Final
from urllib.parse import urlsplit

import aio_pika
import httpx
import structlog

from badk.config import Settings
from badk.errors import BadkError, Code

_CHECK_SEC = 4
log = structlog.get_logger()
PERSIST_POLL_SEC: Final = 0.2
PERSIST_WAIT_SEC: Final = 30
VIEWER_RETRY_SEC: Final = 1


class ViewerUnreachable(Exception):
    """Viewer API gave no usable answer. Never a BadkError (design 22.8)."""


class RunDeferred(Exception):
    """The start hook gave up before the ack: serve.handle nacks and requeues."""


def amqp_probe(url: str) -> None:
    """Connect and authenticate once. Own thread + loop, so it works inside a running loop."""

    async def go() -> None:
        async with asyncio.timeout(_CHECK_SEC):  # connect, login and close share one bound
            conn = await aio_pika.connect(url, timeout=_CHECK_SEC)
            await conn.close()

    with ThreadPoolExecutor(1) as ex:
        ex.submit(asyncio.run, go()).result()


def stack_check(conf: Settings) -> None:
    if not conf.rabbitmq_url:
        raise BadkError(Code.VALIDATION_ERROR, "RABBITMQ_URL is required")
    try:
        u = urlsplit(conf.rabbitmq_url)
        host, port = u.hostname, u.port or (5671 if u.scheme == "amqps" else 5672)
    except ValueError as e:
        raise BadkError(Code.VALIDATION_ERROR, "RABBITMQ_URL is not a valid URL") from e
    if not host:
        raise BadkError(Code.VALIDATION_ERROR, "RABBITMQ_URL is not a valid URL")
    try:
        amqp_probe(conf.rabbitmq_url)
    except Exception as e:  # never the user or password in the message
        raise BadkError(
            Code.QUEUE_UNAVAILABLE,
            f"cannot reach RabbitMQ at {host}:{port}: {type(e).__name__}",
        ) from e
    try:
        ok = (
            httpx.get(f"{conf.viewer_url}/health", timeout=_CHECK_SEC).status_code == httpx.codes.OK
        )
    except httpx.HTTPError:
        ok = False
    if not ok:
        raise BadkError(
            Code.VIEWER_UNAVAILABLE, f"viewer not healthy at {urlsplit(conf.viewer_url).netloc}"
        )


def prior_run(base: str, run_id: str) -> tuple[str, str | None] | None:
    """None = unknown run (404 RUN_NOT_FOUND); else (status, request mark). Design 22.8."""
    try:
        r = httpx.get(f"{base}/api/v1/runs/{run_id}", timeout=2)
        if r.status_code == httpx.codes.OK:
            body = r.json()
            return body["status"], body["agents"]["coordinator"]["metadata"].get("request")
        if r.status_code == httpx.codes.NOT_FOUND and r.json()["error"]["code"] == "RUN_NOT_FOUND":
            return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise ViewerUnreachable(repr(e)) from e
    raise ViewerUnreachable(f"unexpected answer {r.status_code}")


def _warn_waiting(run_id: str, base: str, mark: str, unreachable: bool) -> None:
    if unreachable:
        log.warning(
            "run request waiting: viewer API unreachable",
            run_id=run_id,
            url=base,
            code="VIEWER_UNAVAILABLE",
            want=mark,
        )
    else:  # no code: the viewer answered, the mark is just not there yet
        log.warning("run request waiting: mark not yet visible", run_id=run_id, url=base, want=mark)


def wait_visible(base: str, run_id: str, mark: str, deadline_sec: float | None = None) -> None:
    """Poll until R shows request `mark`. Deadline set: RunDeferred at the deadline.
    None (post-ack): bounded by PERSIST_WAIT_SEC, then logs an ERROR and returns (the request is
    acked, nothing to requeue); also returns, after one WARNING, once R is terminal or shows an
    unexpected mark, since `mark` can then never appear. Warns only after VIEWER_RETRY_SEC."""
    start = time.monotonic()
    end = start + (PERSIST_WAIT_SEC if deadline_sec is None else deadline_sec)
    last_warn = float("-inf")
    while True:
        unreachable = False
        try:
            seen = prior_run(base, run_id)
            if seen is not None and seen[1] == mark:
                return
            if (
                deadline_sec is None
                and seen is not None
                and (seen[0] != "RUNNING" or seen[1] != "pending")
            ):
                log.warning(
                    "run request wait abandoned: mark can no longer appear",
                    run_id=run_id,
                    status=seen[0],
                    seen_mark=seen[1],
                    want=mark,
                )
                return
        except ViewerUnreachable:
            unreachable = True
        now = time.monotonic()
        if now >= end:
            if deadline_sec is not None:
                raise RunDeferred(f"run {run_id} not visible with request={mark}")
            log.error("acked mark not visible", run_id=run_id, url=base, want=mark)
            return
        if now - start >= VIEWER_RETRY_SEC and now - last_warn >= VIEWER_RETRY_SEC:
            last_warn = now
            _warn_waiting(run_id, base, mark, unreachable)
        time.sleep(PERSIST_POLL_SEC)
