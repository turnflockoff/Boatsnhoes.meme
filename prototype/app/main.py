from __future__ import annotations

import hmac
import json
import logging
import random
import time
import uuid
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from . import audit_policy, observability, rewards, schemas, tasks, workers
from .ledger import Ledger
from .models import (ChallengeCreate, EnrollmentCreate, JobCreate, JobEnvelope, ResultSubmission,
                     WorkerRegistration)
from .security import sha256_text
from .signing import ManifestSigner

log = logging.getLogger("bnh.coordinator")
access_log = logging.getLogger("bnh.access")

# Fields covered by the coordinator's Ed25519 signature (versioned; see app/schemas.py).
SIGNED_FIELDS = schemas.SIGNED_FIELDS

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
    started_at = now()

    @app.middleware("http")
    async def log_requests(request: Request, call_next: Callable[..., Any]) -> Any:
        """One structured line per request: path only (no query, headers or body), status
        and duration. The request id is echoed back so a worker's error can be matched
        to the coordinator's log line."""
        request_id = (observability.safe_request_id(request.headers.get("x-request-id"))
                      or uuid.uuid4().hex)
        fields = {"request_id": request_id, "method": request.method,
                  "path": request.url.path}
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            access_log.exception("request failed", extra=fields | {"status": 500})
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        level = logging.WARNING if response.status_code >= 500 else logging.INFO
        access_log.log(level, "request", extra=fields | {"status": response.status_code,
                                                          "duration_ms": duration_ms})
        return response

    def envelope(job: dict[str, Any]) -> JobEnvelope:
        return JobEnvelope(**{k: job[k] for k in JobEnvelope.model_fields})

    def new_job_fields(task_type: str, payload: str, reward_points: int, expires_at: int,
                       model_sha256: str | None) -> tuple[dict[str, Any], str]:
        fields = {
            "job_id": str(uuid.uuid4()),
            "task_type": task_type,
            "payload": payload,
            "payload_hash": sha256_text(payload),
            "reward_points": reward_points,
            "expires_at": expires_at,
            "schema_version": schemas.JOB_SCHEMA_VERSION,
            "model_sha256": model_sha256,
        }
        assert set(fields) == set(SIGNED_FIELDS)
        return fields, signer.sign(fields)

    def validated_model_hash(task_type: str, payload: str) -> str | None:
        """Validate a new job's payload and return the pinned model hash it will carry."""
        executor = tasks.get(task_type)
        try:
            executor.validate(payload)
            return executor.model_sha256(payload)
        except tasks.TaskError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def require_operator(token: str | None) -> None:
        expected = settings.operator_token
        if not expected:
            log.warning("operator auth refused", extra={"reason": "operator endpoints disabled"})
            raise HTTPException(status_code=403, detail="operator endpoints are disabled "
                                "(set OPERATOR_TOKEN)")
        if not token or not hmac.compare_digest(token.encode(), expected.encode()):
            log.warning("operator auth refused", extra={"reason": "operator token required"})
            raise HTTPException(status_code=403, detail="operator token required")

    def authenticate_worker(authorization: str | None, claimed_id: str | None) -> str:
        """Return the worker id that owns the bearer key, or raise 401/403.
        If the request also names a worker id, it must be the key's own worker."""
        def refuse(code: int, reason: str, worker_id: str | None = None) -> HTTPException:
            # Never log the key itself: only why it was refused and whose it was.
            log.warning("worker auth refused", extra={"reason": reason, "worker_id": worker_id,
                                                      "claimed_worker_id": claimed_id})
            headers = {"WWW-Authenticate": "Bearer"} if code == 401 else None
            return HTTPException(status_code=code, detail=reason, headers=headers)

        scheme, _, key = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            raise refuse(401, "worker key required")
        worker = ledger.worker_for_key(sha256_text(key.strip()))
        if worker is None:
            raise refuse(401, "unknown worker key")
        if worker["revoked_at"] is not None:
            raise refuse(403, "worker key revoked", worker["worker_id"])
        if claimed_id is not None and claimed_id != worker["worker_id"]:
            raise refuse(403, "key belongs to a different worker", worker["worker_id"])
        return str(worker["worker_id"])

    @app.get("/health")
    def health() -> Any:
        """Public liveness check for a monitor or load balancer: 200 if the coordinator can
        read its database, 503 if not. Deliberately says nothing about workers or audits."""
        body: dict[str, Any] = {"mode": "local-prototype",
                                "uptime_seconds": now() - started_at,
                                "job_schema": schemas.JOB_SCHEMA_VERSION,
                                "receipt_schema": schemas.RECEIPT_SCHEMA_VERSION}
        try:
            ledger.ping()
            jobs = ledger.status_counts()
        except Exception as exc:  # noqa: BLE001 - any database failure means "down"
            # The error text can contain file paths: log its type, don't return it.
            log.error("health check failed", extra={"check": "database",
                                                    "error_type": type(exc).__name__})
            return JSONResponse(status_code=503, content=body | {
                "status": "down", "checks": {"database": "error", "signing_key": "loaded"}})
        return body | {"status": "ok", "jobs": jobs,
                       "checks": {"database": "ok", "signing_key": "loaded"}}

    @app.get("/v1/operator/health")
    def operator_health(x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only: workers, queue, audits waiting for someone eligible, recent events
        and the ledger's self-checks, plus plain-language warnings. Read-only."""
        require_operator(x_operator_token)
        t = now()
        snap = ledger.health_snapshot(t)
        problems = ledger.check_consistency()
        warnings: list[str] = []
        if snap["workers"]["active"] == 0:
            warnings.append(f"no active workers in the last {settings.auditor_active_seconds} "
                            "seconds")
        waiting = snap["audits"]["waiting_for_auditor"]
        if waiting:
            warnings.append(f"{waiting} audit/tie-breaker job(s) waiting for an eligible "
                            "auditor: needs an active worker with a different owner")
        if snap["queue"]["overdue_leases"]:
            warnings.append(f"{snap['queue']['overdue_leases']} lease(s) past due; they are "
                            "requeued on the next worker request")
        if snap["queue"]["past_deadline"]:
            warnings.append(f"{snap['queue']['past_deadline']} job(s) past their deadline; "
                            "they expire on the next worker request")
        struck = sorted(w for w, h in ledger.audit_history().items() if h["strikes"])
        if struck:
            warnings.append(f"{len(struck)} worker(s) with a strike (failed hidden test or "
                            f"outvoted): {', '.join(struck)}. Their results are now re-checked "
                            "more often; look at /v1/workers/<id>/trust and decide whether to "
                            "revoke")
        if problems:
            warnings.append(f"{len(problems)} consistency problem(s): see "
                            "consistency_problems")
        return {"status": "attention" if warnings else "ok", "checked_at": t,
                "uptime_seconds": t - started_at, "signing_key_fingerprint": signer.fingerprint,
                **snap, "consistency_problems": problems, "warnings": warnings}

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
        log.info("worker revoked", extra={"worker_id": worker_id, "already_revoked": not changed})
        return {"worker_id": worker_id, "status": "revoked", "already_revoked": not changed}

    @app.post("/v1/jobs", response_model=JobEnvelope)
    def create_job(body: JobCreate) -> JobEnvelope:
        model_sha256 = validated_model_hash(body.task_type, body.payload)
        t = now()
        fields, signature = new_job_fields(body.task_type, body.payload, body.reward_points,
                                           t + body.ttl_seconds, model_sha256)
        ledger.create_job(fields, signature, settings.max_attempts, t)
        log.info("job created", extra={"job_id": fields["job_id"], "task_type": body.task_type})
        return JobEnvelope(**fields, signature=signature)

    @app.post("/v1/challenges")
    def create_challenge(body: ChallengeCreate,
                         x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only: queue a hidden known-answer job. Workers can't tell it apart."""
        require_operator(x_operator_token)
        model_sha256 = validated_model_hash(body.task_type, body.payload)
        if not tasks.get(body.task_type).verify(body.payload, body.expected_result).ok:
            raise HTTPException(status_code=422,
                                detail="expected_result fails this task's own verifier")
        t = now()
        fields, signature = new_job_fields(body.task_type, body.payload, body.reward_points,
                                           t + body.ttl_seconds, model_sha256)
        ledger.create_job(fields, signature, settings.max_attempts, t, job_kind="challenge",
                          expected_result=body.expected_result)
        # Same line as a normal job: logs never say which jobs are hidden tests.
        log.info("job created", extra={"job_id": fields["job_id"], "task_type": body.task_type})
        return {"job_id": fields["job_id"], "status": "queued", "job_kind": "challenge"}

    @app.get("/v1/audits")
    def list_audits(x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_operator(x_operator_token)
        return {"audits": ledger.audits(), "audit_rate": settings.audit_rate,
                "audit_policy": audit_policy.describe(settings)}

    @app.get("/v1/workers/{worker_id}/trust")
    def worker_trust(worker_id: str,
                     x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_operator(x_operator_token)
        if not ledger.has_worker(worker_id):
            raise HTTPException(status_code=404, detail="unknown worker")
        # Operator-only: the tier says how closely this rig is watched (threat model #1).
        decision = audit_policy.decide_for(ledger.audit_history(worker_id), settings)
        return ledger.worker_trust(worker_id) | {
            "audit_tier": decision.tier, "audit_rate": decision.rate,
            "checks_passed": decision.checks_passed, "strikes": decision.strikes,
            "audit_policy": audit_policy.AUDIT_POLICY_VERSION}

    @app.get("/v1/rewards/report")
    def reward_report(since: int = 0, until: int | None = None, sign: bool = False,
                      x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only Phase 5 report. Operator-only because a live per-job settlement
        view could let a worker spot a failed hidden challenge right after submitting;
        publish a report only after its period has closed. `sign=true` adds the
        coordinator's Ed25519 signature and is refused while the period is still open."""
        require_operator(x_operator_token)
        try:
            t = now()
            report = rewards.build_report(ledger, since, until if until is not None else t + 1)
            return rewards.sign_report(report, signer, t) if sign else report
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
        log.info("job leased", extra={"job_id": job["job_id"], "worker_id": worker_id,
                                      "attempt": job["attempts"]})
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
        receipt = body.model_dump()
        if receipt["receipt_version"] != schemas.RECEIPT_SCHEMA_VERSION:
            log.warning("receipt refused", extra={"job_id": job_id, "worker_id": worker_id,
                                                  "reason": "unsupported receipt_version"})
            raise HTTPException(status_code=422, detail=schemas.receipt_problem(receipt, {}))
        job = ledger.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        # Track E: the receipt must say it ran this job version, on this input, with this
        # model. A mismatch is a mistake or a stale worker; it changes nothing but the log.
        problem = schemas.receipt_problem(receipt, job)
        if problem:
            ledger.note(t, "receipt_mismatch", job_id, worker_id, problem)
            log.warning("receipt refused", extra={"job_id": job_id, "worker_id": worker_id,
                                                  "reason": problem})
            raise HTTPException(status_code=422, detail=problem)
        # Task-specific verifier. It checks the answer (or only its shape, for tasks
        # marked "schema-only"); it never proves which hardware executed the task.
        executor = tasks.get(job["task_type"])
        verdict = executor.verify(job["payload"], body.result)
        # Phase 4: an unproven work result is re-run by a different worker. How often
        # depends on this worker's record (`audit-tier-v1`, threat model #1): new rigs and
        # rigs with a strike are re-checked more often than established ones.
        # The audit job is prepared here and only stored if the result is accepted.
        # Same time window as the original, so the deadline doesn't give the audit away.
        window = job["expires_at"] - job["created_at"]
        audit_job = tiebreak_job = None
        if (verdict.ok and verdict.needs_audit and job["job_kind"] == "work"
                and rng.random() < audit_policy.decide_for(
                    ledger.audit_history(worker_id), settings).rate):
            # Copy the original's model hash: a re-pin since then must not change the job.
            audit_job = new_job_fields(job["task_type"], job["payload"], job["reward_points"],
                                       t + window, job["model_sha256"])
        # A tie-breaker is prepared for every audit result but only stored on a mismatch.
        if verdict.ok and job["job_kind"] == "audit" and settings.tiebreak:
            tiebreak_job = new_job_fields(job["task_type"], job["payload"],
                                          job["reward_points"], t + window, job["model_sha256"])
        outcome, job = ledger.submit(job_id, worker_id, body.result_hash, verdict.ok, t,
                                     verdict.method, verdict.confidence, result=body.result,
                                     needs_audit=verdict.needs_audit, audit_job=audit_job,
                                     agree=executor.results_agree, tiebreak_job=tiebreak_job)
        log.log(logging.INFO if outcome in ("accepted", "duplicate") else logging.WARNING,
                "result submitted", extra={"job_id": job_id, "worker_id": worker_id,
                                           "outcome": outcome, "verification": verdict.method})
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
        observability.configure_logging()
        app = create_app()
        return app
    raise AttributeError(name)
