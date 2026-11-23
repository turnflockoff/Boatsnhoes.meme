"""Structured logs and health reporting (Track D).

Logs are one JSON object per line so a machine can search them, and they must never carry
secrets (worker keys, operator token, enrollment codes) or job content (prompts, answers,
expected answers of hidden tests), so a log can be pasted into a bug report.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from app import model_registry, observability
from app.config import Settings
from app.main import create_app
from tests.auth_helpers import OP, OPERATOR, FirstPickRng, WorkerClient, enroll, receipt

T0 = 1_800_000_000


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


MODEL_ID = "tiny-test-model"


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"models": [
        {"model_id": MODEL_ID, "file_name": "tiny.gguf", "source_url": "https://example.invalid/t",
         "license": "Apache-2.0", "sha256": None, "size_bytes": None, "min_vram_mb": 1}]}))
    monkeypatch.setenv("MODEL_REGISTRY", str(reg))
    model = tmp_path / "tiny.gguf"
    model.write_bytes(b"GGUF fake weights for tests")
    model_registry.pin(MODEL_ID, model)


@pytest.fixture
def env(tmp_path: Path):
    clock = Clock()
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, operator_token=OPERATOR, audit_rate=1.0)
    app = create_app(settings, clock=clock, rng=FirstPickRng(7))
    return WorkerClient(app), clock, app.state.ledger


def lines(caplog: pytest.LogCaptureFixture) -> list[dict]:
    """Every captured bnh.* record, formatted exactly as the JSON log file would be."""
    fmt = observability.JsonFormatter()
    return [json.loads(fmt.format(r)) for r in caplog.records if r.name.startswith("bnh")]


def submit(client, job_id: str, worker: str, result: str):
    return client.post(f"/v1/jobs/{job_id}/submit", json={
        "worker_id": worker, "result": result, "result_hash": sha(result),
        "started_at": T0, "completed_at": T0, **receipt(client, job_id)})


# -- the formatter -------------------------------------------------------------------------

def test_json_formatter_writes_one_object_per_line_with_extra_fields() -> None:
    record = logging.LogRecord("bnh.test", logging.WARNING, __file__, 1, "hello %s",
                               ("world",), None)
    record.job_id = "j-1"
    record.duration_ms = 1.5
    record.when = object()  # not JSON-serialisable: must not crash the logger
    out = observability.JsonFormatter().format(record)
    assert "\n" not in out
    data = json.loads(out)
    assert data["level"] == "warning" and data["logger"] == "bnh.test"
    assert data["msg"] == "hello world" and data["job_id"] == "j-1"
    assert data["duration_ms"] == 1.5 and isinstance(data["when"], str)
    assert data["ts"].endswith("Z")


def test_json_formatter_redacts_secret_field_names() -> None:
    record = logging.LogRecord("bnh.test", logging.INFO, __file__, 1, "x", None, None)
    for name in ("worker_key", "authorization", "operator_token", "enrollment_code",
                 "payload", "result", "expected_result", "signature", "private_key"):
        setattr(record, name, "SECRET-VALUE")
    out = observability.JsonFormatter().format(record)
    assert "SECRET-VALUE" not in out
    assert json.loads(out)["worker_key"] == "[redacted]"


def test_json_formatter_includes_exceptions() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys
        record = logging.LogRecord("bnh.test", logging.ERROR, __file__, 1, "failed", None,
                                   sys.exc_info())
    data = json.loads(observability.JsonFormatter().format(record))
    assert data["error_type"] == "RuntimeError" and "boom" in data["error"]


def test_configure_logging_validates_and_does_not_stack_handlers(monkeypatch) -> None:
    logger = logging.getLogger("bnh")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    try:
        monkeypatch.setenv("LOG_FORMAT", "xml")
        with pytest.raises(ValueError, match="LOG_FORMAT"):
            observability.configure_logging()
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_LEVEL", "chatty")
        with pytest.raises(ValueError, match="LOG_LEVEL"):
            observability.configure_logging()
        monkeypatch.setenv("LOG_LEVEL", "debug")
        observability.configure_logging()
        observability.configure_logging()
        ours = [h for h in logger.handlers if getattr(h, "_bnh_handler", False)]
        assert len(ours) == 1 and isinstance(ours[0].formatter, observability.JsonFormatter)
        assert logger.level == logging.DEBUG
        monkeypatch.setenv("LOG_FORMAT", "text")
        observability.configure_logging()
        ours = [h for h in logger.handlers if getattr(h, "_bnh_handler", False)]
        assert len(ours) == 1 and not isinstance(ours[0].formatter, observability.JsonFormatter)
    finally:
        logger.handlers[:] = saved[0]
        logger.setLevel(saved[1])
        logger.propagate = saved[2]


# -- request and lifecycle logs ------------------------------------------------------------

def test_every_request_gets_one_access_line_and_a_request_id(env, caplog) -> None:
    client, _, _ = env
    caplog.set_level(logging.INFO, logger="bnh")
    r = client.get("/health", params={"probe": "secret-in-query"})
    rid = r.headers["X-Request-ID"]
    access = [l for l in lines(caplog) if l["msg"] == "request"]
    assert len(access) == 1
    line = access[0]
    assert line["method"] == "GET" and line["path"] == "/health" and line["status"] == 200
    assert line["request_id"] == rid and line["duration_ms"] >= 0
    assert "secret-in-query" not in json.dumps(lines(caplog))  # query strings aren't logged


def test_request_id_from_caller_is_echoed_only_if_safe(env) -> None:
    client, _, _ = env
    assert client.get("/health", headers={"X-Request-ID": "rig-7.abc_1"}
                      ).headers["X-Request-ID"] == "rig-7.abc_1"
    for bad in ("x" * 200, "evil value with spaces", "a{}b"):
        echoed = client.get("/health", headers={"X-Request-ID": bad}).headers["X-Request-ID"]
        assert echoed != bad and len(echoed) == 32


def test_full_workflow_logs_contain_no_secrets_or_job_content(env, caplog) -> None:
    client, clock, _ = env
    caplog.set_level(logging.DEBUG, logger="bnh")
    code = client.post("/v1/enrollments", json={"owner": "owner-a"}, headers=OP
                       ).json()["enrollment_code"]
    reg = client.post("/v1/workers/register", json={"worker_id": "wk-a",
                                                    "enrollment_code": code}).json()
    key_a = reg["worker_key"]
    client.worker_keys["wk-a"] = key_a
    key_b = enroll(client, "wk-b")
    payload = "PAYLOAD-SHOULD-NOT-BE-LOGGED"
    job = client.post("/v1/jobs", json={"payload": payload}).json()
    assert client.post("/v1/jobs/next", params={"worker_id": "wk-a"}).status_code == 200
    assert submit(client, job["job_id"], "wk-a", sha(payload)).status_code == 200
    secret_answer = sha("CHALLENGE-ANSWER-PAYLOAD")
    client.post("/v1/challenges", json={"payload": "CHALLENGE-ANSWER-PAYLOAD",
                                        "expected_result": secret_answer}, headers=OP)
    client.post("/v1/jobs/next", headers={"Authorization": "Bearer bnhw_not-a-real-key"})
    client.get("/v1/rewards/report", params={"until": T0 + 1}, headers=OP)
    client.get("/v1/operator/health", headers=OP)
    text = json.dumps(lines(caplog))
    for secret in (OPERATOR, code, key_a, key_b, "bnhw_not-a-real-key", payload,
                   "CHALLENGE-ANSWER-PAYLOAD", secret_answer, sha(payload)):
        assert secret not in text, secret
    assert "challenge" not in [l.get("job_kind") for l in lines(caplog)]


def test_lifecycle_events_are_logged_with_ids(env, caplog) -> None:
    client, _, _ = env
    caplog.set_level(logging.INFO, logger="bnh")
    enroll(client, "wk-a")
    job = client.post("/v1/jobs", json={"payload": "boats"}).json()
    client.post("/v1/jobs/next", params={"worker_id": "wk-a"})
    submit(client, job["job_id"], "wk-a", sha("boats"))
    by_msg = {l["msg"]: l for l in lines(caplog)}
    assert by_msg["worker enrolled"]["worker_id"] == "wk-a"
    assert by_msg["job created"]["job_id"] == job["job_id"]
    assert by_msg["job created"]["task_type"] == "demo:sha256"
    assert by_msg["job leased"]["worker_id"] == "wk-a"
    done = by_msg["result submitted"]
    assert done["job_id"] == job["job_id"] and done["outcome"] == "accepted"
    assert done["level"] == "info"


def test_refused_requests_are_warnings_with_a_reason(env, caplog) -> None:
    client, _, _ = env
    caplog.set_level(logging.INFO, logger="bnh")
    enroll(client, "wk-a")
    client.post("/v1/jobs/next", headers={"Authorization": "Bearer bnhw_wrong"})
    client.get("/v1/audits", headers={"X-Operator-Token": "wrong-token"})
    job = client.post("/v1/jobs", json={"payload": "boats"}).json()
    client.post("/v1/jobs/next", params={"worker_id": "wk-a"})
    bad = receipt(client, job["job_id"]) | {"payload_hash": "0" * 64}
    client.post(f"/v1/jobs/{job['job_id']}/submit", json={
        "worker_id": "wk-a", "result": sha("boats"), "result_hash": sha(sha("boats")),
        "started_at": T0, "completed_at": T0, **bad})
    warnings = [l for l in lines(caplog) if l["level"] == "warning"]
    reasons = {l["msg"]: l for l in warnings}
    assert reasons["worker auth refused"]["reason"] == "unknown worker key"
    assert reasons["operator auth refused"]["reason"] == "wrong operator token"
    assert reasons["receipt refused"]["job_id"] == job["job_id"]
    assert "bnhw_wrong" not in json.dumps(warnings) and "wrong-token" not in json.dumps(warnings)


# -- health ---------------------------------------------------------------------------------

def test_public_health_reports_checks_and_uptime_but_nothing_private(env) -> None:
    client, clock, _ = env
    clock.t = T0 + 42
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "signing_key": "loaded"}
    assert body["uptime_seconds"] == 42
    assert "workers" not in body and "warnings" not in body


def test_public_health_is_503_when_the_database_fails(env, monkeypatch) -> None:
    client, _, ledger = env

    def broken() -> None:
        raise sqlite3.OperationalError("disk I/O error at /secret/path")
    monkeypatch.setattr(ledger, "ping", broken)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "down" and r.json()["checks"]["database"] == "error"
    assert "/secret/path" not in r.text


def test_operator_health_needs_the_token(env) -> None:
    client, _, _ = env
    assert client.get("/v1/operator/health").status_code == 403
    assert client.get("/v1/operator/health",
                      headers={"X-Operator-Token": "nope"}).status_code == 403


def test_operator_health_with_nobody_online_says_so(env) -> None:
    client, _, _ = env
    body = client.get("/v1/operator/health", headers=OP).json()
    assert body["status"] == "attention"
    assert body["workers"] == {"registered": 0, "active": 0, "revoked": 0}
    assert any("no active workers" in w for w in body["warnings"])


def test_operator_health_counts_workers_queue_and_waiting_audits(env) -> None:
    client, clock, ledger = env
    enroll(client, "wk-a")
    enroll(client, "wk-b", owner="owner-wk-a")  # same owner: can't audit wk-a
    enroll(client, "wk-c")
    client.post("/v1/workers/wk-c/revoke", headers=OP)
    first = client.post("/v1/jobs", json={"payload": "one"}).json()
    clock.t = T0 + 10
    client.post("/v1/jobs", json={"payload": "two"})
    client.post("/v1/jobs/next", params={"worker_id": "wk-a"})  # leases "one"
    clock.t = T0 + 100  # past the 30 s lease, before any sweep
    body = client.get("/v1/operator/health", headers=OP).json()
    assert body["workers"] == {"registered": 3, "active": 2, "revoked": 1}
    assert body["queue"]["queued"] == 1 and body["queue"]["leased"] == 1
    assert body["queue"]["oldest_queued_age_seconds"] == 90
    assert body["queue"]["overdue_leases"] == 1
    assert body["consistency_problems"] == []
    assert ledger.get_job(first["job_id"])["status"] == "leased"  # health is read-only


def test_operator_health_flags_audits_nobody_can_take(pinned, env) -> None:
    client, _, _ = env
    enroll(client, "wk-a")
    enroll(client, "wk-b", owner="owner-wk-a")
    # An AI result triggers an audit (audit_rate=1.0); the only other worker shares the
    # owner, so nobody is eligible and the audit waits.
    payload = json.dumps({"model_id": MODEL_ID, "prompt": "Name a boat.", "max_tokens": 16,
                          "seed": 7, "timeout_s": 30})
    assert client.post("/v1/jobs", json={"task_type": "llm:generate",
                                         "payload": payload}).status_code == 200
    job = client.post("/v1/jobs/next", params={"worker_id": "wk-a"}).json()["job_id"]
    assert submit(client, job, "wk-a", "The Salty Hoe").status_code == 200
    body = client.get("/v1/operator/health", headers=OP).json()
    assert body["audits"]["open"] == 1 and body["audits"]["waiting_for_auditor"] == 1
    assert body["status"] == "attention"
    assert any("waiting for an eligible auditor" in w for w in body["warnings"])


def test_operator_health_reports_consistency_problems(env) -> None:
    client, _, ledger = env
    enroll(client, "wk-a")
    job = client.post("/v1/jobs", json={"payload": "x"}).json()
    with sqlite3.connect(ledger.path) as db:  # tamper: an accepted job with no submission
        db.execute("UPDATE jobs SET status='accepted' WHERE job_id=?", (job["job_id"],))
    body = client.get("/v1/operator/health", headers=OP).json()
    assert body["status"] == "attention"
    assert any(job["job_id"] in p for p in body["consistency_problems"])
    assert any("consistency" in w for w in body["warnings"])


def test_operator_health_is_ok_when_everything_is_quiet(env) -> None:
    client, _, _ = env
    enroll(client, "wk-a")
    job = client.post("/v1/jobs", json={"payload": "x"}).json()
    client.post("/v1/jobs/next", params={"worker_id": "wk-a"})
    submit(client, job["job_id"], "wk-a", sha("x"))
    body = client.get("/v1/operator/health", headers=OP).json()
    assert body["status"] == "ok" and body["warnings"] == []
    assert body["recent_events"]["result_accepted"] == 1



def test_text_format_quotes_values_so_a_worker_cant_forge_a_line() -> None:
    record = logging.LogRecord("bnh.test", logging.WARNING, __file__, 1, "receipt refused",
                               None, None)
    record.job_id = "j-1"
    record.reason = 'bad\n2026-01-01 INFO bnh.coordinator: result submitted outcome=accepted'
    out = observability.TextFormatter().format(record)
    assert "\n" not in out and "job_id=j-1" in out
    assert 'reason="bad\\n2026' in out
