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
from . import (audit_policy, client_address, key_rotation, observability, points,
               public_counts, rate_limit, review, rewards, schemas, tasks, vouches, workers)
from .ledger import PAUSE_VERSION, CoordinatorPaused, HiddenTestRevealed, Ledger
from .models import (ChallengeCreate, EnrollmentCreate, JobCreate, JobEnvelope, PauseRequest,
                     ResultSubmission, WorkerRegistration)
from .security import sha256_text
from .signing import ManifestSigner, SigningKeyError, fingerprint, verify_manifest

log = logging.getLogger("bnh.coordinator")
access_log = logging.getLogger("bnh.access")

# Fields covered by the coordinator's Ed25519 signature (versioned; see app/schemas.py).
SIGNED_FIELDS = schemas.SIGNED_FIELDS

# Audit-trail events that would tell a worker which jobs are audits or challenges.
# They stay in the database for the operator but are hidden from the public job view.
PRIVATE_EVENTS = {"audit_scheduled", "audit_match", "audit_mismatch", "audit_not_run",
                  "passed_challenge", "failed_challenge", "tiebreak_scheduled",
                  "tiebreak_majority", "tiebreak_unresolved", "tiebreak_not_run",
                  "tiebreak_held_for_review",
                  "auditor_assigned", "auditor_unassigned", "audit_assignment_expired",
                  "test_assigned", "test_unassigned", "test_assignment_expired"}

# How long a worker is asked to wait while the coordinator is paused (`pause-v1`).
PAUSED_RETRY_AFTER = 60


class AuthRefused(HTTPException):
    """A credential was sent and it was wrong (bad worker key, operator or submitter token,
    or enrollment code). Counted per client by app/rate_limit.py; enough of them in a row
    and the client is locked out for a while. A *missing* credential is a plain
    HTTPException and isn't counted."""


def create_app(settings: Settings | None = None,
               clock: Callable[[], float] = time.time,
               rng: random.Random | None = None,
               limiter_clock: Callable[[], float] = time.monotonic) -> FastAPI:
    """Build the coordinator. `clock` and `rng` are injectable so tests are deterministic.
    The default `rng` uses the OS's secure randomness, so workers can't predict audits.
    `limiter_clock` drives the request limits (app/rate_limit.py), which measure real
    elapsed time rather than the coordinator's job clock."""
    rng = rng or random.SystemRandom()
    settings = settings or get_settings()
    if settings.points_table and dict(settings.points_table).keys() != tasks.EXECUTORS.keys():
        raise ValueError("points_table must list every task type exactly once")
    # Threat model #9: only the coordinator holds the private key; workers get the public one.
    signer = ManifestSigner.load_or_create(settings.resolved_signing_key_path())
    # Planned rotations (app/key_rotation.py): the public handover chain must end on the key
    # just loaded. Jobs signed by a retired key of this coordinator are re-signed on claim.
    rotations = key_rotation.check_current_key(settings.resolved_signing_key_path(),
                                               signer.public_key_hex)
    retired = key_rotation.retired_keys(rotations)
    # `handover-key-v1`: the offline key's PUBLIC half, announced so workers can pin it.
    handover_key = (settings.handover_public_key or "").strip().lower() or None
    if handover_key and handover_key in retired | {signer.public_key_hex}:
        raise SigningKeyError("COORDINATOR_HANDOVER_PUBLIC_KEY must be a separate offline key, "
                              "not a coordinator signing key")
    if settings.reward_scoring not in rewards.SCORING_RULES:  # fail at start, not mid-report
        raise ValueError(f"unknown reward scoring rule {settings.reward_scoring!r}")
    handover_announcement = (key_rotation.announcement(signer, handover_key)
                             if handover_key else None)
    log.info("manifest signing key loaded", extra={"fingerprint": signer.fingerprint,
                                                   "rotations": len(rotations)})
    ledger = Ledger(settings.database_path, rng=rng,
                    active_seconds=settings.auditor_active_seconds,
                    assign_seconds=settings.audit_assign_seconds,
                    # Hidden tests aimed at a trust tier use the same rule as the audit rate.
                    tier_of=lambda h: audit_policy.decide_for(h, settings).tier,
                    # `auditor-pick-v1`: who may be picked to audit or break a tie.
                    auditor_pick=settings.auditor_pick,
                    # `tiebreak-trust-v1`: whether a tie-breaker's majority needs trusted rigs.
                    tiebreak_decides=settings.tiebreak_decides)
    app = FastAPI(title="Boats N Hoes Useful-Compute MVP", version="0.5.0")
    app.state.ledger = ledger
    app.state.settings = settings
    app.state.signer = signer
    limiter = rate_limit.RateLimiter(settings.rate_limit_per_minute, settings.auth_failure_limit,
                                     settings.auth_failure_window_seconds, limiter_clock)
    app.state.limiter = limiter
    # `public-counts-v1` (threat model #14): public job counts are batched per window.
    batched_counts = public_counts.BatchedCounts(settings.public_counts_interval_seconds,
                                                 ledger.status_counts)
    # Threat model #17 follow-up: behind a listed proxy, limits apply to the real client.
    resolver = client_address.ClientResolver(
        client_address.parse_trusted_proxies(settings.trusted_proxies))
    app.state.client_resolver = resolver
    if resolver.trusted:
        log.info("trusted proxies configured", extra={"trusted_proxies": len(resolver.trusted)})

    def client_of(request: Request) -> str:
        """The client's address, worked out once per request (it is counted for the
        operator health page) and kept in the request scope for later lookups."""
        cached = request.scope.get("bnh_client")
        if cached is None:
            peer = request.client.host if request.client else "unknown"
            cached = resolver.resolve(peer, request.headers.getlist("x-forwarded-for"))
            request.scope["bnh_client"] = cached
        return cached

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
        blocked = limiter.check(client_of(request), request.url.path)
        try:
            if blocked:
                reason, retry_after = blocked
                # The client's address is never logged, only a tag that groups its lines.
                log.warning("request limited", extra={
                    "reason": reason, "retry_after": retry_after,
                    "client_tag": limiter.tag(client_of(request))})
                detail = ("too many failed logins from this address; try again later"
                          if reason == "locked_out" else "too many requests; slow down")
                response = JSONResponse(status_code=429, content={"detail": detail},
                                        headers={"Retry-After": str(retry_after)})
            else:
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

    @app.exception_handler(AuthRefused)
    async def count_auth_failure(request: Request, exc: AuthRefused) -> JSONResponse:
        if limiter.record_auth_failure(client_of(request)):
            log.warning("client locked out", extra={
                "reason": "too many failed logins",
                "window_seconds": settings.auth_failure_window_seconds,
                "client_tag": limiter.tag(client_of(request))})
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                            headers=exc.headers)

    @app.exception_handler(CoordinatorPaused)
    async def coordinator_paused(request: Request, exc: CoordinatorPaused) -> JSONResponse:
        """`pause-v1`: a polite "not now", not an error. The reason stays operator-only."""
        return JSONResponse(status_code=503, headers={"Retry-After": str(PAUSED_RETRY_AFTER)},
                            content={"detail": "coordinator is paused by its operator; "
                                     "try again later", "paused_since": exc.since})

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

    def job_points(task_type: str, requested: int | None) -> int:
        """app/points.py: the table's number when POINTS_TABLE is set, else the request's."""
        try:
            return points.resolve(settings.points_table, task_type, requested)
        except points.PointsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def current_signature(job: dict[str, Any]) -> dict[str, Any]:
        """Return the job with a signature from the current key. A job queued before a
        planned rotation still carries the retired key's signature; re-sign it, but only if
        that old signature really checks out against a key this coordinator handed over
        from. A signature that matches no key of ours (e.g. a row edited by hand) is sent
        unchanged, so the worker refuses it."""
        fields = {k: job[k] for k in SIGNED_FIELDS}
        if verify_manifest(fields, job["signature"], signer.public_key_hex):
            return job
        for old_key in retired:
            if verify_manifest(fields, job["signature"], old_key):
                new_signature = signer.sign(fields)
                ledger.replace_signature(job["job_id"], job["signature"], new_signature, now(),
                                         f"{fingerprint(old_key)} -> {signer.fingerprint}")
                log.info("job re-signed after key rotation",
                         extra={"job_id": job["job_id"], "old_fingerprint": fingerprint(old_key),
                                "fingerprint": signer.fingerprint})
                return job | {"signature": new_signature}
        log.warning("job signature matches no coordinator key", extra={"job_id": job["job_id"]})
        return job

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
        if not token:
            log.warning("operator auth refused", extra={"reason": "operator token required"})
            raise HTTPException(status_code=403, detail="operator token required")
        if not hmac.compare_digest(token.encode(), expected.encode()):
            log.warning("operator auth refused", extra={"reason": "wrong operator token"})
            raise AuthRefused(status_code=403, detail="operator token required")

    def require_job_creator(operator: str | None, submitter: str | None) -> str:
        """POST /v1/jobs: the operator token or the submitter token. Returns which one.
        Without this anyone who can reach the coordinator could queue a job with up to
        1000 points and then claim it with their own rig (minting simulated points)."""
        if not settings.operator_token and not settings.submitter_token:
            log.warning("job creation refused", extra={"reason": "job creation disabled"})
            raise HTTPException(status_code=403, detail="job creation is disabled "
                                "(set OPERATOR_TOKEN or SUBMITTER_TOKEN)")
        if not operator and not submitter:
            log.warning("job creation refused", extra={"reason": "token required"})
            raise HTTPException(status_code=403, detail="X-Submitter-Token or "
                                "X-Operator-Token required to queue jobs")
        for sent, expected, role in ((operator, settings.operator_token, "operator"),
                                     (submitter, settings.submitter_token, "submitter")):
            if sent and expected and hmac.compare_digest(sent.encode(), expected.encode()):
                return role
        log.warning("job creation refused", extra={"reason": "wrong token"})
        raise AuthRefused(status_code=403, detail="X-Submitter-Token or X-Operator-Token "
                          "required to queue jobs")

    def authenticate_worker(authorization: str | None, claimed_id: str | None) -> str:
        """Return the worker id that owns the bearer key, or raise 401/403.
        If the request also names a worker id, it must be the key's own worker."""
        def refuse(code: int, reason: str, worker_id: str | None = None,
                   counted: bool = True) -> HTTPException:
            # Never log the key itself: only why it was refused and whose it was.
            log.warning("worker auth refused", extra={"reason": reason, "worker_id": worker_id,
                                                      "claimed_worker_id": claimed_id})
            headers = {"WWW-Authenticate": "Bearer"} if code == 401 else None
            # A wrong, revoked or someone else's key counts toward the lock-out; no key at
            # all is a setup mistake and doesn't.
            kind = AuthRefused if counted else HTTPException
            return kind(status_code=code, detail=reason, headers=headers)

        scheme, _, key = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            raise refuse(401, "worker key required", counted=False)
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
            counts = batched_counts.view(now())
            pause = ledger.pause_state()
        except Exception as exc:  # noqa: BLE001 - any database failure means "down"
            # The error text can contain file paths: log its type, don't return it.
            log.error("health check failed", extra={"check": "database",
                                                    "error_type": type(exc).__name__})
            return JSONResponse(status_code=503, content=body | {
                "status": "down", "checks": {"database": "error", "signing_key": "loaded"}})
        # `pause-v1`: public, so rigs and a status page can tell "paused" from "down".
        # Still 200: results for jobs leased before the pause are accepted. No reason here.
        return body | {"status": "paused" if pause else "ok", "accepting_work": pause is None,
                       "paused_since": pause["since"] if pause else None, **counts,
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
        pause = ledger.pause_state()
        if pause:
            warnings.append(f"coordinator PAUSED since {pause['since']} ({t - pause['since']} s, "
                            f"by {pause['by']}): no jobs handed out or created, no new rigs. "
                            f"Reason: {pause['reason']}. Resume on the server with "
                            "`python -m app.incident resume`")
        if snap["workers"]["active"] == 0:
            warnings.append(f"no active workers in the last {settings.auditor_active_seconds} "
                            "seconds")
        waiting = snap["audits"]["waiting_for_auditor"]
        if waiting:
            warnings.append(f"{waiting} audit/tie-breaker job(s) waiting for an eligible "
                            "auditor: needs an active worker with a different owner")
        aimed_waiting = snap["aimed_tests"]["waiting_for_rig"]
        if aimed_waiting:
            warnings.append(f"{aimed_waiting} aimed hidden test(s) waiting for a rig: no "
                            "active rig in that trust tier, or the named rig is revoked")
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
            if settings.auditor_pick == "uniform":
                # Threat model #4/#6: a caught rig still has the same chance to audit
                # others, or to be the deciding vote in a tie-breaker.
                warnings.append("rigs with a strike can still be picked to audit others or "
                                "break ties (AUDITOR_PICK=uniform); AUDITOR_PICK=trusted-first "
                                "uses them only when nobody else is eligible")
        # Threat model #4 follow-up: a rig that keeps letting its audits go.
        follow = ledger.audit_follow_through()
        droppers = sorted(w for w, f in follow.items() if f["dropped"] >= settings.audit_drop_warn)
        if droppers:
            warnings.append(f"{len(droppers)} worker(s) dropped {settings.audit_drop_warn} or more "
                            f"assigned audit/tie-breaker job(s): {', '.join(droppers)}. Often just "
                            "a rig that went offline, but a rig skipping checks it doesn't like "
                            "looks the same; see audit_follow_through in /v1/workers/<id>/trust "
                            "(rigs busy with older jobs are not counted: not_reached)")
        # Threat model #7: held results the software can't settle on its own.
        queue = review.review_queue(ledger, t)
        if queue["counts"]["needs_review"]:
            warnings.append(f"{queue['counts']['needs_review']} held case(s) need a person "
                            "(stuck dispute, three different answers, or a failed hidden "
                            "test): see /v1/operator/review")
        # Threat model #3 (`vouch-check-v1`): rigs that agreed with a rig later caught and
        # that nothing has checked independently yet.
        vouch = vouches.vouch_report(ledger, t)
        if vouch["unchecked_leads"]:
            warnings.append(f"{len(vouch['unchecked_leads'])} rig(s) agreed with a rig that "
                            f"was later caught and never passed a hidden test of their own: "
                            f"{', '.join(vouch['unchecked_leads'])}. Could be honest, could "
                            "be a friend confirming fakes; aim a hidden test at each "
                            "(target_worker). See /v1/operator/vouches")
        # app/rate_limit.py: someone keeps sending wrong keys/tokens/codes.
        limits = limiter.snapshot() | {"proxies": resolver.snapshot()}
        warnings.extend(resolver.warnings())
        if limits["locked_out_clients"]:
            warnings.append(f"{len(limits['locked_out_clients'])} client address(es) locked out "
                            "after repeated wrong keys, tokens or enrollment codes: see "
                            "rate_limits. Could be a misconfigured rig or someone guessing")
        # Threat model #16 follow-up: a submitter token without a points table can queue
        # jobs worth up to 1000 points for a friend's rig to claim.
        if settings.submitter_token and not settings.points_table:
            warnings.append("SUBMITTER_TOKEN is set but POINTS_TABLE isn't, so whoever holds "
                            f"the submitter token picks each job's points (up to "
                            f"{points.MAX_POINTS}); set POINTS_TABLE to fix points per task type")
        # `test-reuse-v2`: hidden tests that share an input (or a near copy of one) with
        # another hidden test.
        reuse = ledger.test_reuse()
        if reuse["open_sharing_input"]:
            warnings.append(f"{reuse['open_sharing_input']} open hidden test(s) use the same "
                            "input as another hidden test (ignoring case, spacing, "
                            "punctuation and settings like the seed); rigs that compare "
                            "notes could spot them. Prefer a fresh input for each test")
        if settings.public_settle_delay_seconds == 0:
            warnings.append("PUBLIC_SETTLE_DELAY_SECONDS is 0, so the public ledger shows "
                            "each result's bucket the moment it is accepted: a rig can tell "
                            "its hidden tests from normal work (threat model #14). Set it "
                            "longer than an audit and tie-breaker take, e.g. 3600.")
        if settings.public_counts_interval_seconds == 0:
            warnings.append("PUBLIC_COUNTS_INTERVAL_SECONDS is 0, so the public job counts "
                            "are live: right after a rig submits, `queued` going up by one "
                            "tells it that result is being re-checked (threat model #14). "
                            "Set it to batch the counts, e.g. 900.")
        if problems:
            warnings.append(f"{len(problems)} consistency problem(s): see "
                            "consistency_problems")
        return {"status": "attention" if warnings else "ok", "checked_at": t,
                "uptime_seconds": t - started_at, "signing_key_fingerprint": signer.fingerprint,
                "signing_key_rotations": len(rotations),
                "handover_key": {"rule": key_rotation.HANDOVER_RULE,
                                 "fingerprint": fingerprint(handover_key)} if handover_key
                else None,
                **snap, "audit_dropouts": {w: f["dropped"] for w, f in follow.items()
                                           if f["dropped"]},
                "review": queue["counts"] | {"points_held": queue["points_held"]},
                "vouches": {"rule": vouches.VOUCH_RULE,
                            "caught_rigs": len(vouch["caught_rigs"]),
                            "leads": len(vouch["leads"]),
                            "unchecked_leads": vouch["unchecked_leads"],
                            "weak_confirmations": vouch["weak_confirmations"]},
                "rate_limits": limits, "points": points.describe(settings.points_table),
                "auditor_pick": settings.auditor_pick,
                "tiebreak_decides": settings.tiebreak_decides, "hidden_test_reuse": reuse,
                "reward_scoring": settings.reward_scoring,
                "public_settle_delay_seconds": settings.public_settle_delay_seconds,
                "public_counts_interval_seconds": settings.public_counts_interval_seconds,
                "pause": {"rule": PAUSE_VERSION, "paused": pause is not None} | (pause or {}),
                "consistency_problems": problems, "warnings": warnings}

    @app.get("/v1/operator/review")
    def operator_review(needs_person_only: bool = False,
                        x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only review queue (threat model #7): every case with points held, the
        answers side by side, and whether a person needs to look. Read-only: it shows
        answers and hidden tests' expected answers, so it never goes in a log line."""
        require_operator(x_operator_token)
        queue = review.review_queue(ledger, now())
        if needs_person_only:
            queue["cases"] = [c for c in queue["cases"] if c["needs_person"]]
        return queue

    @app.get("/v1/operator/vouches")
    def operator_vouches(x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only vouch check (`vouch-check-v1`, threat model #3): rigs whose answer
        agreed with a rig that was later caught, and confirmations that rest only on a
        caught rig. Read-only; no prompts or answers, but it names who audited whom."""
        require_operator(x_operator_token)
        return vouches.vouch_report(ledger, now())

    @app.post("/v1/operator/pause")
    def operator_pause(body: PauseRequest,
                       x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only emergency stop (`pause-v1`, app/incident.py): stop handing out and
        creating work and enrolling rigs, at once, across restarts. There is deliberately
        no resume endpoint: resuming needs `python -m app.incident resume` on the server,
        so a leaked operator token can stop the coordinator but can't restart it."""
        require_operator(x_operator_token)
        try:
            newly, since = ledger.pause(now(), body.reason, by="api")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # The reason can describe the incident: never logged.
        log.warning("coordinator paused", extra={"by": "api", "already_paused": not newly})
        return {"status": "paused", "since": since, "already_paused": not newly,
                "resume": "on the server: python -m app.incident resume --note '...'"}

    @app.get("/v1/coordinator/key")
    def coordinator_key() -> dict[str, Any]:
        """Public: the key workers use to check job signatures. Safe to publish.
        Workers should pin it once (compare the fingerprint with the operator) and refuse
        any later change, so a fake coordinator can't just serve its own key."""
        return {"algorithm": "ed25519", "public_key": signer.public_key_hex,
                "fingerprint": signer.fingerprint, "rotations": rotations,
                "handover_key": handover_announcement}

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
            raise AuthRefused(status_code=403,
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
    def create_job(body: JobCreate,
                   x_operator_token: str | None = Header(default=None),
                   x_submitter_token: str | None = Header(default=None)) -> JobEnvelope:
        require_job_creator(x_operator_token, x_submitter_token)
        reward_points = job_points(body.task_type, body.reward_points)
        model_sha256 = validated_model_hash(body.task_type, body.payload)
        t = now()
        fields, signature = new_job_fields(body.task_type, body.payload, reward_points,
                                           t + body.ttl_seconds, model_sha256)
        ledger.create_job(fields, signature, settings.max_attempts, t)
        log.info("job created", extra={"job_id": fields["job_id"], "task_type": body.task_type})
        return JobEnvelope(**fields, signature=signature)

    @app.post("/v1/challenges")
    def create_challenge(body: ChallengeCreate,
                         x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Operator-only: queue a hidden known-answer job. Workers can't tell it apart.
        `target_worker` / `target_tier` aim it at one rig or a trust tier (see models)."""
        require_operator(x_operator_token)
        if body.target_worker is not None:
            aimed = ledger.get_worker(body.target_worker)
            if aimed is None:
                raise HTTPException(status_code=404, detail="no such worker")
            if aimed.get("revoked_at") is not None:
                raise HTTPException(status_code=409, detail="that worker is revoked")
        # Same points as normal work of this type, so a test can't be spotted by them.
        reward_points = job_points(body.task_type, body.reward_points)
        model_sha256 = validated_model_hash(body.task_type, body.payload)
        if not tasks.get(body.task_type).verify(body.payload, body.expected_result).ok:
            raise HTTPException(status_code=422,
                                detail="expected_result fails this task's own verifier")
        t = now()
        fields, signature = new_job_fields(body.task_type, body.payload, reward_points,
                                           t + body.ttl_seconds, model_sha256)
        try:
            ledger.create_job(fields, signature, settings.max_attempts, t, job_kind="challenge",
                              expected_result=body.expected_result, target=body.target())
        except HiddenTestRevealed as exc:
            # `test-reuse-v2`: the rigs that ran this input (or a near copy) before can tell
            # it's a test. Says how it matched, never which old test or what it contained.
            what = ("this exact input" if exc.match == "exact" else
                    "this input differs only in case, spacing, punctuation or settings "
                    "(seed, max_tokens, timeout, model) from one that")
            raise HTTPException(status_code=409, detail=(
                f"{what} was already a hidden test in a period a ledger copy covers "
                f"(up to {exc.revealed_until}), so the rigs that ran it can recognise it; "
                "make a new hidden test with a different question")) from None
        # Same line as a normal job: logs never say which jobs are hidden tests.
        log.info("job created", extra={"job_id": fields["job_id"], "task_type": body.task_type})
        job = ledger.get_job(fields["job_id"]) or {}
        return {"job_id": fields["job_id"], "status": "queued", "job_kind": "challenge",
                "target": body.target(), "assigned_worker": job.get("assigned_worker")}

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
            report = rewards.build_report(ledger, since, until if until is not None else t + 1,
                                          settings.reward_scoring)
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
        return envelope(current_signature(job))

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
    def read_ledger(x_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        """Public ledger. `public-delay-v1` (threat model #14): a result's settlement bucket
        and confidence show only PUBLIC_SETTLE_DELAY_SECONDS after it was accepted; until
        then it counts as "pending". `public-no-hashes-v1` (threat model #14 follow-up):
        public entries never show `result_hash`, because a re-check that agrees has the
        same hash as the job it re-checked. `public-counts-v1` (threat model #14): public job
        counts are taken once per PUBLIC_COUNTS_INTERVAL_SECONDS window, because live
        `queued` rose the instant a result was picked for a re-check. With the operator
        token the view is live and complete."""
        operator = x_operator_token is not None
        if operator:
            require_operator(x_operator_token)
            delay, view = 0, "operator-live"
        else:
            delay, view = settings.public_settle_delay_seconds, "public-delayed"
        t = now()
        cutoff = t - delay if delay else None
        counts = (public_counts.live(ledger.status_counts, t) if operator
                  else batched_counts.view(t))
        rules = [] if operator else ((["public-delay-v1"] if delay else [])
                                     + ["public-no-hashes-v1"]
                                     + ([public_counts.RULE]
                                        if counts["public_counts_interval_seconds"] else []))
        return {"entries": ledger.entries(cutoff, with_hashes=operator),
                "total_simulated_points": ledger.total_points(),
                "points_by_status": ledger.points_by_status(cutoff),
                "view": view, "public_settle_delay_seconds": delay,
                "public_rules": rules, "hidden_fields": [] if operator else ["result_hash"],
                **counts, "consistency_problems": ledger.check_consistency(),
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
