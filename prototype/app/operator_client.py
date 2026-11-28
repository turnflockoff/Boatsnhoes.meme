"""Operator tools against a mistyped or hostile coordinator URL (`operator-client-v1`).

Two command-line tools send the operator token over the network: `app.challenge_pool
issue|issue-leads` (which also sends hidden tests *with their expected answers*) and
`app.rehearsal --url`. The worker got guards against a hostile coordinator in 0.5.53
(`app/worker_guard.py`); these two tools didn't, although what they carry is worth more
than a worker key: the operator token can queue jobs, revoke rigs and pause the
coordinator, and a leaked answer lets a rig pass that hidden test. Shown before this fix,
against a fake coordinator on another address of this machine:
  * `--url http://<another machine>` sent the operator token and every expected answer
    unencrypted, readable by anyone on the way;
  * a 1 MB gzip reply was unpacked to 1 GB (2.2 GB of memory), and the test was then
    counted as issued with job id `None`;
  * a reply that never ended was read until the computer ran out of memory (killed at
    about 6 GB);
  * an error message with terminal control codes cleared the screen, changed the window
    title and printed a fake `Refusing: run ... | sh` line;
  * a reply nested 200,000 brackets deep crashed it with a RecursionError traceback.

OperatorClient is an httpx.Client that the tools use unchanged (`client.post(...)`,
`response.json()`), with the worker's limits applied to every reply:
  * the URL must be https, or plain http to this machine only, unless the operator passes
    `--allow-plain-http` for a test lab they control; no user name or password in it;
  * redirects are never followed (the token and answers would go wherever they point);
  * no compressed replies, at most MAX_RESPONSE_BYTES, whole reply within
    RESPONSE_DEADLINE_SECONDS; breaking a limit raises ReplyRefused (an httpx.HTTPError),
    so the tools' existing "lost contact, this test stays used" handling covers it;
  * `response.json()` refuses JSON nested deeper than 32 levels, NaN/Infinity and huge
    numbers with UnsafeJSON, a ValueError, which the tools already treat as "not JSON".
Text from the coordinator is printed through `worker_guard.say()` by the tools themselves.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from app.worker_guard import (MAX_RESPONSE_BYTES, RESPONSE_DEADLINE_SECONDS, CoordinatorError,
                              check_url, parse_json, read_limited)

OPERATOR_CLIENT_VERSION = "operator-client-v1"


class ReplyRefused(httpx.RequestError):
    """The reply broke a limit (too big, too slow, compressed). Nothing in it was used."""


class UnsafeJSON(ValueError):
    """The reply's body isn't JSON the tools will read."""


class GuardedResponse(httpx.Response):
    """An httpx.Response whose body was read within the limits and whose `.json()` uses
    the worker's careful parser."""

    def json(self, **kwargs: Any) -> Any:
        try:
            data = parse_json(self.content)
        except CoordinatorError as exc:
            raise UnsafeJSON(str(exc)) from None
        if data is None:
            raise UnsafeJSON("empty reply")
        return data


def check_operator_url(url: str, allow_plain_http: bool = False) -> str:
    """`url` if the operator tools may send the token there, else ValueError (one line)."""
    try:
        return check_url(url, allow_plain_http)
    except CoordinatorError as exc:
        raise ValueError(str(exc).replace("the worker key would", "the operator token "
                                          "(and any hidden-test answers) would")) from None


class OperatorClient(httpx.Client):
    """httpx.Client for operator tools: checked URL, no redirects, limited replies."""

    def __init__(self, base_url: str, *, allow_plain_http: bool = False,
                 max_bytes: int = MAX_RESPONSE_BYTES,
                 deadline: float = RESPONSE_DEADLINE_SECONDS,
                 clock: Callable[[], float] = time.monotonic, **kwargs: Any) -> None:
        check_operator_url(base_url, allow_plain_http)
        kwargs.setdefault("timeout", 15)
        super().__init__(base_url=base_url, **kwargs)
        self._max_bytes = max_bytes
        self._deadline = deadline
        self._clock = clock

    def send(self, request: httpx.Request, *, stream: bool = False,
             auth: Any = httpx.USE_CLIENT_DEFAULT,
             follow_redirects: Any = httpx.USE_CLIENT_DEFAULT) -> httpx.Response:
        if stream:
            raise TypeError("OperatorClient reads every reply itself; stream=True isn't offered")
        request.headers["Accept-Encoding"] = "identity"  # ask for an uncompressed reply
        started = self._clock()
        response = super().send(request, stream=True, auth=auth, follow_redirects=False)
        try:
            body = read_limited(response, max_bytes=self._max_bytes, deadline=self._deadline,
                                started=started, clock=self._clock)
        except CoordinatorError as exc:
            raise ReplyRefused(str(exc), request=request) from None
        finally:
            response.close()
        headers = [(k, v) for k, v in response.headers.multi_items()
                   if k.lower() not in ("content-encoding", "transfer-encoding")]
        return GuardedResponse(response.status_code, headers=headers, content=body,
                               request=request)
