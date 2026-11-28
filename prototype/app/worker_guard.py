"""Worker-side guards against a hostile coordinator or network (`worker-input-v1`).

The coordinator's API got a hostile-input fuzz in 0.5.52 (`input-limits-v1`). This is the
other side of the wire: the worker runs on a volunteer's own machine, so a fake coordinator,
a typo'd URL or someone in the middle of the connection must not be able to crash it, fill
its memory, scribble on the owner's terminal or steal its key. Before this, `demo_worker.py`:
  * read every reply whole into memory, with no size limit (a 5 GB reply would be read),
    and let httpx unpack compressed replies (1 MB of gzip can unpack to 1 GB);
  * crashed with a stack trace on a reply that wasn't JSON, was nested 100,000 brackets
    deep, held a number with 5,000 digits, or was a list where it expected an object;
  * printed the coordinator's error messages and a refused job's id straight to the
    terminal, so terminal control codes (colours, cursor moves, a fake prompt, a changed
    window title) chosen by whoever answered went straight to the owner's screen;
  * saved whatever came back as the worker key, newlines and all;
  * spun in a tight loop, as fast as the network allowed, when every job it was handed was
    refused;
  * would send its worker key over plain `http://` to any address, readable by anyone on
    the way (signatures protect the *jobs*, not the key).

Guards here, used by demo_worker.py:
  check_url        https, or plain http only to this machine, unless the owner opts in.
  request_json     one request; refuses a reply bigger than MAX_RESPONSE_BYTES (before
                   reading if it says its size, otherwise while reading), any compressed
                   reply, one that takes longer than RESPONSE_DEADLINE_SECONDS in total, JSON
                   nested deeper than MAX_JSON_DEPTH, NaN/Infinity and huge numbers.
  say              makes any text from the coordinator safe to print: control and invisible
                   characters become `?`, and it is cut to a short length.
  check_worker_key / check_job_id   shapes the worker accepts before saving or using them.
  refusal_wait     how long to wait after refused jobs, growing to MAX_REFUSAL_WAIT_SECONDS.

Everything the coordinator sends is still checked the old way too (signatures, hashes,
pins); these guards only make sure that checking can't be skipped by crashing first.
"""
from __future__ import annotations

import ipaddress
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from app.input_limits import MAX_JSON_DEPTH, json_depth
# Terminal-safe printing lives in app/screen.py (no third-party imports, so the site and
# dependency checkers can use it too); re-exported here for the many callers that import it.
from app.screen import MAX_SAY_CHARS, MAX_SCREEN_LINE_CHARS, say, say_lines  # noqa: F401

WORKER_GUARD_VERSION = "worker-input-v1"

# The largest honest reply is a job: the coordinator refuses request bodies over 4 MiB at
# its highest setting (256 KiB by default), and a job is that payload plus a few short
# fields. 8 MiB leaves room for any coordinator setting; nothing honest comes close.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Whole reply, start to last byte. httpx's own timeout is per read, so a server sending a
# byte every few seconds could otherwise keep the worker waiting for ever.
RESPONSE_DEADLINE_SECONDS = 60.0
# After a refused job the worker waits 5 s, then 10, 20, 40, then 60 s each time.
FIRST_REFUSAL_WAIT_SECONDS = 5
MAX_REFUSAL_WAIT_SECONDS = 60

WORKER_KEY_PATTERN = re.compile(r"bnhw_[A-Za-z0-9_-]{32,128}")
# Coordinator job ids are UUIDs; anything shaped like a path (`/`, `..`, `?`) is refused
# before it is put into a URL.
JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}")
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


class CoordinatorError(Exception):
    """The coordinator (or whatever answered) sent something the worker won't use."""


@dataclass
class Reply:
    status_code: int
    headers: httpx.Headers
    data: Any  # parsed JSON, or None if the body was empty or not JSON

    def json(self) -> Any:
        """Same name as httpx.Response.json(), so callers read naturally."""
        return self.data

    def detail(self) -> str:
        """The coordinator's error message, safe to print."""
        if isinstance(self.data, dict) and "detail" in self.data:
            value = self.data["detail"]
            return say(value if isinstance(value, str) else json.dumps(value))
        return "(no details)"


def is_loopback(host: str) -> bool:
    host = host.strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str, allow_plain_http: bool = False) -> str:
    """Return `url` if the worker may talk to it, else raise CoordinatorError.
    https anywhere; plain http only to this machine unless `allow_plain_http` (a test lab
    on a private network): over plain http the worker key travels readable by anyone on
    the way, and whoever reads it can claim and submit jobs as this worker."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError as exc:
        raise CoordinatorError(f"bad coordinator URL: {exc}") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise CoordinatorError("coordinator URL must start with https:// (or http:// for "
                               "this machine) and name a host")
    if parts.username is not None or parts.password is not None:
        raise CoordinatorError("coordinator URL must not contain a user name or password")
    if parts.scheme == "http" and not is_loopback(host) and not allow_plain_http:
        raise CoordinatorError(
            f"refusing plain http:// to {say(host)}: the worker key would travel unencrypted. "
            "Use https://, or pass --allow-plain-http for a test lab you control.")
    return url


def finite_float(text: str) -> float:
    """`parse_float` hook: `1e400` would otherwise quietly become infinity."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number too big")
    return value


def parse_json(raw: bytes) -> Any:
    """Parse a reply body, refusing what could crash or mislead the worker.
    Returns None for an empty body; raises CoordinatorError for anything else that isn't
    plain JSON."""
    if not raw.strip():
        return None
    if json_depth(raw) > MAX_JSON_DEPTH:
        raise CoordinatorError(f"reply nested deeper than {MAX_JSON_DEPTH} levels")

    def no_constants(name: str) -> Any:
        raise ValueError(f"{name} is not allowed")

    try:
        return json.loads(raw.decode("utf-8"), parse_constant=no_constants,
                          parse_float=finite_float)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # ValueError covers bad JSON, NaN/Infinity, numbers like 1e400 that would become
        # infinity (`rig-report-v1`), and Python's limit on 4,300+ digit numbers.
        raise CoordinatorError(f"reply is not valid JSON ({say(type(exc).__name__)})") from exc


def request_json(client: httpx.Client, method: str, url: str, *,
                 max_bytes: int = MAX_RESPONSE_BYTES,
                 deadline: float = RESPONSE_DEADLINE_SECONDS,
                 clock: Callable[[], float] = time.monotonic, **kwargs: Any) -> Reply:
    """Send one request and read the reply within the limits above. A non-JSON body on an
    error status is fine (`data` is None); on a 2xx status it is a CoordinatorError."""
    headers = dict(kwargs.pop("headers", None) or {})
    headers["Accept-Encoding"] = "identity"  # ask for an uncompressed reply
    started = clock()
    try:
        request = client.build_request(method, url, headers=headers, **kwargs)
        response = client.send(request, stream=True, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise CoordinatorError(f"could not reach the coordinator ({type(exc).__name__})") from exc
    try:
        body = read_limited(response, max_bytes=max_bytes, deadline=deadline,
                            started=started, clock=clock)
    finally:
        response.close()
    try:
        data = parse_json(body)
    except CoordinatorError:
        if 200 <= response.status_code < 300:
            raise
        data = None
    if 200 <= response.status_code < 300 and not isinstance(data, dict):
        raise CoordinatorError("reply is not a JSON object")
    return Reply(response.status_code, response.headers, data)


def read_limited(response: httpx.Response, *, max_bytes: int, deadline: float, started: float,
                 clock: Callable[[], float]) -> bytes:
    """Read a streamed reply's raw body within the limits, or raise CoordinatorError:
    compressed replies refused, at most `max_bytes` (checked against Content-Length before
    reading, then while reading), done within `deadline` seconds of `started`. Doesn't
    close the response; the caller does. Shared with app/operator_client.py."""
    try:
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise CoordinatorError(f"compressed reply ({say(encoding, 20)}) refused")
        declared = response.headers.get("content-length", "").strip()
        if declared.isdigit() and int(declared) > max_bytes:
            raise CoordinatorError(f"reply too big ({declared} bytes; limit {max_bytes})")
        body = bytearray()
        try:
            for chunk in response.iter_raw():
                body += chunk
                if len(body) > max_bytes:
                    raise CoordinatorError(f"reply too big (over {max_bytes} bytes)")
                if clock() - started > deadline:
                    raise CoordinatorError(f"reply too slow (over {deadline:g} s)")
        except httpx.StreamConsumed:
            # Only in-process transports (tests) hand over a reply already in memory.
            body = bytearray(response.content)
            if len(body) > max_bytes:
                raise CoordinatorError(f"reply too big (over {max_bytes} bytes)") from None
    except httpx.HTTPError as exc:
        raise CoordinatorError(f"reply cut off ({type(exc).__name__})") from exc
    return bytes(body)


def check_worker_key(key: Any) -> str:
    """The worker key exactly as the coordinator issues it (`bnhw_` + 43 URL-safe
    characters), or CoordinatorError. Checked before it is saved to disk."""
    if not isinstance(key, str) or not WORKER_KEY_PATTERN.fullmatch(key):
        raise CoordinatorError("coordinator sent a worker key in the wrong shape")
    return key


def check_job_id(job_id: Any) -> str:
    if not isinstance(job_id, str) or not JOB_ID_PATTERN.fullmatch(job_id):
        raise CoordinatorError("job id in the wrong shape")
    return job_id


def refusal_wait(refusals_in_a_row: int) -> int:
    """Seconds to wait after the n-th refused job in a row (n >= 1)."""
    if refusals_in_a_row < 1:
        return 0
    return min(FIRST_REFUSAL_WAIT_SECONDS * 2 ** min(refusals_in_a_row - 1, 10),
               MAX_REFUSAL_WAIT_SECONDS)
