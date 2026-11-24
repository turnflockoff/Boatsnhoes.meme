"""Hostile-input checks for the coordinator API (`input-limits-v1`, code audit first pass).

Two parts:
  * targeted tests for each problem the audit found: unbounded request bodies (memory),
    deeply nested JSON (RecursionError -> 500), integers too big for SQLite (OverflowError
    -> 500), negative worker timestamps, unbounded capability lists, control characters
    in worker names;
  * a route-by-route fuzz: every API route, with valid credentials, gets a battery of
    wrong types, huge and negative numbers, odd strings and broken JSON. The rule is
    simple: a bad request may be refused (4xx), but it must never crash the coordinator
    (5xx) or leak a stack trace. A new route or body model without an example here makes
    the fuzz fail, so it stays complete.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import input_limits, workers
from app.config import Settings, get_settings
from app.main import create_app
from tests.auth_helpers import OP, OPERATOR, receipt

T0 = 1_800_000_000
SUBMITTER = "submitter-token-that-is-long-enough-789"


def make(tmp_path: Path, per_minute: int = 0, **extra: Any) -> TestClient:
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, audit_rate=0.0, audit_rate_new=0.0,
                        audit_rate_suspect=0.0, operator_token=OPERATOR,
                        submitter_token=SUBMITTER, rate_limit_per_minute=per_minute,
                        auth_failure_limit=0, **extra)
    return TestClient(create_app(settings, clock=lambda: T0), raise_server_exceptions=False)


def join(client: TestClient, worker_id: str = "wk-a", owner: str | None = None
         ) -> dict[str, str]:
    code, _ = workers.issue_code(client.app.state.ledger, owner or f"owner-{worker_id}",
                                 3600, T0)
    r = client.post("/v1/workers/register", json={"worker_id": worker_id,
                                                  "enrollment_code": code})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['worker_key']}"}


def job_count(client: TestClient) -> int:
    return sum(client.app.state.ledger.status_counts().values())


def padded(body: dict[str, Any], total: int) -> bytes:
    """Valid JSON of exactly `total` bytes: the body plus trailing spaces (still valid)."""
    raw = json.dumps(body).encode()
    return raw + b" " * (total - len(raw))


JSON = {"content-type": "application/json"}


# -- request body size -----------------------------------------------------------------------

def test_default_body_limit_is_256_kib() -> None:
    assert Settings(database_path=Path("x")).max_body_bytes == 256 * 1024


def test_a_body_over_the_limit_is_refused_with_413_before_anything_runs(tmp_path: Path) -> None:
    client = make(tmp_path, max_body_bytes=16 * 1024)
    over = padded({"payload": "hello boats"}, 16 * 1024 + 1)
    r = client.post("/v1/jobs", content=over, headers=JSON | OP)
    assert r.status_code == 413, r.text
    assert "too large" in r.json()["detail"]
    assert job_count(client) == 0  # the same JSON would have queued a job


def _run_asgi(middleware: Any, headers: list[tuple[bytes, bytes]]) -> tuple[int, list]:
    """Drive the middleware by hand with a client that must never be read from."""
    import asyncio
    sent: list[dict[str, Any]] = []
    reads: list[int] = []

    async def receive() -> dict[str, Any]:
        reads.append(1)
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/v1/jobs", "headers": headers,
             "query_string": b""}
    asyncio.run(middleware(scope, receive, send))
    return sent[0]["status"], reads


@pytest.mark.parametrize("length,status", [(b"10485760", 413), (b"12abc", 400),
                                           (b"-5", 400)])
def test_a_bad_or_too_big_content_length_is_refused_without_reading(length: bytes,
                                                                    status: int) -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise AssertionError("the app must not run")
    mw = input_limits.BodyLimit(app, max_bytes=16 * 1024)
    got, reads = _run_asgi(mw, [(b"content-length", length)])
    assert got == status
    assert reads == []  # not one byte of the 10 MB was read


def test_a_body_exactly_at_the_limit_is_accepted(tmp_path: Path) -> None:
    client = make(tmp_path, max_body_bytes=16 * 1024)
    r = client.post("/v1/jobs", content=padded({"payload": "hello boats"}, 16 * 1024),
                    headers=JSON | OP)
    assert r.status_code == 200, r.text
    assert job_count(client) == 1


def test_a_streamed_body_without_a_length_is_cut_off_at_the_limit(tmp_path: Path) -> None:
    client = make(tmp_path, max_body_bytes=16 * 1024)
    sent = {"chunks": 0}

    def chunks() -> Iterator[bytes]:
        yield b'{"payload": "hello boats"}'
        for _ in range(100):  # 100 x 1 KiB of spaces; far past the limit
            sent["chunks"] += 1
            yield b" " * 1024

    r = client.post("/v1/jobs", content=chunks(), headers=JSON | OP)
    assert r.status_code == 413, r.text
    assert job_count(client) == 0


def test_the_largest_honest_body_fits_under_the_default_limit(tmp_path: Path) -> None:
    """A hidden test at every field's maximum, written in the most bloated legal JSON
    (every character an escaped emoji, 12 bytes each), still fits."""
    client = make(tmp_path)
    join(client, "w" * 80, owner="owner-w")  # longest allowed worker name, as the target
    emoji = "\U0001F6A4"  # speedboat
    body = {"task_type": "demo:sha256", "payload": emoji * 4096,
            "expected_result": emoji * 8192, "reward_points": 1000, "ttl_seconds": 3600,
            "target_worker": "w" * 80}
    raw = json.dumps(body, ensure_ascii=True).encode()
    assert len(raw) > 140_000  # really is big
    r = client.post("/v1/challenges", content=raw, headers=JSON | OP)
    # It gets past the size and nesting limits to the task's own answer check (a demo
    # answer is a 64-character hash, so 8192 emoji is the wrong answer, not too big).
    assert r.status_code == 422, r.text
    assert "verifier" in r.json()["detail"]
    # The same payload with its real answer is accepted.
    import hashlib
    body["expected_result"] = hashlib.sha256(body["payload"].encode()).hexdigest()
    r = client.post("/v1/challenges", content=json.dumps(body, ensure_ascii=True).encode(),
                    headers=JSON | OP)
    assert r.status_code == 200, r.text


def test_oversized_requests_still_count_against_the_request_limit(tmp_path: Path) -> None:
    client = make(tmp_path, per_minute=2, max_body_bytes=16 * 1024)
    over = padded({"payload": "x"}, 20 * 1024)
    assert client.post("/v1/jobs", content=over, headers=JSON | OP).status_code == 413
    assert client.post("/v1/jobs", content=over, headers=JSON | OP).status_code == 413
    assert client.post("/v1/jobs", content=over, headers=JSON | OP).status_code == 429


def test_oversized_request_is_logged_with_413_and_no_body(tmp_path: Path,
                                                          caplog: pytest.LogCaptureFixture
                                                          ) -> None:
    client = make(tmp_path, max_body_bytes=16 * 1024)
    caplog.set_level(logging.INFO, logger="bnh")
    secret = "do-not-log-this-body-text"
    client.post("/v1/jobs", content=padded({"payload": secret}, 17 * 1024), headers=JSON | OP)
    statuses = [getattr(r, "status", None) for r in caplog.records if r.name.startswith("bnh")]
    assert 413 in statuses
    assert secret not in caplog.text


def test_max_body_bytes_comes_from_the_environment_within_bounds(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("MAX_BODY_BYTES", "65536")
    assert get_settings().max_body_bytes == 65536
    for bad in ("100", str(64 * 1024 * 1024)):  # below 16 KiB / above 4 MiB
        monkeypatch.setenv("MAX_BODY_BYTES", bad)
        with pytest.raises(ValueError):
            get_settings()


# -- JSON nesting ----------------------------------------------------------------------------

def test_deeply_nested_json_is_refused_with_400_not_a_crash(tmp_path: Path) -> None:
    client = make(tmp_path)
    deep = b"[" * 5000 + b"]" * 5000
    r = client.post("/v1/workers/register", content=deep, headers=JSON)  # no login needed
    assert r.status_code == 400, r.text
    assert "nested" in r.json()["detail"]


def test_nesting_inside_strings_does_not_count(tmp_path: Path) -> None:
    client = make(tmp_path)
    r = client.post("/v1/jobs", json={"payload": "[" * 1000 + '{"\\"' * 500}, headers=OP)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("raw,depth", [
    (b'{"a": [1, {"b": [2]}]}', 4),
    (b'"[[[[[["', 0),
    (b'["\\"[[[[", [[]]]', 3),
    (b"", 0),
    (b"[[[", 3),  # unbalanced; JSON parsing refuses it later, depth is still 3
])
def test_json_depth_counts_brackets_outside_strings(raw: bytes, depth: int) -> None:
    assert input_limits.json_depth(raw) == depth


# -- validation errors -----------------------------------------------------------------------

@pytest.mark.parametrize("raw", [b"1e999", b'{"reason": 1e999}', b"-Infinity", b"NaN"])
def test_infinite_numbers_get_a_422_not_a_crash(tmp_path: Path, raw: bytes) -> None:
    client = make(tmp_path)
    r = client.post("/v1/operator/pause", content=raw, headers=JSON | OP)
    assert r.status_code == 422, r.text


def test_validation_errors_do_not_echo_what_was_sent(tmp_path: Path) -> None:
    """A refused body is never sent back: not a secret typed into the wrong field, and
    not 4 KiB+ of junk per error."""
    client = make(tmp_path)
    secret = "enrollment-code-typed-into-the-wrong-box"
    r = client.post("/v1/workers/register", json={"worker_id": "x", "enrollment_code": 5,
                                                  "capabilities": secret})
    assert r.status_code == 422
    assert secret not in r.text
    errors = r.json()["detail"]
    assert {tuple(e["loc"]) for e in errors} >= {("body", "worker_id"),
                                                  ("body", "capabilities")}
    assert all(set(e) == {"type", "loc", "msg"} for e in errors)
    big = client.post("/v1/jobs", json={"payload": "a" * 100_000}, headers=OP)
    assert big.status_code == 422 and len(big.text) < 1000


# -- numbers and names ------------------------------------------------------------------------

def test_reward_report_with_a_huge_until_is_refused_not_a_crash(tmp_path: Path) -> None:
    client = make(tmp_path)
    r = client.get("/v1/rewards/report", params={"until": 2 ** 70}, headers=OP)
    assert r.status_code == 422, r.text


def test_negative_worker_timestamps_are_refused(tmp_path: Path) -> None:
    client = make(tmp_path)
    auth = join(client)
    job = client.post("/v1/jobs", json={"payload": "hi"}, headers=OP).json()
    client.post("/v1/jobs/next", headers=auth)
    import hashlib
    result = hashlib.sha256(b"hi").hexdigest()
    body = {"result": result, "result_hash": hashlib.sha256(result.encode()).hexdigest(),
            "started_at": -(2 ** 70), "completed_at": T0} | receipt(client, job["job_id"])
    r = client.post(f"/v1/jobs/{job['job_id']}/submit", json=body, headers=auth)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("caps", [
    ["demo:sha256"] * 17,           # too many
    ["x" * 65],                     # too long
    ["demo:sha256\n"],              # control character
    [""],                           # empty
    ["DEMO SHA"],                   # space / capitals
])
def test_capability_lists_are_bounded(tmp_path: Path, caps: list[str]) -> None:
    client = make(tmp_path)
    code, _ = workers.issue_code(client.app.state.ledger, "owner-a", 3600, T0)
    r = client.post("/v1/workers/register", json={"worker_id": "wk-a", "capabilities": caps,
                                                  "enrollment_code": code})
    assert r.status_code == 422, r.text


def test_normal_capability_lists_still_work(tmp_path: Path) -> None:
    client = make(tmp_path)
    code, _ = workers.issue_code(client.app.state.ledger, "owner-a", 3600, T0)
    r = client.post("/v1/workers/register", json={
        "worker_id": "wk-a.rig_1:card0", "capabilities": ["demo:sha256", "llm:generate"],
        "enrollment_code": code})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("name", ["wk-a\n", "wk\x00a", "wk a", "wk-‮b", "<script>"])
def test_worker_names_are_plain(tmp_path: Path, name: str) -> None:
    client = make(tmp_path)
    code, _ = workers.issue_code(client.app.state.ledger, "owner-a", 3600, T0)
    r = client.post("/v1/workers/register", json={"worker_id": name,
                                                  "enrollment_code": code})
    assert r.status_code == 422, r.text


# -- route-by-route fuzz -----------------------------------------------------------------------

# One valid example body per request model. Each fuzz case replaces one field.
EXAMPLES: dict[str, dict[str, Any]] = {
    "WorkerRegistration": {"worker_id": "wk-fuzz", "capabilities": ["demo:sha256"],
                           "enrollment_code": "c" * 20},
    "EnrollmentCreate": {"owner": "owner-fuzz", "ttl_seconds": 3600},
    "PauseRequest": {"reason": "fuzz"},
    "JobCreate": {"task_type": "demo:sha256", "payload": "fuzz", "ttl_seconds": 60},
    "ChallengeCreate": {"task_type": "demo:sha256", "payload": "fuzz",
                        "expected_result": "x", "ttl_seconds": 60},
    "ResultSubmission": {"result": "x", "result_hash": "0" * 64, "started_at": T0,
                         "completed_at": T0, "receipt_version": "bnh-receipt/1",
                         "job_schema_version": "bnh-job/1", "payload_hash": "0" * 64},
}

HOSTILE_VALUES: list[Any] = [None, True, -1, 0, 2 ** 63, -(2 ** 70), 2 ** 70, 1.5, 1e308,
                             "", "\x00", "a\nb", "‮", "x" * 10_000, "' OR 1=1 --",
                             [], [1, "a", None], {}, {"$gt": ""}]

HOSTILE_BODIES: list[bytes] = [b"", b"null", b"[]", b'"x"', b"1e999", b"NaN", b'{"a":',
                               b"\xff\xfe\x00", b"{" * 3000, b"[" * 3000 + b"]" * 3000,
                               b'{"a":' * 200 + b"1" + b"}" * 200]

HOSTILE_QUERY: list[Any] = [-1, 2 ** 70, -(2 ** 70), "x", "", "1e9", "\x00", "nan"]

HOSTILE_PATH: list[str] = ["x", "%00", "..%2f..%2fetc", "a" * 5000, "%ff%fe", "1;DROP"]


def fuzz_targets(client: TestClient) -> list[APIRoute]:
    """Every API route, the emergency pause last: once a fuzz case pauses the coordinator,
    every later request gets the deliberate "paused" 503 instead of reaching its code."""
    routes = [r for r in client.app.routes if isinstance(r, APIRoute)]
    return sorted(routes, key=lambda r: r.path == "/v1/operator/pause")


def no_crash(r: Any, what: str) -> None:
    paused = r.status_code == 503 and "paused by its operator" in r.text
    assert r.status_code < 500 or paused, f"{what} -> {r.status_code}: {r.text[:300]}"
    assert "Traceback" not in r.text, what


@pytest.fixture()
def fuzz_client(tmp_path: Path) -> tuple[TestClient, dict[str, str], str]:
    client = make(tmp_path)
    auth = join(client, "wk-a")
    job = client.post("/v1/jobs", json={"payload": "hi"}, headers=OP).json()
    return client, auth, job["job_id"]


def test_every_body_model_has_a_fuzz_example(fuzz_client: Any) -> None:
    client, _, _ = fuzz_client
    models = {_model(r) for r in fuzz_targets(client) if r.body_field}
    assert models <= EXAMPLES.keys(), f"add an example for {models - EXAMPLES.keys()}"


def test_fuzz_every_route_never_crashes(fuzz_client: Any) -> None:
    client, auth, job_id = fuzz_client
    headers = OP | auth | {"X-Submitter-Token": SUBMITTER}
    tried = 0
    for route in fuzz_targets(client):
        path = route.path.replace("{job_id}", job_id).replace("{worker_id}", "wk-a")
        for method in sorted(route.methods - {"HEAD"}):
            def send(url: str = path, **kw: Any) -> Any:
                nonlocal tried
                tried += 1
                return client.request(method, url, headers=headers | kw.pop("headers", {}),
                                      **kw)

            # odd path segments
            for seg in HOSTILE_PATH:
                for name in ("{job_id}", "{worker_id}"):
                    if name in route.path:
                        url = route.path.replace(name, seg)
                        url = url.replace("{job_id}", job_id).replace("{worker_id}", "wk-a")
                        no_crash(send(url, json=EXAMPLES.get(_model(route), {})),
                                 f"{method} {url[:60]}")
            # odd query values for every declared query parameter
            for param in route.dependant.query_params:
                for value in HOSTILE_QUERY:
                    no_crash(send(params={param.alias: value}),
                             f"{method} {route.path}?{param.alias}={str(value)[:20]}")
            # broken bodies
            for raw in HOSTILE_BODIES:
                no_crash(send(content=raw, headers=JSON), f"{method} {route.path} {raw[:20]!r}")
            # one hostile value per field of a valid example
            model = _model(route)
            if model:
                for field in EXAMPLES[model]:
                    for value in HOSTILE_VALUES:
                        body = EXAMPLES[model] | {field: value}
                        no_crash(send(json=body),
                                 f"{method} {route.path} {field}={str(value)[:20]}")
    assert tried > 500  # the fuzz really ran


def _model(route: APIRoute) -> str | None:
    return route.body_field.field_info.annotation.__name__ if route.body_field else None
