"""http tool executor (design 9.2): render the request template, send it, return the response."""

import concurrent.futures
import contextlib
import ipaddress
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

import httpx

from badk.engine.envelope import ToolCall
from badk.errors import BadkError, Code, redact, redact_cut
from badk.tools.definition import ENV_REF, VAR, ToolDef
from badk.tools.execute import MAX_OUTPUT_BYTES, lookup, render, to_text
from badk.tools.tool_env import fill


def _leaf(v: object, args: Mapping[str, Any]) -> object:
    """Header/body leaf: exactly `{{x}}` takes the JSON value, else string substitution."""
    if isinstance(v, dict):
        return {k: _leaf(x, args) for k, x in v.items()}
    if isinstance(v, list):
        return [_leaf(x, args) for x in v]
    if not isinstance(v, str):
        return v
    if m := VAR.fullmatch(v):
        return lookup(m[1], args)
    return render(v, args, str)


def _name(t: object, default: str) -> str:
    """Name a non-finite number after its placeholder when the leaf is exactly `{{x}}`."""
    m = VAR.fullmatch(t) if isinstance(t, str) else None
    return m[0] if m else default


def _head(key: str, t: object, args: Mapping[str, Any], tool: str) -> str:
    """Header `key`. `{{env.NAME}}` is filled here only (TOOL-5; load refuses it elsewhere).
    A keyed tool has a fixed host and follows no redirect, so the value goes to that host only."""
    if isinstance(t, str) and ENV_REF.search(t):
        return fill(tool, t, lambda s: render(s, args, str))
    return to_text(_name(t, f"header {key}"), _leaf(t, args))


def _headers_body(
    req: Mapping[str, Any], args: Mapping[str, Any], name: str
) -> tuple[dict[str, str], str | None]:
    tmpl = req.get("headers") or {}
    if isinstance(tmpl, dict):
        headers = {k: _head(k, t, args, name) for k, t in tmpl.items()}
    else:  # headers: "{{h}}" takes a whole object
        hv = _leaf(tmpl, args)
        if not isinstance(hv, dict):
            raise BadkError(Code.TOOL_ERROR, f"{name}: headers must be an object")
        headers = {k: to_text(_name(tmpl, "headers"), x) for k, x in hv.items()}
    body = _leaf(req["body"], args) if "body" in req else None
    return headers, None if body is None else to_text(_name(req["body"], "body"), body)


def _quote(v: str) -> str:
    if v in {".", ".."}:  # httpx normalises dot segments: /api/.. would hit /
        raise BadkError(Code.TOOL_ERROR, f"url value may not be {v!r}")
    return urllib.parse.quote(v, safe="")


def _url(template: str, args: Mapping[str, Any]) -> str:
    """A url that is exactly `{{x}}` is taken as-is (http/https string); else values are quoted."""
    if m := VAR.fullmatch(template):
        v = lookup(m[1], args)
        u = urllib.parse.urlsplit(v) if isinstance(v, str) else None
        if not isinstance(v, str) or u is None or u.scheme not in {"http", "https"} or not u.netloc:
            raise BadkError(Code.TOOL_ERROR, f"url {template} must be an http(s) URL string")
        return v
    return render(template, args, _quote)


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_V4_COMPAT = ipaddress.ip_network("::/96")


def _resolve(host: str) -> list[str]:
    return [str(i[4][0]) for i in socket.getaddrinfo(host, None)]


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Globally routable unicast; IPv6 forms that embed an IPv4 address are judged by it."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in _NAT64 or ip in _V4_COMPAT:
            ip = ipaddress.IPv4Address(ip.packed[-4:])
        elif ip in _NAT64_LOCAL:
            return False
    return ip.is_global and not ip.is_multicast


def _via_proxy(url: httpx.URL) -> bool:
    """Would the environment send this URL through a proxy (the proxy resolves the host)?"""
    proxies = urllib.request.getproxies()
    return bool(proxies.get(url.scheme) or proxies.get("all")) and not urllib.request.proxy_bypass(
        url.host
    )


def _resolved(host: str, tool: ToolDef, deadline: float) -> list[str]:
    """getaddrinfo in a worker thread, bounded by the request deadline."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(_resolve, host).result(timeout=max(deadline - time.monotonic(), 0))
    except concurrent.futures.TimeoutError:
        raise BadkError(Code.TOOL_TIMEOUT, f"{tool.name}: over {tool.timeout} s") from None
    except OSError as e:
        raise BadkError(Code.TOOL_HTTP_ERROR, f"cannot resolve {host}: {e}") from e
    finally:
        ex.shutdown(wait=False)


def _deny_private(request: httpx.Request, tool: ToolDef, deadline: float) -> None:
    """Refuse non-public targets (SSRF). A proxied URL is not resolved here (the proxy resolves
    it): only IP literals and `localhost` are judged.
    ponytail: resolves separately from the connect, so DNS rebinding can slip past; see the
    known limits in design 10.2."""
    host = request.url.host
    if host == "localhost" or host.endswith(".localhost"):
        addrs = ["127.0.0.1"]
    else:
        try:
            addrs = [str(ipaddress.ip_address(host))]
        except ValueError:
            addrs = [] if _via_proxy(request.url) else _resolved(host, tool, deadline)
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not _public(ip):
            raise BadkError(
                Code.TOOL_ERROR, f"{host} resolves to non-public {ip}; set allow_private_network"
            )


def _hop(request: httpx.Request, tool: ToolDef, deadline: float) -> None:
    """Request hook, run on every hop incl. redirects: one wall-clock budget for the whole call."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BadkError(Code.TOOL_TIMEOUT, f"{tool.name}: over {tool.timeout} s")
    if not tool.allow_private_network:
        _deny_private(request, tool, deadline)
        remaining = deadline - time.monotonic()
    request.extensions["timeout"] = dict.fromkeys(("connect", "read", "write", "pool"), remaining)


def _stop(r: httpx.Response, fired: threading.Event) -> None:
    """Timer callback: wake a read blocked in recv (shutdown, then close)."""
    fired.set()
    with contextlib.suppress(Exception):
        sock = r.extensions["network_stream"].get_extra_info("socket")
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        r.close()


def _read(r: httpx.Response, tool: ToolDef, deadline: float) -> tuple[bytes, bool]:
    """Body up to the cap (+1 probes for more); a timer cuts a stalled read at the deadline."""
    fired = threading.Event()
    timer = threading.Timer(max(deadline - time.monotonic(), 0), _stop, (r, fired))
    timer.start()
    buf = bytearray()
    try:
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > MAX_OUTPUT_BYTES:
                break
    except httpx.HTTPError:
        if not fired.is_set():
            raise
    finally:
        timer.cancel()
    if fired.is_set():
        raise BadkError(Code.TOOL_TIMEOUT, f"{tool.name}: over {tool.timeout} s")
    return bytes(buf[:MAX_OUTPUT_BYTES]), len(buf) > MAX_OUTPUT_BYTES


def _redacted(resp: dict[str, Any]) -> dict[str, Any]:
    """The response for BadkError.details: secrets and URL credentials stripped."""
    h = {
        k: [redact(x) for x in v] if isinstance(v, list) else redact(v)
        for k, v in resp["headers"].items()
    }
    return resp | {"headers": h, "body": redact(resp["body"])}


def _headers(r: httpx.Response) -> dict[str, str | list[str]]:
    """Lower-cased names; a repeated name (Set-Cookie) becomes a list of values."""
    out: dict[str, str | list[str]] = {}
    for k, v in r.headers.multi_items():
        prev = out.get(k)
        if prev is None:
            out[k] = v
        else:
            out[k] = [*prev, v] if isinstance(prev, list) else [prev, v]
    return out


def execute_http(call: ToolCall, tool: ToolDef) -> dict[str, Any]:
    req = tool.request or {}
    method = str(req.get("method", "GET"))
    url = _url(str(req.get("url", "")), call.args)
    headers, content = _headers_body(req, call.args, tool.name)
    deadline = time.monotonic() + tool.timeout
    hooks: dict[str, list[Callable[..., Any]]] = {
        "request": [partial(_hop, tool=tool, deadline=deadline)]
    }
    try:
        # ponytail: client per call; lru_cache a client if connection reuse ever matters
        with (
            httpx.Client(
                timeout=tool.timeout, follow_redirects=not tool.env, event_hooks=hooks
            ) as c,
            c.stream(method, url, headers=headers, content=content) as r,
        ):
            data, cut = _read(r, tool, deadline)
    except httpx.TimeoutException:
        raise BadkError(Code.TOOL_TIMEOUT, f"{tool.name}: over {tool.timeout} s") from None
    except httpx.HTTPError as e:
        raise BadkError(Code.TOOL_HTTP_ERROR, f"{tool.name}: {e}") from e
    text = data.decode(errors="replace")
    text = redact_cut(text) if cut else text  # the cap may keep the first part of a secret
    if not r.is_success:
        raise BadkError(
            Code.TOOL_HTTP_ERROR,
            f"{tool.name}: HTTP {r.status_code}: {redact(text)[:500]}",
            [
                _redacted(
                    {
                        "status": r.status_code,
                        "headers": _headers(r),
                        "body": text,
                        "truncated": cut,
                    }
                )
            ],
        )
    return {"status": r.status_code, "headers": _headers(r), "body": text, "truncated": cut}
