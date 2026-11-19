from __future__ import annotations

import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from .config import get_settings
from .ledger import Ledger
from .models import JobCreate, JobEnvelope, ResultSubmission, WorkerRegistration
from .security import sha256_text, sign_manifest

settings = get_settings()
ledger = Ledger(settings.database_path)
app = FastAPI(title="Boats N Hoes Useful-Compute MVP", version="0.1.0")
queue: deque[str] = deque()
jobs: dict[str, dict[str, Any]] = {}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "local-prototype"}


@app.post("/v1/workers/register")
def register_worker(body: WorkerRegistration) -> dict[str, str]:
    ledger.register_worker(body.worker_id, json.dumps(body.capabilities), int(time.time()))
    return {"worker_id": body.worker_id, "status": "registered"}


@app.post("/v1/jobs", response_model=JobEnvelope)
def create_job(body: JobCreate) -> JobEnvelope:
    now = int(time.time())
    job_id = str(uuid.uuid4())
    fields = {
        "job_id": job_id,
        "task_type": body.task_type,
        "payload": body.payload,
        "payload_hash": sha256_text(body.payload),
        "reward_points": body.reward_points,
        "expires_at": now + body.ttl_seconds,
    }
    signature = sign_manifest(fields, settings.secret)
    jobs[job_id] = {**fields, "signature": signature, "status": "queued", "worker_id": None}
    queue.append(job_id)
    return JobEnvelope(**jobs[job_id])


@app.post("/v1/jobs/next", response_model=JobEnvelope)
def next_job(worker_id: str) -> JobEnvelope:
    if not ledger.has_worker(worker_id):
        raise HTTPException(status_code=403, detail="worker is not registered")
    now = int(time.time())
    while queue:
        job_id = queue.popleft()
        job = jobs.get(job_id)
        if not job or job["status"] != "queued":
            continue
        if job["expires_at"] <= now:
            job["status"] = "expired"
            continue
        job["status"] = "leased"
        job["worker_id"] = worker_id
        return JobEnvelope(**{k: job[k] for k in JobEnvelope.model_fields})
    raise HTTPException(status_code=404, detail="no queued jobs")


@app.post("/v1/jobs/{job_id}/submit")
def submit_result(job_id: str, body: ResultSubmission) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    if job["status"] != "leased":
        raise HTTPException(status_code=409, detail="job is not awaiting a result")
    if job["worker_id"] != body.worker_id:
        raise HTTPException(status_code=403, detail="job leased to a different worker")
    now = int(time.time())
    if job["expires_at"] <= now:
        job["status"] = "expired"
        raise HTTPException(status_code=410, detail="job expired")
    if body.completed_at < body.started_at or body.completed_at > now + 30:
        raise HTTPException(status_code=422, detail="invalid worker timestamps")
    if sha256_text(body.result) != body.result_hash:
        raise HTTPException(status_code=422, detail="result hash mismatch")

    # Deterministic demo verifier: the task is SHA256(payload). This verifies
    # the answer, not that the assigned GPU executed the task.
    expected = sha256_text(job["payload"])
    if body.result != expected:
        raise HTTPException(status_code=422, detail="task result failed deterministic verification")
    try:
        ledger.record_submission(job_id, body.worker_id, body.result_hash, job["reward_points"], now)
    except Exception as exc:
        # A duplicate ledger primary key is treated as a conflict. Do not expose
        # database internals to the client.
        raise HTTPException(status_code=409, detail="duplicate or previously recorded submission") from exc
    job["status"] = "accepted"
    return {"job_id": job_id, "status": "accepted", "reward_points": job["reward_points"],
            "verification": "deterministic-answer-check", "execution_proof": False}


@app.get("/v1/ledger")
def read_ledger() -> dict[str, Any]:
    entries = ledger.entries()
    return {"entries": entries, "total_simulated_points": ledger.total_points(),
            "notice": "Simulated points only; not tokens or money."}
