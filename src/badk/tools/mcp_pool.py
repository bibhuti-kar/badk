"""MCP-1b: one kept MCP session per server, in one asyncio loop on a daemon thread (design 28.1).

Each session lives in its own owner task, which enters and exits `async with Client`, because
the SDK's anyio cancel scopes must exit in the task that entered them. Agent threads only
send requests on it. The pool never resends a call: a dead session is TOOL_ERROR and the
one retry of RES-1 / FLOW-5 is the caller's."""

import asyncio
import atexit
import concurrent.futures
import hashlib
import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, Final

import anyio
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

from badk.engine.envelope import ToolCall
from badk.errors import BadkError, Code, redact
from badk.tools import mcp_config
from badk.tools.definition import ToolDef

_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
# Guards _OWNERS: lookup-or-create, supersede and drop are each one step under it, so two
# first calls never start one server twice. Held only in sync code (no await inside).
_OWNERS_LOCK = threading.Lock()
_OWNERS: dict[str, "_Owner"] = {}
_DEAD = (anyio.ClosedResourceError, anyio.BrokenResourceError)
START_MAX_SEC: Final = 120  # a start (npx download included) that never answers is dropped
HTTP_TIMES: Final = httpx2.Timeout(30, read=300)  # the SDK's values; httpx's default is 5 s


class _Owner:
    def __init__(self, key: str, name: str, spec: dict[str, Any]) -> None:
        self.key, self.name = key, name
        self.inflight, self.superseded = 0, False
        self.client: Client | None = None
        loop = asyncio.get_running_loop()
        self.ready: asyncio.Future[None] = loop.create_future()
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self._own(spec))
        loop.call_later(START_MAX_SEC, self._give_up)

    async def _own(self, spec: dict[str, Any]) -> None:
        try:
            async with _target(spec) as target, Client(target, read_timeout_seconds=None) as c:
                self.client = c
                self.ready.set_result(None)
                await self.stop.wait()
        except BaseException as e:  # a failed start reaches the waiting callers
            if not self.ready.done():
                self.ready.set_exception(e)
        finally:
            self.stop.set()
            if not self.ready.done():
                self.ready.cancel()

    def _give_up(self) -> None:
        if not self.ready.done():  # MCP-1.6: the timeout never cancels a start; this does
            self.ready.set_exception(TimeoutError(f"no answer in {START_MAX_SEC} s"))
            self.drop()
            self.task.cancel()

    def drop(self) -> None:
        self.stop.set()
        with _OWNERS_LOCK:
            if _OWNERS.get(self.key) is self:
                del _OWNERS[self.key]

    def release(self) -> None:
        with _OWNERS_LOCK:
            self.inflight -= 1
            if self.superseded and self.inflight == 0:  # MCP-1.7: drained, so close it
                self.stop.set()


@asynccontextmanager
async def _target(spec: dict[str, Any]) -> AsyncIterator[Any]:
    """Stdio parameters, or a streamable HTTP transport with the headers and SDK timeouts."""
    if "command" in spec:
        yield StdioServerParameters(
            command=spec["command"],
            args=spec.get("args", []),
            env=spec.get("env"),
            cwd=spec.get("cwd") or None,
        )
        return
    async with httpx2.AsyncClient(headers=spec.get("headers") or None, timeout=HTTP_TIMES) as http:
        yield streamable_http_client(spec["url"], http_client=http)


def _loop() -> asyncio.AbstractEventLoop:
    global _LOOP  # noqa: PLW0603 - started once, under _LOCK
    with _LOCK:
        if _LOOP is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="badk-mcp", daemon=True).start()
            _LOOP = loop
        return _LOOP


def _close_all() -> None:
    """MCP-1.7 atexit: cancel every task on the pool loop (owners, superseded ones and starts
    still running); each `async with Client` exit ends its server. The wait is bounded."""
    if _LOOP is None:
        return

    async def close() -> None:
        if tasks := asyncio.all_tasks() - {asyncio.current_task()}:
            for t in tasks:
                t.cancel()
            await asyncio.wait(tasks, timeout=3)

    concurrent.futures.wait([asyncio.run_coroutine_threadsafe(close(), _LOOP)], timeout=4)


atexit.register(_close_all)


def _owner(name: str, spec: dict[str, Any]) -> _Owner:
    """The live owner for this spec, counted in flight. A new spec for `name` supersedes the
    old owners: no new call reaches them, and each closes once its calls end."""
    key = name + ":" + hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    with _OWNERS_LOCK:
        o = _OWNERS.get(key)
        if o is None or o.task.done():
            for old in [x for x in _OWNERS.values() if x.name == name and x.key != key]:
                del _OWNERS[old.key]
                old.superseded = True
                if old.inflight == 0:
                    old.stop.set()
            o = _OWNERS[key] = _Owner(key, name, spec)
        o.inflight += 1
    return o


def _why(e: BaseException) -> str:
    """The first leaf of an exception group (the SDK wraps HTTP errors), masked and cut."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return redact(f"{type(e).__name__}: {e}")[:500]


def _closed(o: _Owner) -> BadkError:
    o.drop()
    return BadkError(Code.TOOL_ERROR, f"mcp server {o.name} closed; the next call starts it again")


async def _request[T](
    name: str, spec: dict[str, Any], tool: str, work: Callable[[Client], Awaitable[T]]
) -> T:
    """Run `work(client)` on the kept session of `name`, starting it first when needed."""
    o = _owner(name, spec)
    try:
        return await _on(o, tool, work)
    finally:
        o.release()


async def _on[T](o: _Owner, tool: str, work: Callable[[Client], Awaitable[T]]) -> T:
    try:
        await asyncio.shield(o.ready)  # a caller timeout cancels this wait, never the start
    except asyncio.CancelledError:
        if o.task.done():  # not our cancel: the start died
            raise _closed(o) from None
        raise
    except Exception as e:
        o.drop()
        why = f"{tool}: cannot start mcp server {o.name}: {_why(e)}"
        raise BadkError(Code.TOOL_ERROR, why) from e
    assert o.client is not None
    try:
        return await work(o.client)
    except BadkError:
        raise
    except (*_DEAD, MCPError) as e:
        if not isinstance(e, MCPError) or e.code == CONNECTION_CLOSED or o.task.done():
            raise _closed(o) from None
        raise BadkError(Code.TOOL_ERROR, f"{tool}: {redact(e.message)[:500]}") from e
    except Exception as e:  # pydantic, httpx, ...: a tool error, masked, so the retry applies
        raise BadkError(Code.TOOL_ERROR, f"{tool}: {_why(e)}") from e


def _submit[T](coro: Coroutine[Any, Any, T], tool: ToolDef) -> T:
    fut = asyncio.run_coroutine_threadsafe(coro, _loop())
    try:
        return fut.result(tool.timeout)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        raise BadkError(Code.TOOL_TIMEOUT, f"{tool.name}: over {tool.timeout} s") from None


async def _list(c: Client) -> dict[str, Any]:
    """Every page of tools/list; refused once the pages pass the byte cap or a cursor repeats."""
    from badk.tools.execute import MAX_OUTPUT_BYTES  # noqa: PLC0415 - execute imports us lazily

    tools: list[Any] = []
    cursor: str | None = None
    seen, size = set[str](), 0
    while True:
        page = await c.list_tools(cursor=cursor)
        got = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in page.tools]
        tools += got
        size += len(json.dumps(got).encode())  # linear, approximate: execute_mcp checks exactly
        if size > MAX_OUTPUT_BYTES:
            raise BadkError(Code.TOOL_ERROR, f"tools/list: over {MAX_OUTPUT_BYTES} bytes")
        if not (cursor := page.next_cursor):
            return {"tools": tools}
        if cursor in seen:
            raise BadkError(Code.TOOL_ERROR, f"tools/list: cursor {cursor[:40]!r} repeats")
        seen.add(cursor)


def _text(body: dict[str, Any]) -> str:
    return "\n".join(i["text"] for i in body.get("content", []) if i.get("type") == "text")


def execute_mcp(call: ToolCall, tool: ToolDef) -> dict[str, Any]:
    """One mcp call's tool result (modes: design 28.1); an unknown server starts nothing."""
    from badk.tools.execute import MAX_OUTPUT_BYTES  # noqa: PLC0415 - execute imports us lazily

    args = call.args
    server = tool.server or str(args.get("server", ""))
    spec = mcp_config.spec_for(server)
    if tool.action == "list":
        body: dict[str, Any] = _submit(_request(server, spec, tool.name, _list), tool)
    else:
        name = tool.tool or str(args.get("tool", ""))
        arguments = args if tool.tool else args.get("arguments", {})
        if not isinstance(arguments, dict):  # before anything starts
            raise BadkError(Code.TOOL_ERROR, f"{tool.name}: arguments must be an object")

        async def work(c: Client) -> dict[str, Any]:
            r = await c.call_tool(name, arguments)
            return r.model_dump(mode="json", by_alias=True, exclude_none=True)

        body = _submit(_request(server, spec, name, work), tool)
        if body.get("isError"):
            raise BadkError(Code.TOOL_ERROR, f"{name}: MCP tool error: {redact(_text(body))[:500]}")
    if len(json.dumps(body).encode()) > MAX_OUTPUT_BYTES:
        raise BadkError(Code.TOOL_ERROR, f"{tool.name}: output over {MAX_OUTPUT_BYTES} bytes")
    return body
