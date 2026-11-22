from __future__ import annotations

import hmac
import json
import logging
import random
import time
import uuid
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException

from .config import Settings, get_settings
from . import rewards, tasks, workers
from .ledger import Ledger
from .models import (ChallengeCreate, EnrollmentCreate, JobCreate, JobEnvelope, ResultSubmission,
                     WorkerRegistration)
from .security import sha256_text
from .signing import ManifestSigner

log = logging.getLogger("bnh.coordinator")

# Fields covered by the coordinator's Ed25519 signature. Workers must verify exactly these.
SIGNED_FIELDS = ("job_id", "task_type", "payload", "payload_hash", "reward_points", "expires_at")

# Audit-trail events that would tell a worker which jobs are audits or challenges.
# They stay in the database for the operator but are hidden from the public job view.
PRIVATE_EVENTS = {"audit_scheduled", "audit_match", "audit_mismatch", "audit_not_run",
                  "passed_challenge", "failed_challenge", "tiebreak_scheduled",
                  "tiebreak_majority", "tiebreak_unresolved", "tiebreak_not_run",
                  "auditor_assigned", "auditor_unassigned"}


def create_app(settings: Settings | None = None,
               clock: Callable[[], float] = time.time,
               rng: random.Random | None = None) -> FastAPI:
    """Build the coordinator. `clock` and `rng` are injectable so tests are deterministic.
    The default `rng` uses the OS's secure randomness, so workers can't predict audits."""
    rng = rng or random.SystemRandom()
    settings = settings or get_settings()
    # Threat model #9: only the coordinator holds the private key; workers get the public one.
    signer = ManifestSigner.load_or_create(settings.resolved_signing_key_path())
    log.info("manifest signing key loaded", extra={"fingerprint": signer.fingerprint})
    ledger = Ledger(settings.database_path, rng=rng,
                    active_seconds=settings.auditor_active_seconds,
                    assign_seconds=settings.audit_assign_seconds)
    app = FastAPI(title="Boats N Hoes Useful-Compute MVP", version="0.5.0")
    app.state.ledger = ledger
    app.state.settings = settings
    app.state.signer = signer

    def now() -> int:
        return int(clock())
    app.state.now = now

    def envelope(job: dict[str, Any]) -> JobEnvelope:
        return JobEnvelope(**{k: job[k] for k in JobEnvelope.model_fields})

    def new_job_fields(task_type: str, payload: str, reward_points: int,
                       expires_at: int) -> tuple[dict[str, Any], str]:
        fields = {
            "job_id": str(uuid.uuid4()),
            "task_type": task_type,
            "payload": payload,
            "payload_hash": sha256_text(payload),
            "reward_points": reward_points,
            "expires_at": expires_at,
        }
        return fields, signer.sign(fields)

    def require_operator(token: str | None) -> None:
        expected = settings.operator_token
        if not expected:
            raise HTTPException(status_code=403, detail="operator endpoints are disabled "
                                "(set OPERATOR_TOKEN)")
        if not token or not hmac.compare_digest(token.encode(), expected.encode()):
            raise HTTPException(status_code=403, detail="operator token required")

    def authenticate_worker(authorization: str | None, claimed_id: str | None) -> str:
        """Return the worker id that owns the bearer key, or raise 401/403.
        If the request also names a worker id, it must be the key's own worker."""
        scheme, _, key = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            raise HTTPException(status_code=401, detail="worker key required",
                                headers={"WWW-Authenticate": "Bearer"})
        worker = ledger.worker_for_key(sha256_text(key.strip()))
        if worker is None:
            raise HTTPException(status_code=401, detail="unknown worker key",
                                headers={"WWW-Authenticate": "Bearer"})
        if worker["revoked_at"] is not None:
            raise HTTPException(status_code=403, detail="worker key revoked")
        if claimed_id is not None and claimed_id != worker["worker_id"]:
            raise HTTPException(status_code=403, detail="key belongs to a different worker")
        return str(worker["worker_id"])

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "mode": "local-prototype", "jobs": ledger.status_counts()}

    @app.get("/v1/coordinator/key")
    def coordinator_key() -> dict[str, Any]:
        """Public: the key workers use to check job signatures. Safe to publish.
        Workers should pin it once (compare the fingerprint with the operator) and refuse
        any later change, so a fake coordinator can't just serve its own key."""
        return {"algorithm": "ed25519", "public_key": signer.public_key_hex,
                "fingerprint": signer.fingerprint}

    @app.post("/v1/enrollments")
    def create_enrollment(body: EnrollmentCreate,
                          x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only: a one-time code that lets one new worker register.
        Only its hash is stored, so this response is the only time it is shown."""
        require_operator(x_operator_token)
        code, expires_at = workers.issue_code(ledger, body.owner, body.ttl_seconds, now())
        return {"enrollment_code": code, "owner": body.owner, "expires_at": expires_at,
                "notice": "Single use. Give it to the rig owner privately."}

    @app.post("/v1/workers/register")
    def register_worker(body: WorkerRegistration,
                        authorization: str | None = Header(default=None)) -> dict[str, Any]:
        capabilities = json.dumps(body.capabilities)
        if authorization:  # an existing worker updating what it can run
            worker_id = authenticate_worker(authorization, body.worker_id)
            ledger.update_capabilities(worker_id, capabilities, now())
            return {"worker_id": worker_id, "status": "updated"}
        if not body.enrollment_code:
            raise HTTPException(status_code=401, detail="enrollment code required "
                                "(the operator creates one with POST /v1/enrollments)")
        key = workers.new_worker_key()
        outcome, owner = ledger.enroll_worker(body.worker_id, capabilities,
                                              sha256_text(body.enrollment_code),
                                              sha256_text(key), now())
        if outcome == "bad_code":
            raise HTTPException(status_code=403,
                                detail="enrollment code is invalid, used or expired")
        if outcome == "taken":
            raise HTTPException(status_code=409, detail="worker id is already registered; "
                                "send its key to update it")
        if outcome == "revoked":
            raise HTTPException(status_code=403, detail="worker is revoked")
        log.info("worker enrolled", extra={"worker_id": body.worker_id})
        return {"worker_id": body.worker_id, "status": "registered", "owner": owner,
                "worker_key": key,
                "notice": "Shown once. Keep it private; the coordinator stores only its hash."}

    @app.post("/v1/workers/{worker_id}/revoke")
    def revoke_worker(worker_id: str,
                      x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only: block a worker's key and take back any job it is holding."""
        require_operator(x_operator_token)
        if not ledger.has_worker(worker_id):
            raise HTTPException(status_code=404, detail="unknown worker")
        changed = ledger.revoke_worker(worker_id, now())
        return {"worker_id": worker_id, "status": "revoked", "already_revoked": not changed}

    @app.post("/v1/jobs", response_model=JobEnvelope)
    def create_job(body: JobCreate) -> JobEnvelope:
        try:
            tasks.get(body.task_type).validate(body.payload)
        except tasks.TaskError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        t = now()
        fields, signature = new_job_fields(body.task_type, body.payload, body.reward_points,
                                           t + body.ttl_seconds)
        ledger.create_job(fields, signature, settings.max_attempts, t)
        return JobEnvelope(**fields, signature=signature)

    @app.post("/v1/challenges")
    def create_challenge(body: ChallengeCreate,
                         x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only: queue a hidden known-answer job. Workers can't tell it apart."""
        require_operator(x_operator_token)
        executor = tasks.get(body.task_type)
        try:
            executor.validate(body.payload)
        except tasks.TaskError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not executor.verify(body.payload, body.expected_result).ok:
            raise HTTPException(status_code=422,
                                detail="expected_result fails this task's own verifier")
        t = now()
        fields, signature = new_job_fields(body.task_type, body.payload, body.reward_points,
                                           t + body.ttl_seconds)
        ledger.create_job(fields, signature, settings.max_attempts, t, job_kind="challenge",
                          expected_result=body.expected_result)
        return {"job_id": fields["job_id"], "status": "queued", "job_kind": "challenge"}

    @app.get("/v1/audits")
    def list_audits(x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_operator(x_operator_token)
        return {"audits": ledger.audits(), "audit_rate": settings.audit_rate}

    @app.get("/v1/workers/{worker_id}/trust")
    def worker_trust(worker_id: str,
                     x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_operator(x_operator_token)
        if not ledger.has_worker(worker_id):
            raise HTTPException(status_code=404, detail="unknown worker")
        return ledger.worker_trust(worker_id)

    @app.get("/v1/rewards/report")
    def reward_report(since: int = 0, until: int | None = None,
                      x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only Phase 5 report. Operator-only because a live per-job settlement
        view could let a worker spot a failed hidden challenge right after submitting;
        publish a report only after its period has closed."""
        require_operator(x_operator_token)
        try:
            return rewards.build_report(ledger, since, until if until is not None else now() + 1)
        except rewards.ReportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = ledger.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        public = ("job_id", "task_type", "status", "attempts", "max_attempts", "expires_at",
                  "lease_expires_at", "worker_id")
        events = [e for e in ledger.events(job_id) if e["event"] not in PRIVATE_EVENTS]
        return {k: job[k] for k in public} | {"events": events}

    @app.post("/v1/jobs/next", response_model=JobEnvelope)
    def next_job(worker_id: str | None = None,
                 authorization: str | None = Header(default=None)) -> JobEnvelope:
        worker_id = authenticate_worker(authorization, worker_id)
        job = ledger.claim_next(worker_id, settings.lease_seconds, now())
        if job is None:
            raise HTTPException(status_code=404, detail="no queued jobs")
        return envelope(job)

    @app.post("/v1/jobs/{job_id}/submit")
    def submit_result(job_id: str, body: ResultSubmission,
                      authorization: str | None = Header(default=None)) -> dict[str, Any]:
        worker_id = authenticate_worker(authorization, body.worker_id)
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
        executor = tasks.get(job["task_type"])
        verdict = executor.verify(job["payload"], body.result)
        # Phase 4: an unproven work result is re-run by a different worker at `audit_rate`.
        # The audit job is prepared here and only stored if the result is accepted.
        # Same time window as the original, so the deadline doesn't give the audit away.
        window = job["expires_at"] - job["created_at"]
        audit_job = tiebreak_job = None
        if (verdict.ok and verdict.needs_audit and job["job_kind"] == "work"
                and rng.random() < settings.audit_rate):
            audit_job = new_job_fields(job["task_type"], job["payload"], job["reward_points"],
                                       t + window)
        # A tie-breaker is prepared for every audit result but only stored on a mismatch.
        if verdict.ok and job["job_kind"] == "audit" and settings.tiebreak:
            tiebreak_job = new_job_fields(job["task_type"], job["payload"],
                                          job["reward_points"], t + window)
        outcome, job = ledger.submit(job_id, worker_id, body.result_hash, verdict.ok, t,
                                     verdict.method, verdict.confidence, result=body.result,
                                     needs_audit=verdict.needs_audit, audit_job=audit_job,
                                     agree=executor.results_agree, tiebreak_job=tiebreak_job)
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
                "points_by_status": ledger.points_by_status(),
                "jobs": ledger.status_counts(), "consistency_problems": ledger.check_consistency(),
                "notice": "Simulated points only; not tokens or money."}

    return app


def __getattr__(name: str) -> Any:
    # `uvicorn app.main:app` builds the app lazily so importing this module (tests,
    # tooling) never touches the default database or creates a signing key.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
