from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable

from fastapi import FastAPI, HTTPException

from .config import Settings, get_settings
from . import tasks
from .ledger import Ledger
from .models import JobCreate, JobEnvelope, ResultSubmission, WorkerRegistration
from .security import sha256_text, sign_manifest

log = logging.getLogger("bnh.coordinator")

# Fields covered by the coordinator's HMAC signature. Workers must verify exactly these.
SIGNED_FIELDS = ("job_id", "task_type", "payload", "payload_hash", "reward_points", "expires_at")


def create_app(settings: Settings | None = None,
               clock: Callable[[], float] = time.time) -> FastAPI:
    """Build the coordinator. `clock` is injectable so tests can move time forward."""
    settings = settings or get_settings()
    ledger = Ledger(settings.database_path)
    app = FastAPI(title="Boats N Hoes Useful-Compute MVP", version="0.3.0")
    app.state.ledger = ledger
    app.state.settings = settings

    def now() -> int:
        return int(clock())

    def envelope(job: dict[str, Any]) -> JobEnvelope:
        return JobEnvelope(**{k: job[k] for k in JobEnvelope.model_fields})

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "mode": "local-prototype", "jobs": ledger.status_counts()}

    @app.post("/v1/workers/register")
    def register_worker(body: WorkerRegistration) -> dict[str, str]:
        ledger.register_worker(body.worker_id, json.dumps(body.capabilities), now())
        log.info("worker registered", extra={"worker_id": body.worker_id})
        return {"worker_id": body.worker_id, "status": "registered"}

    @app.post("/v1/jobs", response_model=JobEnvelope)
    def create_job(body: JobCreate) -> JobEnvelope:
        try:
            tasks.get(body.task_type).validate(body.payload)
        except tasks.TaskError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        t = now()
        fields = {
            "job_id": str(uuid.uuid4()),
            "task_type": body.task_type,
            "payload": body.payload,
            "payload_hash": sha256_text(body.payload),
            "reward_points": body.reward_points,
            "expires_at": t + body.ttl_seconds,
        }
        signature = sign_manifest(fields, settings.secret)
        ledger.create_job(fields, signature, settings.max_attempts, t)
        return JobEnvelope(**fields, signature=signature)

    @app.get("/v1/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = ledger.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        public = ("job_id", "task_type", "status", "attempts", "max_attempts", "expires_at",
                  "lease_expires_at", "worker_id")
        return {k: job[k] for k in public} | {"events": ledger.events(job_id)}

    @app.post("/v1/jobs/next", response_model=JobEnvelope)
    def next_job(worker_id: str) -> JobEnvelope:
        if not ledger.has_worker(worker_id):
            raise HTTPException(status_code=403, detail="worker is not registered")
        job = ledger.claim_next(worker_id, settings.lease_seconds, now())
        if job is None:
            raise HTTPException(status_code=404, detail="no queued jobs")
        return envelope(job)

    @app.post("/v1/jobs/{job_id}/submit")
    def submit_result(job_id: str, body: ResultSubmission) -> dict[str, Any]:
        t = now()
        if body.completed_at < body.started_at or body.completed_at > t + 30:
            raise HTTPException(status_code=422, detail="invalid worker timestamps")
        if sha256_text(body.result) != body.result_hash:
            raise HTTPException(status_code=422, detail="result hash mismatch")
        job = ledger.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        # Task-specific verifier. It checks the answer (or only its shape, for tasks
        # marked "schema-only"); it never proves which hardware executed the task.
        verdict = tasks.get(job["task_type"]).verify(job["payload"], body.result)
        outcome, job = ledger.submit(job_id, body.worker_id, body.result_hash, verdict.ok, t,
                                     verdict.method, verdict.confidence)
        receipt = {"job_id": job_id, "verification": verdict.method,
                   "confidence": verdict.confidence, "needs_audit": verdict.needs_audit,
                   "execution_proof": False}
        if outcome in ("accepted", "duplicate"):
            return receipt | {"status": "accepted", "reward_points": job["reward_points"],
                              "idempotent_replay": outcome == "duplicate"}
        errors = {
            "unknown": (404, "unknown job"),
            "conflict": (409, "job already accepted with a different result"),
            "not_leased": (409, "job is not awaiting a result"),
            "wrong_worker": (403, "job leased to a different worker"),
            "expired": (410, "job expired"),
            "rejected_requeued": (422, "result failed verification; job requeued"),
            "rejected_final": (422, "result failed verification; no attempts left"),
        }
        code, detail = errors[outcome]
        raise HTTPException(status_code=code, detail=detail)

    @app.get("/v1/ledger")
    def read_ledger() -> dict[str, Any]:
        return {"entries": ledger.entries(), "total_simulated_points": ledger.total_points(),
                "jobs": ledger.status_counts(), "consistency_problems": ledger.check_consistency(),
                "notice": "Simulated points only; not tokens or money."}

    return app


def __getattr__(name: str) -> Any:
    # `uvicorn app.main:app` builds the app lazily so importing this module (tests,
    # tooling) never touches the default database or requires a configured secret.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
