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

**Not done yet:** `llm:generate` has **never run** on project hardware: no llama.cpp build, no downloaded model and no RX 470 test yet (Phase 3). Inference results are only checked for shape (`schema-only`). Phase 4 re-run audits are needed before they mean anything.

To try inference on a GPU box later:

    python -m app.model_registry download qwen2.5-0.5b-instruct-q4_k_m ./models --yes-download
    python -m app.model_registry pin qwen2.5-0.5b-instruct-q4_k_m ./models/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf
    LLAMA_CLI=/path/to/llama-cli MODEL_DIR=./models python demo_worker.py

**Still missing (honest limits):** workers register without authentication, and anyone who can reach the API can create jobs. The coordinator is trusted, and the shared secret is symmetric, so a worker that knows it could forge manifests. Per-worker keys or asymmetric signatures are the fix. An inference adapter exists (Phase 2) but is untested on real hardware, and there is no RX 470 validation yet (Phase 3).

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

In another terminal:

    cd prototype
    . .venv/bin/activate
    python demo_worker.py --url http://127.0.0.1:8000

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

- `POST /v1/workers/register` — register a worker identifier.
- `POST /v1/jobs` — create a deterministic demo job.
- `POST /v1/jobs/next` — worker claims the next queued job.
- `POST /v1/jobs/{job_id}/submit` — submit a result.
- `GET /v1/ledger` — view simulated accepted-work entries.
- `GET /health` — local health check.

## GPU diagnostic

From the prototype directory:

    python -m app.gpu_probe_cli

This checks whether `vulkaninfo --summary` can run. Device enumeration is not proof that AI inference works. Run the Ubuntu hardware inventory commands in the main design document and test each GPU separately.

## Not implemented

- Persistent job queue and leases across API restarts.
- Worker-side manifest signature verification.
- Real model inference on RX 470.
- Independent task verifiers and audit sampling.
- Public deployment security, client authentication, TLS, rate limits, and abuse prevention.
- Ethereum ERC-20 integration or reward distribution.

Treat this as an engineering starter, not production infrastructure.
