"""Healthcheck heartbeat file (D-28), shared by the writer and `badk serve`."""

import asyncio
from pathlib import Path

import aio_pika
import structlog

# Compose and Dockerfile healthchecks repeat this literal (they cannot import it).
log = structlog.get_logger()
HEARTBEAT = Path("/tmp/badk.heartbeat")
HEARTBEAT_EVERY_S = 5


async def beat(conn: aio_pika.abc.AbstractRobustConnection) -> None:
    while True:
        if conn.connected.is_set():  # is_closed stays False while a robust connection reconnects
            try:
                await asyncio.to_thread(HEARTBEAT.touch)
            except OSError as e:  # keep beating; the healthcheck will show the fault
                log.error("heartbeat_failed", error=repr(e))
        await asyncio.sleep(HEARTBEAT_EVERY_S)
