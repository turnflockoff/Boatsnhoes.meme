from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.ledger import Ledger
from app.main import create_app
from demo_worker import ManifestError, check_manifest
from tests.auth_helpers import OPERATOR, WorkerClient, enroll

T0 = 1_800_000_000


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(tmp_path: Path):
    clock = Clock()
    settings = Settings(database_path=tmp_path / "db.sqlite3",
                        lease_seconds=30, max_attempts=2, operator_token=OPERATOR)
    app = create_app(settings, clock=clock)
    return WorkerClient(app), clock, settings, app.state.ledger


def register(client: WorkerClient, *workers: str) -> None:
    for w in workers:
        enroll(client, w)


def submit(client: TestClient, job_id: str, worker: str, result: str, t: int = T0):
    return client.post(f"/v1/jobs/{job_id}/submit", json={
        "worker_id": worker, "result": result, "result_hash": sha(result),
        "started_at": t, "completed_at": t})


def test_worker_can_process_and_submit_job(env) -> None:
    client, _, _, ledger = env
    register(client, "wk-1")
    job = client.post("/v1/jobs", json={"payload": "hello boats"}).json()
    claimed = client.post("/v1/jobs/next", params={"worker_id": "wk-1"})
    assert claimed.status_code == 200
    pub = client.get("/v1/coordinator/key").json()["public_key"]
    check_manifest(claimed.json(), pub, now=T0)  # worker-side verification passes
    response = submit(client, job["job_id"], "wk-1", sha("hello boats"))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted" and body["execution_proof"] is False
    assert ledger.total_points() == 1
    assert ledger.check_consistency() == []


def test_unregistered_worker_cannot_claim(env) -> None:
    client, *_ = env
    client.post("/v1/jobs", json={"payload": "hello"})
    assert client.post("/v1/jobs/next", params={"worker_id": "nobody"}).status_code == 401


def test_job_cannot_be_claimed_twice_while_leased(env) -> None:
    client, *_ = env
    register(client, "wk-1", "wk-2")
    client.post("/v1/jobs", json={"payload": "once"})
    assert client.post("/v1/jobs/next", params={"worker_id": "wk-1"}).status_code == 200
    assert client.post("/v1/jobs/next", params={"worker_id": "wk-2"}).status_code == 404


def test_wrong_answer_requeues_then_rejects_when_attempts_run_out(env) -> None:
    client, _, _, ledger = env
    register(client, "wk-1")
    job = client.post("/v1/jobs", json={"payload": "hello"}).json()
    client.post("/v1/jobs/next", params={"worker_id": "wk-1"})
    first = submit(client, job["job_id"], "wk-1", "not-the-answer")
    assert first.status_code == 422 and "requeued" in first.json()["detail"]
    assert ledger.get_job(job["job_id"])["status"] == "queued"
    client.post("/v1/jobs/next", params={"worker_id": "wk-1"})
    second = submit(client, job["job_id"], "wk-1", "still-wrong")
    assert second.status_code == 422 and "no attempts left" in second.json()["detail"]
    assert ledger.get_job(job["job_id"])["status"] == "rejected"
    assert ledger.total_points() == 0


def test_jobs_survive_coordinator_restart(env, tmp_path: Path) -> None:
    client, clock, settings, _ = env
    register(client, "wk-1")
    job = client.post("/v1/jobs", json={"payload": "persist me"}).json()
    # New process, same database: the worker's key still works after the restart.
    restarted = WorkerClient(create_app(settings, clock=clock), keys=client.worker_keys)
    claimed = restarted.post("/v1/jobs/next", params={"worker_id": "wk-1"})
    assert claimed.status_code == 200 and claimed.json()["job_id"] == job["job_id"]
    assert submit(restarted, job["job_id"], "wk-1", sha("persist me")).status_code == 200


def test_silent_worker_lease_expires_and_job_is_requeued(env) -> None:
    client, clock, _, ledger = env
    register(client, "slow", "fast")
    job = client.post("/v1/jobs", json={"payload": "lease"}).json()
    client.post("/v1/jobs/next", params={"worker_id": "slow"})
    clock.t += 31  # lease_seconds=30
    again = client.post("/v1/jobs/next", params={"worker_id": "fast"})
    assert again.status_code == 200 and again.json()["job_id"] == job["job_id"]
    late = submit(client, job["job_id"], "slow", sha("lease"), t=clock.t)
    assert late.status_code == 403  # the original holder lost the lease
    assert submit(client, job["job_id"], "fast", sha("lease"), t=clock.t).status_code == 200
    events = [e["event"] for e in ledger.events(job["job_id"])]
    assert "lease_timeout_requeued" in events and events[-1] == "result_accepted"


def test_lease_timeouts_exhaust_attempts_and_fail_the_job(env) -> None:
    client, clock, _, ledger = env
    register(client, "wk-1")
    job = client.post("/v1/jobs", json={"payload": "flaky", "ttl_seconds": 600}).json()
    for _ in range(2):  # max_attempts=2
        assert client.post("/v1/jobs/next", params={"worker_id": "wk-1"}).status_code == 200
        clock.t += 31
    assert client.post("/v1/jobs/next", params={"worker_id": "wk-1"}).status_code == 404
    assert ledger.get_job(job["job_id"])["status"] == "failed"


def test_expired_job_cannot_be_claimed_or_submitted(env) -> None:
    client, clock, _, ledger = env
    register(client, "wk-1")
    job = client.post("/v1/jobs", json={"payload": "late", "ttl_seconds": 10}).json()
    client.post("/v1/jobs/next", params={"worker_id": "wk-1"})
    clock.t += 11
    assert submit(client, job["job_id"], "wk-1", sha("late"), t=clock.t).status_code == 410
    assert client.post("/v1/jobs/next", params={"worker_id": "wk-1"}).status_code == 404
    assert ledger.get_job(job["job_id"])["status"] == "expired"


def test_duplicate_submission_is_idempotent_and_paid_once(env) -> None:
    client, _, _, ledger = env
    register(client, "wk-1", "wk-2")
    job = client.post("/v1/jobs", json={"payload": "dup", "reward_points": 5}).json()
    client.post("/v1/jobs/next", params={"worker_id": "wk-1"})
    assert submit(client, job["job_id"], "wk-1", sha("dup")).status_code == 200
    replay = submit(client, job["job_id"], "wk-1", sha("dup"))
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
    assert submit(client, job["job_id"], "wk-2", sha("dup")).status_code == 409
    assert ledger.total_points() == 5 and len(ledger.entries()) == 1


def test_malformed_results_are_rejected_before_touching_the_ledger(env) -> None:
    client, _, _, ledger = env
    register(client, "wk-1")
    job = client.post("/v1/jobs", json={"payload": "x"}).json()
    client.post("/v1/jobs/next", params={"worker_id": "wk-1"})
    bad_hash = client.post(f"/v1/jobs/{job['job_id']}/submit", json={
        "worker_id": "wk-1", "result": sha("x"), "result_hash": sha("other"),
        "started_at": T0, "completed_at": T0})
    assert bad_hash.status_code == 422
    backwards = client.post(f"/v1/jobs/{job['job_id']}/submit", json={
        "worker_id": "wk-1", "result": sha("x"), "result_hash": sha(sha("x")),
        "started_at": T0, "completed_at": T0 - 5})
    assert backwards.status_code == 422
    assert submit(client, "no-such-job", "wk-1", sha("x")).status_code == 404
    assert ledger.get_job(job["job_id"])["status"] == "leased"


def test_concurrent_claims_never_double_lease(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "race.sqlite3")
    for i in range(5):
        ledger.create_job({"job_id": f"job-{i}", "task_type": "demo:sha256", "payload": str(i),
                           "payload_hash": sha(str(i)), "reward_points": 1,
                           "expires_at": T0 + 600}, "sig", 3, T0)
    wins: list[str] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        job = ledger.claim_next(f"w{n}", 30, T0)
        if job:
            with lock:
                wins.append(job["job_id"])

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(wins) == [f"job-{i}" for i in range(5)]  # each job exactly once
    assert ledger.check_consistency() == []


def test_worker_refuses_tampered_or_foreign_manifests(env, tmp_path: Path) -> None:
    from app.signing import ManifestSigner

    client, *_ = env
    register(client, "wk-1")
    client.post("/v1/jobs", json={"payload": "legit", "reward_points": 1})
    job = client.post("/v1/jobs/next", params={"worker_id": "wk-1"}).json()
    pub = client.get("/v1/coordinator/key").json()["public_key"]
    other = ManifestSigner.load_or_create(tmp_path / "other-coordinator.key").public_key_hex
    check_manifest(job, pub, now=T0)
    with pytest.raises(ManifestError, match="bad signature"):
        check_manifest(job | {"reward_points": 999}, pub, now=T0)
    with pytest.raises(ManifestError, match="bad signature"):
        check_manifest(job, other, now=T0)  # signed by a different coordinator
    with pytest.raises(ManifestError, match="expired"):
        check_manifest(job, pub, now=job["expires_at"] + 1)
    with pytest.raises(ManifestError, match="missing"):
        check_manifest({k: v for k, v in job.items() if k != "signature"}, pub, now=T0)


def test_signing_key_never_appears_in_api_responses(env) -> None:
    client, _, settings, _ = env
    register(client, "wk-1")
    job = client.post("/v1/jobs", json={"payload": "s"}).json()
    client.post("/v1/jobs/next", params={"worker_id": "wk-1"})
    pem = settings.resolved_signing_key_path().read_text()
    body = "".join(line for line in pem.splitlines() if "-----" not in line)
    for path in ("/health", "/v1/ledger", f"/v1/jobs/{job['job_id']}", "/v1/coordinator/key"):
        text = client.get(path).text
        assert body not in text and "PRIVATE KEY" not in text
