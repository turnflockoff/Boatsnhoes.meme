"""Hostile-input limits for the coordinator API (`input-limits-v1`, code audit first pass).

Before this, the coordinator trusted the web framework to cope with any request body:
  * there was no size limit, so anyone (no login needed) could send a 1 GB body and the
    coordinator read all of it into memory before the field checks refused it;
  * a body of a few thousand nested brackets (`[[[[...`) made Python's JSON reader run out
    of recursion depth: a 500 error and a stack trace in the log, on any route;
  * a refused body was echoed back inside the 422 error ("input": ...), which could be a
    secret typed into the wrong field or a 4 MB string, and a number like 1e999 (infinity)
    in that echo couldn't be turned into JSON at all, so the 422 itself crashed into a 500.

This module adds three small guards:
  BodyLimit           ASGI middleware: refuses a body over `max_bytes` with 413, both when
                      the Content-Length header says so (nothing is read) and when a body
                      without a length streams past the limit (reading stops there); then
                      refuses JSON nested deeper than MAX_JSON_DEPTH with 400.
  json_depth          how deeply brackets nest outside strings, without parsing the JSON.
  validation_error    422 handler that sends back only type, location and message for each
                      problem, never the value that was sent.

The middleware sits inside the request logger and rate limiter (see app/main.py), so
refused requests are still logged (path and status only, never the body) and counted.
"""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

INPUT_LIMITS_VERSION = "input-limits-v1"

DEFAULT_MAX_BODY_BYTES = 256 * 1024
# Bounds for MAX_BODY_BYTES. The largest honest body (a hidden test at every field's
# maximum, every character an escaped emoji) is about 150 KB, so 16 KiB is only for tests.
MIN_BODY_BYTES = 16 * 1024
MAX_BODY_BYTES = 4 * 1024 * 1024
# No coordinator request nests deeper than 2 (an object holding a list of names).
MAX_JSON_DEPTH = 32
# Biggest timestamp or period bound accepted from a client: 2^53, far past any real date,
# and well inside what SQLite can store (2^63), so a huge number is a 422, not a crash.
MAX_TIMESTAMP = 2 ** 53

_STRING = re.compile(rb'"(?:[^"\\]|\\.)*"', re.DOTALL)
_NOT_BRACKETS = bytes(b for b in range(256) if b not in b"[]{}")

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def json_depth(raw: bytes) -> int:
    """The deepest bracket nesting in `raw`, ignoring brackets inside JSON strings.
    Doesn't check the JSON is valid; the framework's parser does that afterwards."""
    brackets = _STRING.sub(b"", raw).translate(None, _NOT_BRACKETS)
    depth = deepest = 0
    for b in brackets:
        depth += 1 if b in (0x5B, 0x7B) else -1  # [ {  vs  ] }
        deepest = max(deepest, depth)
    return deepest


def _refuse(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


class BodyLimit:
    def __init__(self, app: Callable[..., Awaitable[None]], max_bytes: int,
                 max_depth: int = MAX_JSON_DEPTH) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.max_depth = max_depth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        too_large = _refuse(413, f"request body too large (limit {self.max_bytes} bytes)")
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None:
            if not length.isdigit():
                await _refuse(400, "invalid Content-Length header")(scope, receive, send)
                return
            if int(length) > self.max_bytes:
                await too_large(scope, receive, send)
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":  # client went away
                await self.app(scope, _replay(bytes(body), message, receive), send)
                return
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await too_large(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        if body and json_depth(bytes(body)) > self.max_depth:
            await _refuse(400, f"request body is nested too deeply "
                               f"(limit {self.max_depth} levels)")(scope, receive, send)
            return
        await self.app(scope, _replay(bytes(body), None, receive), send)


def _replay(body: bytes, pending: dict[str, Any] | None, receive: Receive) -> Receive:
    """A `receive` that hands the already-read body over once, then any message that
    interrupted reading (a disconnect), then whatever the client sends next."""
    queue: list[dict[str, Any]] = [{"type": "http.request", "body": body, "more_body": False}]
    if pending is not None:
        queue.append(pending)

    async def replay() -> dict[str, Any]:
        if queue:
            return queue.pop(0)
        return await receive()
    return replay


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 with what was wrong and where, never what was sent."""
    errors = [{"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": e.get("msg")}
              for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})
