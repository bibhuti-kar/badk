"""FastAPI entry point: viewer API and HTTP ingress."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from html import escape
from typing import Any, Final

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from badk.api.runs import router as runs_router
from badk.config import load_settings
from badk.errors import BadkError, Code
from badk.logging_setup import configure_logging
from badk.persistence.db import ensure_schema, make_engine
from badk.persistence.events import close_quietly, open_queue
from badk.viewer import pages

log = structlog.get_logger()
_STATUS = {
    Code.RUN_NOT_FOUND: 404,
    Code.VALIDATION_ERROR: 422,
    Code.QUEUE_UNAVAILABLE: 503,
}
_HTTP: Final = {  # status -> (code, fixed message); never exc.detail
    404: (Code.NOT_FOUND, "not found"),
    405: (Code.METHOD_NOT_ALLOWED, "method not allowed"),
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.settings = load_settings()
    configure_logging(app.state.settings.log_level)
    engine = make_engine(app.state.settings)
    await ensure_schema(engine)
    app.state.engine = engine
    app.state.publish_lock = asyncio.Lock()
    app.state.conn, app.state.channel = await open_queue(app.state.settings.rabbitmq_url)
    try:
        yield
    finally:
        try:
            await close_quietly(app.state.conn)
        finally:
            await engine.dispose()


app = FastAPI(title="badk", lifespan=lifespan)
app.include_router(runs_router)
app.include_router(pages.router)
app.mount("/static", StaticFiles(directory=pages.STATIC_DIR), name="static")


def _error_response(request: Request, err: BadkError, status: int) -> Response:
    """JSON envelope under /api/, an HTML error page elsewhere (status preserved)."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(err.envelope(), status_code=status)
    page = (
        f"<!doctype html><title>{err.code}</title><h1>{err.code}</h1><p>{escape(err.message)}</p>"
    )
    return HTMLResponse(page, status_code=status)


@app.exception_handler(BadkError)
async def _badk_error(request: Request, exc: BadkError) -> Response:
    return _error_response(request, exc, _STATUS.get(exc.code, 500))


@app.exception_handler(StarletteHTTPException)  # unknown route, wrong method
async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    code, msg = _HTTP.get(exc.status_code) or (
        (Code.VALIDATION_ERROR, "request refused")
        if exc.status_code < 500
        else (Code.INTERNAL_ERROR, "internal error")
    )
    return JSONResponse(  # JSON on every path: HTTP-2.1 wants the envelope for /nope too
        BadkError(code, msg).envelope(), status_code=exc.status_code, headers=exc.headers
    )


def _field(e: Mapping[str, Any]) -> str:
    """Location without the leading body/query/path segment; `body` for unparseable JSON."""
    loc = e["loc"]
    if e["type"] == "json_invalid":
        return "body"
    return ".".join(map(str, loc[1:])) or str(loc[0])


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    details = [{"field": _field(e), "message": e["msg"]} for e in exc.errors()]
    err = BadkError(Code.VALIDATION_ERROR, "invalid request", details)
    return _error_response(request, err, 422)


@app.exception_handler(Exception)
async def _internal_error(request: Request, exc: Exception) -> Response:
    log.error("unhandled_exception", code=Code.INTERNAL_ERROR, path=request.url.path, exc_info=exc)
    return _error_response(request, BadkError(Code.INTERNAL_ERROR, "internal error"), 500)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe used by the container HEALTHCHECK."""
    return {"status": "ok"}
