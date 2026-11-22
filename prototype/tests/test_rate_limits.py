"""Who may queue jobs, per-client request limits, and the lock-out after failed logins.

Plain TestClient with every header set by hand, so nothing is hidden by WorkerClient.
The TestClient always connects as the address "testclient".
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import observability, workers
from app.config import Settings, get_settings
from app.main import create_app
from app.rate_limit import RateLimiter
from tests.auth_helpers import OP, OPERATOR, receipt

T0 = 1_800_000_000
SUBMITTER = "submitter-token-that-is-long-enough-789"
SUB = {"X-Submitter-Token": SUBMITTER}


def sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


class Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(tmp_path: Path, operator_token: str | None = OPERATOR,
         submitter_token: str | None = SUBMITTER, per_minute: int = 240,
         failure_limit: int = 10, window: int = 600):
    limiter_clock = Clock()
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, audit_rate=0.0, audit_rate_new=0.0,
                        audit_rate_suspect=0.0, operator_token=operator_token,
                        submitter_token=submitter_token, rate_limit_per_minute=per_minute,
                        auth_failure_limit=failure_limit, auth_failure_window_seconds=window)
    app = create_app(settings, clock=lambda: T0, limiter_clock=limiter_clock)
    return TestClient(app), limiter_clock


def join(client: TestClient, worker_id: str) -> dict[str, str]:
    """Enroll a worker straight from the database (like the CLI) and return its auth header."""
    code, _ = workers.issue_code(client.app.state.ledger, f"owner-{worker_id}", 3600, T0)
    r = client.post("/v1/workers/register", json={"worker_id": worker_id,
                                                  "enrollment_code": code})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['worker_key']}"}


def lines(caplog: pytest.LogCaptureFixture) -> list[dict]:
    fmt = observability.JsonFormatter()
    return [json.loads(fmt.format(r)) for r in caplog.records if r.name.startswith("bnh")]


# -- who may queue jobs ---------------------------------------------------------------------

def test_queuing_a_job_needs_the_submitter_or_operator_token(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    body = {"payload": "hello boats"}
    assert client.post("/v1/jobs", json=body).status_code == 403
    assert client.post("/v1/jobs", json=body,
                       headers={"X-Submitter-Token": "nope"}).status_code == 403
    assert client.post("/v1/jobs", json=body,
                       headers={"X-Operator-Token": "nope"}).status_code == 403
    assert client.post("/v1/jobs", json=body, headers=SUB).status_code == 200
    assert client.post("/v1/jobs", json=body, headers=OP).status_code == 200
    # A wrong token in one header doesn't spoil a right one in the other.
    assert client.post("/v1/jobs", json=body,
                       headers=SUB | {"X-Operator-Token": "nope"}).status_code == 200


def test_job_creation_is_disabled_with_no_tokens_set(tmp_path: Path) -> None:
    client, _ = make(tmp_path, operator_token=None, submitter_token=None)
    r = client.post("/v1/jobs", json={"payload": "x"}, headers=SUB)
    assert r.status_code == 403 and "disabled" in r.json()["detail"]


def test_submitter_token_only_queues_jobs(tmp_path: Path) -> None:
    """Least privilege: a program that queues work can't see hidden tests, answers,
    audits, reports or enroll rigs."""
    client, _ = make(tmp_path)
    for method, path, body in (("GET", "/v1/audits", None),
                               ("GET", "/v1/operator/health", None),
                               ("GET", "/v1/operator/review", None),
                               ("GET", "/v1/rewards/report", None),
                               ("POST", "/v1/enrollments", {"owner": "x"}),
                               ("POST", "/v1/challenges", {"payload": "x",
                                                           "expected_result": sha("x")})):
        r = client.request(method, path, json=body, headers={"X-Operator-Token": SUBMITTER})
        assert r.status_code == 403, path


def test_a_worker_cannot_mint_points_by_queuing_its_own_job(tmp_path: Path) -> None:
    """The hole this closes: with open job creation, any rig could queue a 1000-point job
    on an empty queue, claim it and be paid for it."""
    client, _ = make(tmp_path)
    auth = join(client, "wk-a")
    r = client.post("/v1/jobs", json={"payload": "free points", "reward_points": 1000},
                    headers=auth)
    assert r.status_code == 403
    assert client.post("/v1/jobs/next", params={"worker_id": "wk-a"},
                       headers=auth).status_code == 404  # nothing was queued
    assert client.get("/v1/ledger").json()["total_simulated_points"] == 0


def test_job_from_the_submitter_runs_like_any_other(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    auth = join(client, "wk-a")
    job = client.post("/v1/jobs", json={"payload": "hello"}, headers=SUB).json()
    assert client.post("/v1/jobs/next", params={"worker_id": "wk-a"},
                       headers=auth).status_code == 200
    r = client.post(f"/v1/jobs/{job['job_id']}/submit", headers=auth, json={
        "worker_id": "wk-a", "result": sha("hello"), "result_hash": sha(sha("hello")),
        "started_at": T0, "completed_at": T0, **receipt(client, job["job_id"])})
    assert r.status_code == 200 and r.json()["status"] == "accepted"


# -- request rate -----------------------------------------------------------------------------

def test_a_client_over_the_rate_gets_429_with_retry_after(tmp_path: Path) -> None:
    client, clock = make(tmp_path, per_minute=6)
    for _ in range(6):
        assert client.get("/v1/coordinator/key").status_code == 200
    r = client.get("/v1/coordinator/key")
    assert r.status_code == 429 and r.headers["Retry-After"] == "10"  # 6/min = one per 10 s
    assert "slow down" in r.json()["detail"]
    assert r.headers.get("X-Request-ID")  # still logged and traceable
    clock.t += 10
    assert client.get("/v1/coordinator/key").status_code == 200
    assert client.get("/v1/coordinator/key").status_code == 429
    clock.t += 3600  # a long pause refills the bucket, but only up to its size
    assert [client.get("/v1/coordinator/key").status_code for _ in range(7)] == [200] * 6 + [429]


def test_health_is_never_limited(tmp_path: Path) -> None:
    client, _ = make(tmp_path, per_minute=1, failure_limit=1)
    client.post("/v1/jobs/next", headers={"Authorization": "Bearer bnhw_wrong"})  # locked out
    assert client.get("/v1/coordinator/key").status_code == 429
    assert all(client.get("/health").status_code == 200 for _ in range(20))


def test_zero_turns_the_rate_limit_off(tmp_path: Path) -> None:
    client, _ = make(tmp_path, per_minute=0)
    assert all(client.get("/v1/coordinator/key").status_code == 200 for _ in range(300))


def test_each_address_has_its_own_bucket() -> None:
    clock = Clock()
    limiter = RateLimiter(2, 0, 600, clock)
    assert limiter.check("10.0.0.1", "/v1/jobs/next") is None
    assert limiter.check("10.0.0.1", "/v1/jobs/next") is None
    assert limiter.check("10.0.0.1", "/v1/jobs/next") == ("rate_limited", 30)
    assert limiter.check("10.0.0.2", "/v1/jobs/next") is None
    assert limiter.snapshot()["since_start"]["rate_limited_requests"] == 1


# -- lock-out after failed logins ------------------------------------------------------------

def test_wrong_worker_keys_lock_the_address_out_then_it_expires(tmp_path: Path) -> None:
    client, clock = make(tmp_path, failure_limit=3, window=600)
    auth = join(client, "wk-a")
    for _ in range(3):
        clock.t += 1
        assert client.post("/v1/jobs/next",
                           headers={"Authorization": "Bearer bnhw_guess"}).status_code == 401
    # Locked out everywhere, even with a right key (same address), until the window passes.
    r = client.post("/v1/jobs/next", params={"worker_id": "wk-a"}, headers=auth)
    assert r.status_code == 429 and "failed logins" in r.json()["detail"]
    assert r.headers["Retry-After"] == "598"  # first failure at t=1, window 600, now t=3
    clock.t = 600
    assert client.post("/v1/jobs/next", params={"worker_id": "wk-a"},
                       headers=auth).status_code == 429
    clock.t = 601  # the first failure left the window: below the limit again
    assert client.post("/v1/jobs/next", params={"worker_id": "wk-a"},
                       headers=auth).status_code == 404  # let in; just no jobs


def test_every_kind_of_wrong_credential_counts(tmp_path: Path) -> None:
    client, _ = make(tmp_path, failure_limit=4)
    join(client, "wk-a")
    other = join(client, "wk-b")
    client.get("/v1/audits", headers={"X-Operator-Token": "wrong-operator-token"})
    client.post("/v1/jobs", json={"payload": "x"}, headers={"X-Submitter-Token": "wrong"})
    client.post("/v1/workers/register", json={"worker_id": "wk-c",
                                              "enrollment_code": "bnhe_" + "g" * 43})
    # wk-b's real key used to claim as wk-a: a valid key for the wrong worker.
    client.post("/v1/jobs/next", params={"worker_id": "wk-a"}, headers=other)
    assert client.get("/v1/coordinator/key").status_code == 429


def test_revoked_key_counts_too(tmp_path: Path) -> None:
    client, _ = make(tmp_path, failure_limit=2)
    auth = join(client, "wk-a")
    assert client.post("/v1/workers/wk-a/revoke", headers=OP).status_code == 200
    for _ in range(2):
        assert client.post("/v1/jobs/next", headers=auth).status_code == 403
    assert client.get("/v1/coordinator/key").status_code == 429


def test_missing_credentials_do_not_count(tmp_path: Path) -> None:
    """No key, no token or no code is a setup mistake, not a guess."""
    client, _ = make(tmp_path, failure_limit=2)
    for _ in range(5):
        assert client.post("/v1/jobs/next").status_code == 401
        assert client.get("/v1/audits").status_code == 403
        assert client.post("/v1/jobs", json={"payload": "x"}).status_code == 403
        assert client.post("/v1/workers/register", json={"worker_id": "wk-z"}).status_code == 401
    assert client.get("/v1/coordinator/key").status_code == 200
    assert client.app.state.limiter.snapshot()["since_start"]["auth_failures"] == 0


def test_zero_turns_the_lock_out_off(tmp_path: Path) -> None:
    client, _ = make(tmp_path, failure_limit=0)
    for _ in range(50):
        assert client.post("/v1/jobs/next",
                           headers={"Authorization": "Bearer bnhw_guess"}).status_code == 401
    assert client.app.state.limiter.snapshot()["since_start"]["auth_failures"] == 50


def test_operator_health_shows_lock_outs_and_logs_never_show_the_address(
        tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="bnh")
    client, _ = make(tmp_path, failure_limit=2, per_minute=1000)
    for _ in range(3):
        client.post("/v1/jobs/next", headers={"Authorization": "Bearer bnhw_guess"})
    # The operator, from another address, sees who is locked out and why.
    limiter = client.app.state.limiter
    assert limiter.check("10.9.9.9", "/v1/operator/health") is None
    health = client.app.state.limiter.snapshot()
    assert health["since_start"] == {"rate_limited_requests": 0, "locked_out_requests": 1,
                                     "auth_failures": 2}
    [locked] = health["locked_out_clients"]
    assert locked["client"] == "testclient" and locked["failures_in_window"] == 2
    tag = locked["client_tag"]
    # Logs: a tag groups the client's lines, its address never appears.
    log_lines = lines(caplog)
    assert any(l["msg"] == "client locked out" and l["client_tag"] == tag for l in log_lines)
    assert any(l["msg"] == "request limited" and l["reason"] == "locked_out"
               and l["client_tag"] == tag for l in log_lines)
    assert "testclient" not in json.dumps(log_lines)
    assert "bnhw_guess" not in json.dumps(log_lines)


def test_operator_health_endpoint_warns_about_locked_out_clients(tmp_path: Path) -> None:
    client, clock = make(tmp_path, failure_limit=2, window=60)
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["rate_limits"]["locked_out_clients"] == []
    assert not any("locked out" in w for w in health["warnings"])
    # Lock out a different address than the operator's by talking to the limiter directly.
    limiter = client.app.state.limiter
    limiter.record_auth_failure("203.0.113.7")
    limiter.record_auth_failure("203.0.113.7")
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["status"] == "attention"
    assert [c["client"] for c in health["rate_limits"]["locked_out_clients"]] == ["203.0.113.7"]
    assert any("1 client address(es) locked out" in w for w in health["warnings"])
    clock.t += 61
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["rate_limits"]["locked_out_clients"] == []


# -- settings --------------------------------------------------------------------------------

def test_settings_check_the_new_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR)
    monkeypatch.setenv("SUBMITTER_TOKEN", SUBMITTER)
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    s = get_settings()
    assert s.submitter_token == SUBMITTER and s.rate_limit_per_minute == 0
    assert s.auth_failure_limit == 10 and s.auth_failure_window_seconds == 600
    monkeypatch.setenv("SUBMITTER_TOKEN", "short")
    with pytest.raises(ValueError, match="at least 24"):
        get_settings()
    monkeypatch.setenv("SUBMITTER_TOKEN", OPERATOR)
    with pytest.raises(ValueError, match="different from OPERATOR_TOKEN"):
        get_settings()
    monkeypatch.setenv("SUBMITTER_TOKEN", SUBMITTER)
    monkeypatch.setenv("AUTH_FAILURE_LIMIT", "-1")
    with pytest.raises(ValueError, match="AUTH_FAILURE_LIMIT"):
        get_settings()


def test_submitter_header_is_refused_cleanly_when_no_submitter_token_is_set(
        tmp_path: Path) -> None:
    client, _ = make(tmp_path, submitter_token=None)
    r = client.post("/v1/jobs", json={"payload": "x"}, headers={"X-Submitter-Token": OPERATOR})
    assert r.status_code == 403  # an operator token in the wrong header isn't accepted
    assert client.post("/v1/jobs", json={"payload": "x"}, headers=OP).status_code == 200


def test_old_failures_are_forgotten_after_the_window() -> None:
    """Once the window has passed, one new mistake mustn't lock the address out again."""
    clock = Clock()
    limiter = RateLimiter(0, 3, 100, clock)
    for _ in range(3):
        limiter.record_auth_failure("10.0.0.1")
    assert limiter.check("10.0.0.1", "/v1/jobs/next") == ("locked_out", 100)
    clock.t = 101
    assert limiter.check("10.0.0.1", "/v1/jobs/next") is None
    limiter.record_auth_failure("10.0.0.1")
    assert limiter.check("10.0.0.1", "/v1/jobs/next") is None
    assert limiter.snapshot()["locked_out_clients"] == []
    limiter.record_auth_failure("10.0.0.1")
    limiter.record_auth_failure("10.0.0.1")
    [locked] = limiter.snapshot()["locked_out_clients"]
    assert locked["failures_in_window"] == 3  # the three from before the window are gone


def test_demo_worker_waits_when_told_to_slow_down(tmp_path: Path) -> None:
    from demo_worker import post_with_backoff, retry_after_seconds

    client, clock = make(tmp_path, per_minute=2)
    auth = join(client, "wk-a")  # registering used one request from the bucket
    waits: list[int] = []

    def sleep(seconds: int) -> None:  # instead of really sleeping, move the limiter's clock
        waits.append(seconds)
        clock.t += seconds

    first = post_with_backoff(client, "/v1/jobs/next", sleep=sleep,
                              params={"worker_id": "wk-a"}, headers=auth)
    assert first.status_code == 404 and waits == []
    again = post_with_backoff(client, "/v1/jobs/next", sleep=sleep,
                              params={"worker_id": "wk-a"}, headers=auth)
    assert again.status_code == 404 and waits == [30]  # waited, then got through
    # It gives up after a few tries instead of looping forever, and never waits over 60 s.
    stuck = post_with_backoff(client, "/v1/jobs/next", tries=3, sleep=lambda s: waits.append(s),
                              params={"worker_id": "wk-a"}, headers=auth)
    assert stuck.status_code == 429 and waits[1:] == [30, 30]
    locked = type("R", (), {"headers": {"Retry-After": "598"}})()
    assert retry_after_seconds(locked) == 60  # type: ignore[arg-type]
    garbled = type("R", (), {"headers": {"Retry-After": "soon"}})()
    assert retry_after_seconds(garbled) == 5  # type: ignore[arg-type]
