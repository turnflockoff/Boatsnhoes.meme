from __future__ import annotations

import hashlib
import time

from fastapi.testclient import TestClient

from app.main import app, jobs, queue, ledger


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def setup_function() -> None:
    jobs.clear()
    queue.clear()


def test_worker_can_process_and_submit_job() -> None:
    client = TestClient(app)
    worker = "test-worker-1"
    assert client.post("/v1/workers/register", json={"worker_id": worker}).status_code == 200
    created = client.post("/v1/jobs", json={"payload": "hello boats"})
    assert created.status_code == 200
    job = created.json()
    claimed = client.post("/v1/jobs/next", params={"worker_id": worker})
    assert claimed.status_code == 200
    now = int(time.time())
    result = sha("hello boats")
    response = client.post(f"/v1/jobs/{job['job_id']}/submit", json={
        "worker_id": worker, "result": result, "result_hash": sha(result),
        "started_at": now, "completed_at": now
    })
    assert response.status_code == 200
    assert response.json()["verification"] == "deterministic-answer-check"
    assert response.json()["execution_proof"] is False


def test_unregistered_worker_cannot_claim() -> None:
    client = TestClient(app)
    created = client.post("/v1/jobs", json={"payload": "hello"})
    assert created.status_code == 200
    response = client.post("/v1/jobs/next", params={"worker_id": "unknown-worker"})
    assert response.status_code == 403


def test_wrong_answer_is_rejected() -> None:
    client = TestClient(app)
    worker = "test-worker-2"
    client.post("/v1/workers/register", json={"worker_id": worker})
    job = client.post("/v1/jobs", json={"payload": "hello"}).json()
    client.post("/v1/jobs/next", params={"worker_id": worker})
    now = int(time.time())
    result = "not-the-answer"
    response = client.post(f"/v1/jobs/{job['job_id']}/submit", json={
        "worker_id": worker, "result": result, "result_hash": sha(result),
        "started_at": now, "completed_at": now
    })
    assert response.status_code == 422


def test_job_cannot_be_claimed_twice_after_lease() -> None:
    client = TestClient(app)
    client.post("/v1/workers/register", json={"worker_id": "worker-3"})
    client.post("/v1/workers/register", json={"worker_id": "worker-4"})
    client.post("/v1/jobs", json={"payload": "once"})
    assert client.post("/v1/jobs/next", params={"worker_id": "worker-3"}).status_code == 200
    assert client.post("/v1/jobs/next", params={"worker_id": "worker-4"}).status_code == 404
