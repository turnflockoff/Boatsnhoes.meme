"""`guess-slot-v1`: wrong tokens sent at the same moment can't all be checked.

The lock-out (rate_limit.py) counts a failure when a token check *finishes*. Before 0.5.62 a
client could send many wrong operator or submitter tokens at once; every one passed the
"is this client locked out?" check before the first failure was counted, so 230 wrong tokens
sent together were all checked against a limit of 10. Now each request carrying such a token
takes a "guess slot" first.

The burst tests start a real uvicorn server on 127.0.0.1 and send the requests over real
connections at the same time, because the race only shows up with real concurrency.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import workers
from app.config import Settings
from app.main import create_app
from app.rate_limit import RateLimiter

OPERATOR = "the-real-operator-token-0123456789"
SUBMITTER = "the-real-submitter-token-0123456789"
LIMIT = 10


class Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def settings(tmp_path: Path, limit: int = LIMIT) -> Settings:
    return Settings(database_path=tmp_path / "db.sqlite3", operator_token=OPERATOR,
                    submitter_token=SUBMITTER, auth_failure_limit=limit,
                    auth_failure_window_seconds=600, rate_limit_per_minute=10_000,
                    audit_rate=0.0, audit_rate_new=0.0, audit_rate_suspect=0.0)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, object]]:
    app = create_app(settings(tmp_path))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        srv.should_exit = True
        thread.join(timeout=10)


def burst(base: str, requests: list[tuple[str, str, dict[str, str], dict | None]]) -> Counter:
    """Send every request at the same moment; return a count of status codes."""
    async def go() -> list[httpx.Response]:
        limits = httpx.Limits(max_connections=len(requests))
        async with httpx.AsyncClient(base_url=base, limits=limits, timeout=30) as c:
            return await asyncio.gather(*[c.request(m, p, headers=h, json=j)
                                          for m, p, h, j in requests])
    return Counter(r.status_code for r in asyncio.run(go()))


def wrong(i: int) -> str:
    return f"wrong-guess-{i:04d}-padding-padding"


# -- real concurrency -------------------------------------------------------------------------

def test_a_burst_of_wrong_operator_tokens_is_checked_at_most_limit_times(server) -> None:
    base, _ = server
    codes = burst(base, [("GET", "/v1/audits", {"X-Operator-Token": wrong(i)}, None)
                         for i in range(80)])
    # Before guess-slot-v1 this was {403: 80}: every guess was checked.
    assert codes[403] == LIMIT
    assert codes[429] == 80 - LIMIT
    assert sum(codes.values()) == 80


def test_a_burst_of_wrong_submitter_tokens_is_checked_at_most_limit_times(server) -> None:
    base, _ = server
    codes = burst(base, [("POST", "/v1/jobs", {"X-Submitter-Token": wrong(i)},
                          {"payload": "hello boats"}) for i in range(60)])
    assert codes[403] == LIMIT
    assert codes[429] == 60 - LIMIT


def test_after_the_burst_the_client_is_locked_out_even_with_the_real_token(server) -> None:
    base, _ = server
    burst(base, [("GET", "/v1/audits", {"X-Operator-Token": wrong(i)}, None)
                 for i in range(30)])
    r = httpx.get(f"{base}/v1/audits", headers={"X-Operator-Token": OPERATOR})
    assert r.status_code == 429
    assert "failed logins" in r.json()["detail"]
    assert int(r.headers["Retry-After"]) > 0


def test_a_burst_of_right_operator_tokens_below_the_limit_all_succeed(server) -> None:
    base, _ = server
    codes = burst(base, [("GET", "/v1/audits", {"X-Operator-Token": OPERATOR}, None)
                         for _ in range(LIMIT)])
    assert codes == Counter({200: LIMIT})


def test_rigs_polling_at_the_same_moment_never_take_guess_slots(server) -> None:
    """Worker keys are 256-bit random values; rigs behind one router may all poll at once."""
    base, app = server
    headers = []
    for i in range(3):
        code, _ = workers.issue_code(app.state.ledger, f"owner-{i}", 3600, int(time.time()))
        r = httpx.post(f"{base}/v1/workers/register",
                       json={"worker_id": f"wk-{i}", "enrollment_code": code})
        assert r.status_code == 200, r.text
        headers.append({"Authorization": f"Bearer {r.json()['worker_key']}"})
    codes = burst(base, [("POST", "/v1/jobs/next?worker_id=wk-" + str(i % 3), headers[i % 3],
                          None) for i in range(4 * LIMIT)])
    assert codes == Counter({404: 4 * LIMIT})  # "no queued jobs", never 429


def test_slots_are_given_back_so_the_operator_is_never_slowed_down(server) -> None:
    base, _ = server
    with httpx.Client(base_url=base) as c:
        for _ in range(5 * LIMIT):
            assert c.get("/v1/audits", headers={"X-Operator-Token": OPERATOR}).status_code == 200
    r = httpx.get(f"{base}/v1/operator/health", headers={"X-Operator-Token": OPERATOR})
    assert r.status_code == 200
    assert r.json()["rate_limits"]["since_start"]["guesses_refused_at_once"] == 0


# -- in-process checks ----------------------------------------------------------------------

def test_health_ignores_a_token_header_even_while_locked_out(tmp_path: Path) -> None:
    client = TestClient(create_app(settings(tmp_path, limit=2)))
    for i in range(2):
        assert client.get("/v1/audits", headers={"X-Operator-Token": wrong(i)}).status_code == 403
    assert client.get("/v1/audits", headers={"X-Operator-Token": OPERATOR}).status_code == 429
    assert client.get("/health", headers={"X-Operator-Token": wrong(9)}).status_code == 200


def test_an_empty_token_header_takes_a_slot_but_is_not_counted_as_a_failure(
        tmp_path: Path) -> None:
    client = TestClient(create_app(settings(tmp_path, limit=2)))
    for _ in range(5):
        assert client.get("/v1/audits", headers={"X-Operator-Token": ""}).status_code == 403
    assert client.get("/v1/audits", headers={"X-Operator-Token": OPERATOR}).status_code == 200
    assert client.app.state.limiter.snapshot()["since_start"]["auth_failures"] == 0


def test_a_slot_is_given_back_when_the_endpoint_crashes(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path, limit=2))

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("crash")

    client = TestClient(app, raise_server_exceptions=False)
    for _ in range(5):
        assert client.get("/boom", headers={"X-Operator-Token": OPERATOR}).status_code == 500
    assert client.get("/v1/audits", headers={"X-Operator-Token": OPERATOR}).status_code == 200
    assert all(c.guessing == 0 for c in app.state.limiter._clients.values())


def test_the_too_many_at_once_reply_never_reveals_the_token_was_checked(
        tmp_path: Path) -> None:
    app = create_app(settings(tmp_path, limit=1))
    limiter = app.state.limiter
    assert limiter.start_guess("testclient") is None  # another request holds the only slot
    client = TestClient(app)
    r = client.get("/v1/audits", headers={"X-Operator-Token": OPERATOR})
    assert r.status_code == 429
    assert r.json()["detail"] == ("too many login attempts at once from this address; "
                                  "try again in a moment")
    assert r.headers["Retry-After"] == "1"
    limiter.finish_guess("testclient")
    assert client.get("/v1/audits", headers={"X-Operator-Token": OPERATOR}).status_code == 200


# -- the limiter on its own -----------------------------------------------------------------

def test_slots_plus_failures_never_go_above_the_limit() -> None:
    clock = Clock(100.0)
    lim = RateLimiter(per_minute=0, auth_failure_limit=3, auth_failure_window=60, clock=clock)
    assert lim.start_guess("a") is None
    assert lim.start_guess("a") is None
    lim.record_auth_failure("a")
    lim.finish_guess("a")          # 1 failure + 1 slot in use
    assert lim.start_guess("a") is None  # 1 + 2
    reason, retry = lim.start_guess("a")
    assert (reason, retry) == ("too_many_at_once", 1)  # not locked yet, so "in a moment"
    lim.finish_guess("a")
    lim.finish_guess("a")
    assert lim.start_guess("a") is None
    assert lim.start_guess("b") is None  # other clients have their own slots
    assert lim.snapshot()["since_start"]["guesses_refused_at_once"] == 1


def test_a_locked_out_client_gets_the_lock_out_wait_not_one_second() -> None:
    clock = Clock(100.0)
    lim = RateLimiter(per_minute=0, auth_failure_limit=2, auth_failure_window=60, clock=clock)
    lim.record_auth_failure("a")
    lim.record_auth_failure("a")
    assert lim.start_guess("a") == ("too_many_at_once", 60)
    clock.t += 61
    assert lim.start_guess("a") is None


def test_slots_are_off_when_the_lock_out_is_off() -> None:
    lim = RateLimiter(per_minute=0, auth_failure_limit=0, auth_failure_window=60,
                      clock=Clock())
    for _ in range(100):
        assert lim.start_guess("a") is None
    lim.finish_guess("a")  # harmless
    assert lim._clients == {}


def test_finishing_more_than_started_never_goes_negative() -> None:
    lim = RateLimiter(per_minute=0, auth_failure_limit=2, auth_failure_window=60,
                      clock=Clock())
    lim.finish_guess("never-seen")
    assert lim.start_guess("a") is None
    lim.finish_guess("a")
    lim.finish_guess("a")
    assert lim._clients["a"].guessing == 0
    assert lim.start_guess("a") is None
    assert lim.start_guess("a") is None
    assert lim.start_guess("a") is not None


def test_a_client_holding_a_slot_is_never_pruned() -> None:
    clock = Clock(0.0)
    lim = RateLimiter(per_minute=0, auth_failure_limit=2, auth_failure_window=60, clock=clock)
    assert lim.start_guess("holder") is None
    clock.t = 10_000.0
    for n in range(10):  # each new client sweeps the oldest entries (`client-prefix-v1`)
        lim.check(f"other-{n}", "/v1/jobs/next")
    assert lim._clients["holder"].guessing == 1


def test_slots_are_thread_safe() -> None:
    lim = RateLimiter(per_minute=0, auth_failure_limit=7, auth_failure_window=60,
                      clock=Clock())
    start = threading.Barrier(50)
    got: list[bool] = []
    lock = threading.Lock()

    def take() -> None:
        start.wait()
        ok = lim.start_guess("a") is None
        with lock:
            got.append(ok)

    threads = [threading.Thread(target=take) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got.count(True) == 7
