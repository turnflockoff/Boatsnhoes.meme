# Boats N Hoes — Useful Compute MVP

This directory contains an early local coordinator/worker scaffold for the RX 470 useful-compute prototype. It is not a blockchain miner and does not use Ethereum consensus.

## Implemented (Phase 1: durable local MVP)

- FastAPI coordinator built by `create_app()`; all job state lives in SQLite (WAL mode), so jobs and leases **survive a coordinator restart**.
- Explicit job state machine: `queued → leased → accepted`, with `rejected`, `expired` and `failed` final states. Every transition is written to an `events` audit table.
- **Atomic claiming** with `BEGIN IMMEDIATE` transactions: two workers, or two coordinator processes on the same database, can't lease the same job.
- **Leases** (`LEASE_SECONDS`, default 60): if a worker goes silent, its job is requeued. After `MAX_ATTEMPTS` (default 3) the job is marked `failed`. Wrong answers requeue the job until attempts run out, then it's `rejected`.
- **Idempotent submissions**: resubmitting the same accepted result returns the original receipt and is paid only once. A different worker submitting after acceptance gets `409`. Late submissions from a worker that lost its lease get `403`. Expired jobs get `410`.
- **The worker verifies the coordinator's HMAC-SHA256 manifest signature**, the payload hash, the task type and the deadline before it executes anything. A job signed with a different secret is refused.
- `GET /v1/ledger` includes job counts and `consistency_problems`, an invariant check an auditor can run at any time. `GET /v1/jobs/{id}` shows a job's status and audit trail.
- A local CPU demo worker that runs a deterministic SHA-256 task (a test fixture, not useful work).
- Vulkan device-enumeration diagnostic helper.
- 13 tests: restart persistence, lease expiry and requeue, retry exhaustion, job expiry, wrong worker, duplicate and conflicting submissions, malformed results, 20-thread concurrent claims, tampered or foreign manifests, and secrets kept out of API responses.

## Implemented (Phase 2: task interface + pinned models)

- **Pluggable task executors** (`app/tasks.py`). Every job type implements `validate`, `available`, `execute` and `verify`. The coordinator validates payloads when a job is created, and workers only advertise task types they can actually run.
- **Verdicts state how much they prove.** `exact` means the coordinator recomputed or fully checked the answer (`demo:sha256`). `schema-only` means the result is well-formed but unproven (`llm:generate`). Receipts and the ledger record `verifier_method`, `confidence` and `needs_audit`.
- **`llm:generate` task** for small text generation via llama.cpp: greedy decoding (temperature 0), a fixed seed, `max_tokens` capped at 256, and a 5–300 s timeout. The worker re-hashes the model file before every run.
- **Pinned model registry** (`models/registry.json`, `app/model_registry.py`) records source URL, upstream, license, approximate size and minimum VRAM. A model can't be used until its sha256 and size are **pinned from a file you downloaded**. The first candidate, Qwen2.5-0.5B-Instruct Q4_K_M (Apache-2.0, ~400 MB), ships **unpinned**.
- **Opt-in download only:** `python -m app.model_registry download <id> <dir> --yes-download`. Model weights are git-ignored.
- Older Phase 1 databases are migrated in place.
- 32 tests in total.

## Implemented (Phase 4: audits, tie-breakers and challenges)

A result hash is not proof that a GPU ran a model. Phase 4 adds the ways the coordinator gathers evidence anyway. Every check is tested in software only; none has run against real GPU output yet.

- **Hidden re-run audits.** When an unproven (`schema-only`) result is accepted, the coordinator re-queues the same job for a *different* worker at rate `AUDIT_RATE` (default 0.25, chosen with the OS's secure randomness). The original worker can never claim its own audit. The audit job is created in the same database transaction as the acceptance, and it gets the same time window as the original, so its deadline doesn't give it away.
- **Tolerance rules** (`results_agree` on each task): SHA must match byte for byte; AI text must match after collapsing whitespace. Anything else (one word, letter case) is a disagreement.
- **Outcomes.** Agreement marks both results `confirmed` with confidence `replicated`. Disagreement marks both `disputed` and their points are held. An audit that never runs (expires, fails, or is rejected) leaves the original `unaudited`. Audits never spawn audits.
- **Hidden tie-breakers** (`TIEBREAK`, default on). When an audit disagrees, the coordinator queues the same job for a *third* worker that is neither side of the dispute, in the same transaction and with the same time window. Majority of three: if the tie-breaker agrees with exactly one side, that side and the tie-breaker become `confirmed` and the other side becomes `outvoted` (points stay held). If it agrees with neither (or both), all three are `unresolved` and held for a person. If no third worker picks it up, the dispute stays held and a `tiebreak_not_run` event is recorded. Tie-breakers never spawn more audits or tie-breakers.
- **Hidden challenge jobs.** The operator queues a known-answer job with `POST /v1/challenges`. Workers get the same envelope, the same receipt and the same audit-trail events as normal work. A wrong (but well-formed) answer is accepted on the surface, so a cheater can't tell they were caught, and recorded as `failed-challenge` with points held.
- **Operator endpoints** (`/v1/challenges`, `/v1/audits`, `/v1/workers/{id}/trust`) require the `X-Operator-Token` header to match `OPERATOR_TOKEN`. If the token isn't set, they are disabled.
- **Public ledger** (`GET /v1/ledger`) adds `points_by_status`: confirmed, pending, unverified, held. Challenge outcomes are folded into confirmed/held so the public view doesn't reveal which jobs were challenges. The public job view hides audit and challenge events.
- New consistency checks: a pending audit with no open audit job, a job leased to an excluded worker, a worker auditing its own result, a worker breaking a tie on its own dispute, and an outvoted/unresolved result with no tie-breaker behind it.
- 19 tests for Phase 4 (51 in total), including an in-place migration of a Phase 2 database and a tampered-database check.
- **Written threat model:** [`docs/VERIFICATION-THREAT-MODEL.md`](../docs/VERIFICATION-THREAT-MODEL.md) lists each attack, today's defense, what is still open, and the order of fixes.

## Implemented (Phase 5: simulated reward reports)

Off-chain points only. Nothing is minted, sent or promised.

- **Versioned scoring rule** `sim-points-v1` (`app/rewards.py`), integer math only. Each accepted result sits in one public bucket: `confirmed` and `unverified` are payable; `pending` (a hidden re-run is still queued) and `held` (disputed, outvoted, unresolved, or a failed hidden test) are not. Unverified work counts because spot-checking only works if unsampled honest work is paid. v1 has no extra penalty for a caught cheater (open decision).
- **Per-worker report** for a period (`since <= accepted_at < until`): accepted results, points per bucket, payable points, and activity (leases, rejected answers, lease timeouts, bad submissions).
- **Auditable and reproducible.** The report carries `report_sha256` over its canonical JSON. The same ledger and the same period always give the same hash. `python -m app.rewards verify report.json --db <db>` re-runs it and lists any entry that differs, and catches an edited report file.
- **Self-checks** in every report: the ledger's consistency problems, and whether per-worker sums match the totals.
- **Never leaks hidden checks.** Reports list job ids, buckets, verifier method and confidence only: no job kinds, results, payloads, expected answers or secrets.
- **Operator-only** (`GET /v1/rewards/report`, needs `X-Operator-Token`). A live per-job view would let a worker see a "held" right after submitting and guess it was a hidden test. Publish a report only after its period has closed.
- Statuses are read when the report is built, so a pending re-run that settles later changes the report for the same period; `verify` says so.
- 10 tests for Phase 5 (61 in total). Also run end to end with a real coordinator process, two demo workers, the HTTP endpoint and the CLI `report`/`verify`.

Build and check a report:

    python -m app.rewards report --db ./data/prototype.sqlite3 --since 0 --until 1800000000 --out report.json
    python -m app.rewards verify report.json --db ./data/prototype.sqlite3

**Fraud risks that remain (be honest about these):**
- **Collusion.** Two workers run by the same person can agree on a wrong answer. Agreement is evidence, not proof. Workers now need an operator code and a per-worker key, and same-owner workers never audit each other, but a person who gets codes under several names is still a ring. In a tie-breaker, two colluders can even outvote an honest auditor.
- **First-come auditors.** An audit goes to whichever eligible worker asks next, so a ring that polls quickly gets more audits than chance would give it.
- **Repeated payloads.** An audit has the same payload as the original. Colluding workers who compare notes could spot it.
- **Trusted coordinator.** The coordinator picks auditors, stores expected answers, and could lie about all of it.
- **Real GPU nondeterminism is unknown.** The whitespace-only tolerance is a guess until two real rigs produce output for the same job. It may need loosening (or a different check) after Phase 3.
- **Two rigs aren't enough for a tie-breaker.** With only two workers online, a dispute stays held until a person looks at it.

**Not done yet:** `llm:generate` has **never run** on project hardware: no llama.cpp build, no downloaded model and no RX 470 test yet (Phase 3). Inference results are only checked for shape (`schema-only`). Phase 4 re-run audits are needed before they mean anything.

To try inference on a GPU box later:

    python -m app.model_registry download qwen2.5-0.5b-instruct-q4_k_m ./models --yes-download
    python -m app.model_registry pin qwen2.5-0.5b-instruct-q4_k_m ./models/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf
    LLAMA_CLI=/path/to/llama-cli MODEL_DIR=./models python demo_worker.py

**Still missing (honest limits):** anyone who can reach the API can create normal jobs, and owner labels depend on the operator handing out codes carefully. The coordinator is trusted, and the shared secret is symmetric, so a worker that knows it could forge manifests. Per-worker keys or asymmetric signatures are the fix. An inference adapter exists (Phase 2) but is untested on real hardware, and there is no RX 470 validation yet (Phase 3).

## Implemented: per-worker keys and authenticated registration

Closes threat-model item #2 *as far as the operator hands out codes carefully*. Tested in software only.

- **One-time enrollment codes.** The operator makes a code tied to an `owner` label (the person or group running the rig) with `POST /v1/enrollments` (needs `X-Operator-Token`) or `python -m app.workers enroll --owner <label>`. A code works once and expires (default 24 h, at most 7 days).
- **Per-worker keys.** A rig sends the code once to `POST /v1/workers/register` and gets its own random key back (`bnhw_…`, 256 bits). The key is shown once; the coordinator stores only its sha256, and never stores codes in plain text either.
- **Every claim and submit needs the key** (`Authorization: Bearer <key>`). The worker id comes from the key. Naming a different worker id gets `403`, a missing or unknown key gets `401`. One rig can't claim or submit as another.
- **No take-overs.** A code can't be used to grab a worker id that already has a key (`409`, and the code is not used up). An existing worker changes its capabilities by registering again with its own key, and gets no new key.
- **Revocation.** `POST /v1/workers/{id}/revoke` (operator) or `python -m app.workers revoke <id>` blocks the key at once and puts any job the worker is holding back in the queue. Its past results stay for the operator to review. A revoked worker id can't re-enroll.
- **One owner never checks itself.** Hidden re-runs and tie-breakers are closed to every worker with the same owner as the worker(s) they exclude, not just the same worker id. A new consistency check flags any job given to a same-owner worker.
- `python -m app.workers list` shows each worker's owner and whether it is active, revoked, or keyless. The trust report shows `owner` and `revoked`. Keys never appear in any API response, listing or log.
- Older databases are migrated in place. Workers registered before this change have no key and must enroll with a code (same worker id is fine).
- The demo worker saves its key to `./data/worker-<id>.key` (owner-only file permissions, git-ignored) on the first run and reuses it after.
- 12 new tests (73 in total). Also run end to end with a real coordinator process: one code from the API, one from the CLI, two demo workers, a reused code refused, a saved key reused, and a revoked worker refused.

**What this does not fix:** owner labels are only as honest as the operator's hand-out. One person who talks the operator into several codes under different names is still a Sybil ring. There is no proof of personhood, no rate limit on the API, and creating normal jobs (`POST /v1/jobs`) still needs no login.

**Your two RX 470s:** if both cards get the same owner label, they will never audit each other, so with only those two rigs online no audit ever runs. For a two-card test, enroll them under two labels (for example `owner-gpu0` and `owner-gpu1`) and remember that agreement between two cards you own proves less than agreement between strangers.

## Safety and correctness boundaries

The HMAC signature authenticates a manifest only to a party that has the shared secret and actually verifies it. The demo worker verifies it before executing. Result hashes are integrity identifiers, not proofs of execution. The coordinator is trusted in this MVP. Rewards are simulated ledger points only; no token minting, private-key handling, wallet signing, or on-chain transfer is implemented.

The reference repositories:
- https://github.com/MooreThreads/ethminer-musa
- https://github.com/simplechain-org/go-ethereum-pow

are research references only. Their PoW/consensus implementations are not integrated into this AI task execution MVP. The first is an Ethash GPU miner with vendor/backend-specific code; the second is an archived Go-Ethereum PoW fork. Neither provides verified AI inference for RX 470 by itself. Review exact licenses and notices before any code reuse.

## Quick start (Ubuntu)

    cd prototype
    python3 -m venv .venv
    . .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env
    # Set PROTOTYPE_SECRET to a long random value.
    uvicorn app.main:app --host 127.0.0.1 --port 8000

In another terminal, make a one-time code for the rig and start the worker with it:

    cd prototype
    . .venv/bin/activate
    CODE=$(python -m app.workers enroll --owner owner-gpu0)
    python demo_worker.py --url http://127.0.0.1:8000 --worker-id rig-gpu0 --enrollment-code "$CODE"

Later runs reuse the saved key, so the code isn't needed again:

    python demo_worker.py --url http://127.0.0.1:8000 --worker-id rig-gpu0

Create a demo job using the API docs at http://127.0.0.1:8000/docs:
- POST /v1/jobs with {"payload":"hello from Boats N Hoes","task_type":"demo:sha256"}.
- The demo worker claims and processes queued jobs.
- GET /v1/ledger shows simulated points.

## Tests

    cd prototype
    . .venv/bin/activate
    pytest -q

A GitHub Actions workflow runs these tests on pushes and pull requests affecting prototype files.

## API outline

- `POST /v1/enrollments` — operator only: make a one-time enrollment code for an owner.
- `POST /v1/workers/register` — new worker: send the enrollment code, get a key once. Existing worker: send its key to update capabilities.
- `POST /v1/workers/{worker_id}/revoke` — operator only: block a worker's key and requeue its job.
- `POST /v1/jobs` — create a deterministic demo job.
- `POST /v1/jobs/next` — worker claims the next queued job (needs its key).
- `POST /v1/jobs/{job_id}/submit` — submit a result (needs its key).
- `GET /v1/ledger` — view simulated accepted-work entries and points by status.
- `GET /v1/jobs/{job_id}` — job status and its public audit trail.
- `POST /v1/challenges` — operator only: queue a hidden known-answer job.
- `GET /v1/audits` — operator only: every audit and challenge with its outcome.
- `GET /v1/workers/{worker_id}/trust` — operator only: how a worker's results held up.
- `GET /v1/rewards/report?since=&until=` — operator only: simulated reward report for a period.
- `GET /health` — local health check.

## GPU diagnostic

From the prototype directory:

    python -m app.gpu_probe_cli

This checks whether `vulkaninfo --summary` can run. Device enumeration is not proof that AI inference works. Run the Ubuntu hardware inventory commands in the main design document and test each GPU separately.

## Not implemented

- Real model inference on RX 470.
- Random (rather than first-come) auditor assignment.
- Public deployment security, TLS, rate limits, and abuse prevention. Workers authenticate with per-worker keys; job creation does not require a login yet.
- Ethereum ERC-20 integration or reward distribution.

Treat this as an engineering starter, not production infrastructure.
