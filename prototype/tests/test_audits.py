"""Phase 4: spot re-run audits, hidden known-answer challenge jobs, verifier records."""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import model_registry, tasks
from app.config import Settings
from app.ledger import Ledger
from app.main import create_app
from tests.auth_helpers import WorkerClient, enroll

SECRET = "test-secret-that-is-long-enough-123"
OPERATOR = "operator-token-that-is-long-enough-456"
T0 = 1_800_000_000
MODEL_ID = "tiny-test-model"
OP = {"X-Operator-Token": OPERATOR}


def sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


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


def make(tmp_path: Path, audit_rate: float = 1.0, operator_token: str | None = OPERATOR):
    clock = Clock()
    settings = Settings(secret=SECRET, database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, audit_rate=audit_rate, operator_token=operator_token)
    app = create_app(settings, clock=clock, rng=random.Random(1))
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b", "wk-c"):
        enroll(client, w)
    return client, clock, app.state.ledger


def llm_payload(prompt: str = "Name a boat.") -> str:
    return json.dumps({"model_id": MODEL_ID, "prompt": prompt, "max_tokens": 16, "seed": 7,
                       "timeout_s": 30})


def claim(client: TestClient, worker: str):
    return client.post("/v1/jobs/next", params={"worker_id": worker})


def submit(client: TestClient, job_id: str, worker: str, result: str):
    return client.post(f"/v1/jobs/{job_id}/submit", json={
        "worker_id": worker, "result": result, "result_hash": sha(result),
        "started_at": T0, "completed_at": T0})


def audit_status(ledger: Ledger, job_id: str) -> str:
    return ledger.submission(job_id)["audit_status"]


def run_llm_job(client: TestClient, worker: str, output: str) -> str:
    job = client.post("/v1/jobs", json={"task_type": "llm:generate", "payload": llm_payload()})
    assert job.status_code == 200
    job_id = claim(client, worker).json()["job_id"]
    assert submit(client, job_id, worker, output).status_code == 200
    return job_id


def test_schema_only_result_gets_rerun_by_a_different_worker(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert audit_status(ledger, original) == "pending"
    # The worker that produced the result can never audit itself.
    assert claim(client, "wk-a").status_code == 404
    audit = claim(client, "wk-b").json()
    assert audit["payload"] == llm_payload() and audit["job_id"] != original
    first = client.get(f"/v1/jobs/{original}").json()
    assert audit["expires_at"] - T0 == 300  # same window as the original job
    assert all(e["event"] != "audit_scheduled" for e in first["events"])
    assert submit(client, audit["job_id"], "wk-b", "The Salty Hoe").status_code == 200
    for job_id in (original, audit["job_id"]):
        sub = ledger.submission(job_id)
        assert sub["audit_status"] == "confirmed" and sub["confidence"] == "replicated"
    # An audit job never spawns an audit of its own.
    assert claim(client, "wk-c").status_code == 404
    body = client.get("/v1/ledger").json()
    assert body["points_by_status"]["confirmed"] == 2 and body["consistency_problems"] == []


def test_disagreeing_rerun_holds_both_results(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    audit_id = claim(client, "wk-b").json()["job_id"]
    submit(client, audit_id, "wk-b", "A totally different boat")
    assert audit_status(ledger, original) == "disputed"
    assert audit_status(ledger, audit_id) == "disputed"
    body = client.get("/v1/ledger").json()
    assert body["points_by_status"]["held"] == 2 and body["points_by_status"]["confirmed"] == 0
    events = [e["event"] for e in ledger.events(original)]
    assert "audit_mismatch" in events


def test_llm_tolerance_ignores_whitespace_only(pinned, tmp_path: Path) -> None:
    llm = tasks.get("llm:generate")
    assert llm.results_agree("The  Salty\nHoe ", "The Salty Hoe")
    assert not llm.results_agree("The Salty Hoe", "The salty hoe")
    assert not tasks.get("demo:sha256").results_agree("abc ", "abc")


def test_spot_rate_zero_leaves_result_unaudited(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, audit_rate=0.0)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert audit_status(ledger, original) == "unaudited"
    assert claim(client, "wk-b").status_code == 404
    assert client.get("/v1/ledger").json()["points_by_status"]["unverified"] == 1


def test_exactly_checked_tasks_are_never_rerun(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = client.post("/v1/jobs", json={"payload": "hello"}).json()["job_id"]
    claim(client, "wk-a")
    submit(client, job_id, "wk-a", sha("hello"))
    assert audit_status(ledger, job_id) == "not-needed"
    assert claim(client, "wk-b").status_code == 404


def test_audit_that_never_runs_marks_result_unaudited(pinned, tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    clock.t += 301  # the re-run gets the original's 300 s window; nobody picks it up
    ledger.sweep(int(clock()))
    assert audit_status(ledger, original) == "unaudited"
    assert "audit_not_run" in [e["event"] for e in ledger.events(original)]
    assert ledger.check_consistency() == []


def test_challenge_endpoint_requires_operator_token(tmp_path: Path) -> None:
    body = {"payload": "x", "expected_result": sha("x")}
    client, _, _ = make(tmp_path)
    assert client.post("/v1/challenges", json=body).status_code == 403
    assert client.post("/v1/challenges", json=body,
                       headers={"X-Operator-Token": "wrong-token-xxxxxxxxxxxxxxxxx"}).status_code == 403
    assert client.post("/v1/challenges", json=body, headers=OP).status_code == 200


def test_challenge_endpoint_disabled_without_configured_token(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path / "x", operator_token=None)
    r = client.post("/v1/challenges", json={"payload": "x", "expected_result": sha("x")},
                    headers=OP)
    assert r.status_code == 403
    assert client.get("/v1/audits", headers=OP).status_code == 403


def test_challenge_with_wrong_expected_answer_is_refused(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    r = client.post("/v1/challenges", json={"payload": "x", "expected_result": sha("y")},
                    headers=OP)
    assert r.status_code == 422


def test_hidden_challenge_looks_like_normal_work(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    normal = client.post("/v1/jobs", json={"task_type": "llm:generate", "payload": llm_payload()})
    challenge = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload("Name a ship."),
        "expected_result": "The Flagship"}).json()
    first, second = claim(client, "wk-a").json(), claim(client, "wk-a").json()
    assert set(first) == set(second)  # identical envelope shape
    assert "expected_result" not in json.dumps(second) and "challenge" not in json.dumps(second)
    status = client.get(f"/v1/jobs/{challenge['job_id']}").json()
    assert "challenge" not in json.dumps(status) and "expected" not in json.dumps(status)
    # A wrong challenge answer is accepted on the surface (so cheaters can't tell)...
    r_challenge = submit(client, challenge["job_id"], "wk-a", "Made up answer")
    r_normal = submit(client, normal.json()["job_id"], "wk-a", "Boaty")
    assert r_challenge.status_code == 200 and r_normal.status_code == 200
    assert set(r_challenge.json()) == set(r_normal.json())
    assert r_challenge.json()["needs_audit"] == r_normal.json()["needs_audit"]
    # ...but the operator sees it failed and its points are held.
    assert audit_status(ledger, challenge["job_id"]) == "failed-challenge"
    trust = client.get("/v1/workers/wk-a/trust", headers=OP).json()
    assert trust["challenges_failed"] == 1 and trust["challenges_passed"] == 0
    assert client.get("/v1/workers/wk-a/trust").status_code == 403
    audits = client.get("/v1/audits", headers=OP).json()["audits"]
    kinds = {a["job_kind"] for a in audits}
    assert kinds == {"audit", "challenge"}


def test_passed_challenge_counts_as_confirmed(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    c = client.post("/v1/challenges", headers=OP,
                    json={"payload": "known", "expected_result": sha("known")}).json()
    claim(client, "wk-b")
    assert submit(client, c["job_id"], "wk-b", sha("known")).status_code == 200
    assert audit_status(ledger, c["job_id"]) == "passed-challenge"
    assert client.get("/v1/workers/wk-b/trust", headers=OP).json()["challenges_passed"] == 1
    assert client.get("/v1/ledger").json()["points_by_status"]["confirmed"] == 1


def test_phase2_database_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, payload TEXT NOT NULL,
            payload_hash TEXT NOT NULL, reward_points INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            signature TEXT NOT NULL, status TEXT NOT NULL, worker_id TEXT, lease_expires_at INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE submissions (job_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL,
            result_hash TEXT NOT NULL, reward_points INTEGER NOT NULL, accepted_at INTEGER NOT NULL,
            verifier_method TEXT NOT NULL DEFAULT 'deterministic-recompute',
            confidence TEXT NOT NULL DEFAULT 'exact');
        INSERT INTO jobs VALUES ('j1','demo:sha256','p','h',1,9,'s','accepted','w',NULL,1,3,1,1);
        INSERT INTO submissions VALUES ('j1','w','r',1,1,'deterministic-recompute','exact');
    """)
    db.commit()
    db.close()
    ledger = Ledger(path)
    assert ledger.submission("j1")["audit_status"] == "not-needed"
    assert ledger.get_job("j1")["job_kind"] == "work"
    assert ledger.check_consistency() == []


# -- Phase 4 (rest): tie-breaker re-runs for disputed audits ---------------------------

def make_dispute(client: TestClient, original_text: str = "The Salty Hoe",
                 audit_text: str = "A totally different boat") -> tuple[str, str]:
    original = run_llm_job(client, "wk-a", original_text)
    audit_id = claim(client, "wk-b").json()["job_id"]
    assert submit(client, audit_id, "wk-b", audit_text).status_code == 200
    return original, audit_id


def test_dispute_schedules_hidden_tiebreak_for_a_third_worker(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    original, audit_id = make_dispute(client)
    # Neither side of the dispute may break the tie.
    assert claim(client, "wk-a").status_code == 404
    assert claim(client, "wk-b").status_code == 404
    tiebreak = claim(client, "wk-c").json()
    assert tiebreak["payload"] == llm_payload()
    assert tiebreak["job_id"] not in (original, audit_id)
    assert tiebreak["expires_at"] - T0 == 300  # same window as normal work
    for job_id in (original, audit_id, tiebreak["job_id"]):
        public = json.dumps(client.get(f"/v1/jobs/{job_id}").json())
        assert "tiebreak" not in public and "audit" not in public
    # Both sides stay held while the tie-breaker runs.
    assert client.get("/v1/ledger").json()["points_by_status"]["held"] == 2
    assert ledger.check_consistency() == []


def test_tiebreak_siding_with_original_confirms_it(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    original, audit_id = make_dispute(client)
    tb = claim(client, "wk-c").json()["job_id"]
    assert submit(client, tb, "wk-c", "The  Salty Hoe\n").status_code == 200
    for job_id in (original, tb):
        sub = ledger.submission(job_id)
        assert sub["audit_status"] == "confirmed" and sub["confidence"] == "replicated"
    assert audit_status(ledger, audit_id) == "outvoted"
    body = client.get("/v1/ledger").json()
    assert body["points_by_status"] == {"confirmed": 2, "pending": 0, "unverified": 0, "held": 1}
    assert body["consistency_problems"] == []
    assert client.get("/v1/workers/wk-b/trust", headers=OP).json()["audits_outvoted"] == 1
    # A tie-breaker never spawns another audit or tie-breaker.
    enroll(client, "wk-d")
    assert claim(client, "wk-d").status_code == 404


def test_tiebreak_siding_with_auditor_outvotes_original(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    original, audit_id = make_dispute(client)
    tb = claim(client, "wk-c").json()["job_id"]
    submit(client, tb, "wk-c", "A totally different boat")
    assert audit_status(ledger, original) == "outvoted"
    assert audit_status(ledger, audit_id) == "confirmed"
    assert audit_status(ledger, tb) == "confirmed"
    assert "tiebreak_majority" in [e["event"] for e in ledger.events(original)]
    trust = client.get("/v1/workers/wk-a/trust", headers=OP).json()
    assert trust["audits_outvoted"] == 1


def test_three_way_disagreement_is_unresolved(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    original, audit_id = make_dispute(client)
    tb = claim(client, "wk-c").json()["job_id"]
    submit(client, tb, "wk-c", "Yet another answer")
    for job_id in (original, audit_id, tb):
        assert audit_status(ledger, job_id) == "unresolved"
    assert client.get("/v1/ledger").json()["points_by_status"]["held"] == 3
    kinds = [a["job_kind"] for a in client.get("/v1/audits", headers=OP).json()["audits"]]
    assert sorted(kinds) == ["audit", "tiebreak"]


def test_tiebreak_that_never_runs_keeps_dispute_held(pinned, tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    original, audit_id = make_dispute(client)
    clock.t += 301  # nobody else picks up the tie-breaker (e.g. only two rigs online)
    ledger.sweep(int(clock()))
    assert audit_status(ledger, original) == "disputed"
    assert audit_status(ledger, audit_id) == "disputed"
    assert "tiebreak_not_run" in [e["event"] for e in ledger.events(original)]
    assert ledger.check_consistency() == []


def test_tiebreak_can_be_switched_off(pinned, tmp_path: Path) -> None:
    clock = Clock()
    settings = Settings(secret=SECRET, database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, audit_rate=1.0, operator_token=OPERATOR, tiebreak=False)
    client = WorkerClient(create_app(settings, clock=clock, rng=random.Random(1)))
    for w in ("wk-a", "wk-b", "wk-c"):
        enroll(client, w)
    make_dispute(client)
    assert claim(client, "wk-c").status_code == 404


def test_consistency_check_flags_bad_tiebreak_records(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    original, audit_id = make_dispute(client)
    tb = claim(client, "wk-c").json()["job_id"]
    submit(client, tb, "wk-c", "The Salty Hoe")
    assert ledger.check_consistency() == []
    db = sqlite3.connect(ledger.path)
    # Tamper: pretend the original worker broke its own tie...
    db.execute("UPDATE submissions SET worker_id='wk-a' WHERE job_id=?", (tb,))
    # ...and an unrelated result claims to be outvoted without any tie-breaker.
    db.execute("INSERT INTO jobs(job_id, task_type, payload, payload_hash, reward_points, "
               "expires_at, signature, status, max_attempts, created_at, updated_at) "
               "VALUES('lonely','demo:sha256','p','h',1,9,'s','accepted',2,1,1)")
    db.execute("INSERT INTO submissions(job_id, worker_id, result_hash, reward_points, "
               "accepted_at, audit_status) VALUES('lonely','wk-c','r',1,1,'outvoted')")
    db.commit()
    db.close()
    problems = ledger.check_consistency()
    assert f"worker broke a tie on its own dispute {tb}" in problems
    assert "tie-breaker outcome without a tie-breaker lonely" in problems
