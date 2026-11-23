"""Per-worker keys and authenticated registration.

These tests use a plain TestClient and set every header by hand, so nothing is hidden by
the WorkerClient helper the other test files use.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import model_registry, workers
from app.config import Settings
from app.ledger import Ledger
from app.main import create_app
from tests.auth_helpers import flat_audit_rate, OP, OPERATOR, FirstPickRng, receipt

T0 = 1_800_000_000
MODEL_ID = "tiny-test-model"


def sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


def make(tmp_path: Path, audit_rate: float = 0.0, operator_token: str | None = OPERATOR):
    clock = Clock()
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, audit_rate=audit_rate,
                        **flat_audit_rate(audit_rate), operator_token=operator_token)
    app = create_app(settings, clock=clock, rng=FirstPickRng(1))
    return TestClient(app), clock, app.state.ledger


def code_for(client: TestClient, owner: str, ttl: int = 3600) -> str:
    r = client.post("/v1/enrollments", json={"owner": owner, "ttl_seconds": ttl}, headers=OP)
    assert r.status_code == 200, r.text
    return r.json()["enrollment_code"]


def register(client: TestClient, worker_id: str, code: str, **extra):
    return client.post("/v1/workers/register",
                       json={"worker_id": worker_id, "enrollment_code": code} | extra)


def join(client: TestClient, worker_id: str, owner: str | None = None) -> str:
    r = register(client, worker_id, code_for(client, owner or f"owner-{worker_id}"))
    assert r.status_code == 200, r.text
    return r.json()["worker_key"]


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def claim(client: TestClient, key: str, worker_id: str | None = None):
    params = {"worker_id": worker_id} if worker_id else None
    return client.post("/v1/jobs/next", params=params, headers=bearer(key))


def submit(client: TestClient, key: str, job_id: str, result: str,
           worker_id: str | None = None):
    body = {"result": result, "result_hash": sha(result), "started_at": T0, "completed_at": T0,
            **receipt(client, job_id)}
    if worker_id:
        body["worker_id"] = worker_id
    return client.post(f"/v1/jobs/{job_id}/submit", json=body, headers=bearer(key))


# -- registration ----------------------------------------------------------------------

def test_registration_needs_a_valid_one_time_code(tmp_path: Path) -> None:
    client, clock, _ = make(tmp_path)
    no_code = client.post("/v1/workers/register", json={"worker_id": "rig-1"})
    assert no_code.status_code == 401
    assert register(client, "rig-1", "bnhe_" + "x" * 40).status_code == 403  # made-up code
    code = code_for(client, "alice")
    first = register(client, "rig-1", code)
    assert first.status_code == 200
    body = first.json()
    assert body["owner"] == "alice" and body["worker_key"].startswith("bnhw_")
    assert register(client, "rig-2", code).status_code == 403  # single use
    late = code_for(client, "alice", ttl=60)
    clock.t += 61
    assert register(client, "rig-3", late).status_code == 403  # expired


def test_enrollment_is_operator_only_and_disabled_without_token(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    assert client.post("/v1/enrollments", json={"owner": "alice"}).status_code == 403
    assert client.post("/v1/enrollments", json={"owner": "alice"},
                       headers={"X-Operator-Token": "wrong-token-but-long-enough-000"}
                       ).status_code == 403
    assert client.post("/v1/enrollments", json={"owner": "bad owner!"},
                       headers=OP).status_code == 422
    off, _, _ = make(tmp_path / "off", operator_token=None)
    assert off.post("/v1/enrollments", json={"owner": "alice"}, headers=OP).status_code == 403


def test_code_cannot_take_over_an_existing_worker(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    key = join(client, "rig-1", "alice")
    spare = code_for(client, "mallory")
    hijack = register(client, "rig-1", spare)
    assert hijack.status_code == 409
    # The failed attempt didn't use up the code, and rig-1 still belongs to alice.
    assert register(client, "rig-2", spare).status_code == 200
    assert ledger.get_worker("rig-1")["owner"] == "alice"
    assert claim(client, key).status_code == 404  # old key still works (no jobs queued)


def test_existing_worker_updates_capabilities_with_its_key(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    key = join(client, "rig-1")
    r = client.post("/v1/workers/register",
                    json={"worker_id": "rig-1", "capabilities": ["demo:sha256", "llm:generate"]},
                    headers=bearer(key))
    assert r.status_code == 200 and r.json()["status"] == "updated"
    assert "worker_key" not in r.json()  # no new key is handed out
    assert json.loads(ledger.get_worker("rig-1")["capabilities"]) == ["demo:sha256",
                                                                      "llm:generate"]
    other = join(client, "rig-2")
    wrong = client.post("/v1/workers/register", json={"worker_id": "rig-1"},
                        headers=bearer(other))
    assert wrong.status_code == 403


def test_keys_and_codes_are_stored_only_as_hashes(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    code = code_for(client, "alice")
    key = register(client, "rig-1", code).json()["worker_key"]
    dump = "\n".join(sqlite3.connect(ledger.path).iterdump())
    assert key not in dump and code not in dump
    assert sha(key) in dump and sha(code) in dump
    for path in ("/health", "/v1/ledger"):
        assert key not in client.get(path).text
    trust = client.get("/v1/workers/rig-1/trust", headers=OP).json()
    assert trust["owner"] == "alice" and trust["revoked"] is False
    assert key not in json.dumps(trust) and sha(key) not in json.dumps(trust)


# -- using the key ---------------------------------------------------------------------

def test_claim_and_submit_need_the_workers_own_key(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    key_a = join(client, "rig-a")
    key_b = join(client, "rig-b")
    job = client.post("/v1/jobs", json={"payload": "hello boats"}, headers=OP).json()
    assert client.post("/v1/jobs/next").status_code == 401  # no key
    assert client.post("/v1/jobs/next", params={"worker_id": "rig-a"}).status_code == 401
    assert claim(client, "bnhw_" + "z" * 43).status_code == 401  # unknown key
    assert claim(client, key_b, worker_id="rig-a").status_code == 403  # impersonation
    assert claim(client, key_a).status_code == 200  # worker id comes from the key
    # rig-b can't submit for rig-a's lease, with or without naming rig-a.
    assert submit(client, key_b, job["job_id"], sha("hello boats"), "rig-a").status_code == 403
    assert submit(client, key_b, job["job_id"], sha("hello boats")).status_code == 403
    no_key = client.post(f"/v1/jobs/{job['job_id']}/submit", json={
        "result": sha("hello boats"), "result_hash": sha(sha("hello boats")),
        "started_at": T0, "completed_at": T0, **receipt(client, job["job_id"])})
    assert no_key.status_code == 401
    ok = submit(client, key_a, job["job_id"], sha("hello boats"))
    assert ok.status_code == 200 and ok.json()["status"] == "accepted"
    assert ledger.submission(job["job_id"])["worker_id"] == "rig-a"
    assert ledger.check_consistency() == []


def test_revoked_worker_is_locked_out_and_its_job_requeued(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    key_a = join(client, "rig-a")
    key_b = join(client, "rig-b")
    job = client.post("/v1/jobs", json={"payload": "revoke me"}, headers=OP).json()
    assert claim(client, key_a).status_code == 200
    assert client.post("/v1/workers/rig-a/revoke").status_code == 403  # operator only
    r = client.post("/v1/workers/rig-a/revoke", headers=OP)
    assert r.status_code == 200 and r.json()["already_revoked"] is False
    assert client.post("/v1/workers/nobody/revoke", headers=OP).status_code == 404
    assert claim(client, key_a).status_code == 403
    assert submit(client, key_a, job["job_id"], sha("revoke me")).status_code == 403
    # Its lease was taken back right away, so another rig can finish the job.
    assert ledger.get_job(job["job_id"])["status"] == "queued"
    assert claim(client, key_b).json()["job_id"] == job["job_id"]
    assert submit(client, key_b, job["job_id"], sha("revoke me")).status_code == 200
    # A revoked worker can't come back with a fresh code either.
    assert register(client, "rig-a", code_for(client, "owner-rig-a")).status_code == 403
    assert "lease_revoked_requeued" in [e["event"] for e in ledger.events(job["job_id"])]
    assert client.get("/v1/workers/rig-a/trust", headers=OP).json()["revoked"] is True
    assert ledger.check_consistency() == []


# -- one owner, many rigs (Sybil) ------------------------------------------------------

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


def llm_job(client: TestClient) -> str:
    payload = json.dumps({"model_id": MODEL_ID, "prompt": "Name a boat.", "max_tokens": 16,
                          "seed": 7, "timeout_s": 30})
    r = client.post("/v1/jobs", json={"task_type": "llm:generate", "payload": payload}, headers=OP)
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def test_same_owner_never_audits_or_breaks_ties_for_itself(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, audit_rate=1.0)
    a1 = join(client, "alice-1", "alice")
    a2 = join(client, "alice-2", "alice")
    b1 = join(client, "bob-1", "bob")
    c1 = join(client, "carol-1", "carol")
    original = llm_job(client)
    assert claim(client, a1).json()["job_id"] == original
    assert submit(client, a1, original, "The Salty Hoe").status_code == 200
    assert ledger.submission(original)["audit_status"] == "pending"
    # alice's second rig is a different worker id, but the same owner: it never gets
    # the hidden re-run of alice's own result.
    assert claim(client, a2).status_code == 404
    audit = claim(client, b1).json()["job_id"]
    assert submit(client, b1, audit, "Some other boat").status_code == 200  # dispute
    # The tie-breaker is closed to both sides' owners: alice-2 still can't get it.
    assert claim(client, a2).status_code == 404
    tiebreak = claim(client, c1).json()["job_id"]
    assert submit(client, c1, tiebreak, "The Salty Hoe").status_code == 200
    assert ledger.submission(original)["audit_status"] == "confirmed"
    assert ledger.submission(audit)["audit_status"] == "outvoted"
    assert ledger.check_consistency() == []


def test_consistency_check_flags_same_owner_audit(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, audit_rate=1.0)
    a1 = join(client, "alice-1", "alice")
    join(client, "alice-2", "alice")
    b1 = join(client, "bob-1", "bob")
    original = llm_job(client)
    claim(client, a1)
    submit(client, a1, original, "The Salty Hoe")
    audit = claim(client, b1).json()["job_id"]
    assert ledger.check_consistency() == []
    db = sqlite3.connect(ledger.path)
    db.execute("UPDATE jobs SET worker_id='alice-2' WHERE job_id=?", (audit,))  # tamper
    db.commit()
    db.close()
    assert (f"job given to a worker with the same owner as an excluded worker {audit}"
            in ledger.check_consistency())


# -- older databases and the operator CLI ----------------------------------------------

def test_pre_auth_database_is_migrated_and_old_workers_must_enroll(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE workers (worker_id TEXT PRIMARY KEY, "
                     "capabilities TEXT NOT NULL, registered_at INTEGER NOT NULL);"
                     "INSERT INTO workers VALUES('old-rig', '[\"demo:sha256\"]', 1);")
    db.commit()
    db.close()
    client, _, ledger = make(tmp_path)
    assert ledger.get_worker("old-rig")["key_hash"] is None
    client.post("/v1/jobs", json={"payload": "x"}, headers=OP)
    assert client.post("/v1/jobs/next", params={"worker_id": "old-rig"}).status_code == 401
    # An operator code gives the old worker id its first key.
    r = register(client, "old-rig", code_for(client, "owner"))
    assert r.status_code == 200
    assert claim(client, r.json()["worker_key"]).status_code == 200


def test_operator_cli_enrolls_lists_and_revokes(tmp_path: Path, capsys) -> None:
    path = tmp_path / "db.sqlite3"
    Ledger(path)
    assert workers.main(["--db", str(path), "enroll", "--owner", "alice"]) == 0
    code = capsys.readouterr().out.strip()
    assert code.startswith("bnhe_")
    client, clock, _ = make(tmp_path)  # same database file
    clock.t = time.time()  # the CLI uses the real clock
    key = register(client, "rig-1", code).json()["worker_key"]
    assert workers.main(["--db", str(path), "list"]) == 0
    listing = capsys.readouterr().out
    assert "rig-1" in listing and "owner=alice" in listing and "active" in listing
    assert key not in listing
    assert workers.main(["--db", str(path), "revoke", "rig-1"]) == 0
    assert claim(client, key).status_code == 403
    assert workers.main(["--db", str(path), "enroll", "--owner", "no spaces"]) == 2
    assert workers.main(["--db", str(tmp_path / "missing.db"), "list"]) == 2


def test_demo_worker_saves_its_key_privately_and_reuses_it(tmp_path: Path) -> None:
    import stat

    from demo_worker import load_or_enroll

    client, _, ledger = make(tmp_path)
    key_file = tmp_path / "keys" / "worker-rig-1.key"
    with pytest.raises(SystemExit, match="enrollment code"):
        load_or_enroll(client, "rig-1", key_file, None, ["demo:sha256"])
    code = code_for(client, "alice")
    key = load_or_enroll(client, "rig-1", key_file, code, ["demo:sha256"])
    assert key_file.read_text().strip() == key
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    # Second run: the saved key is used, no code needed, and nothing new is issued.
    assert load_or_enroll(client, "rig-1", key_file, None, ["demo:sha256", "llm:generate"]) == key
    assert "llm:generate" in ledger.get_worker("rig-1")["capabilities"]
    with pytest.raises(SystemExit, match="refused"):
        load_or_enroll(client, "rig-2", tmp_path / "other.key", code, ["demo:sha256"])
    client.post("/v1/workers/rig-1/revoke", headers=OP)
    with pytest.raises(SystemExit, match="revoked"):
        load_or_enroll(client, "rig-1", key_file, None, ["demo:sha256"])
