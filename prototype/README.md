# Boats N Hoes — Useful Compute MVP

This directory contains an early local coordinator/worker scaffold for the RX 470 useful-compute prototype. It is not a blockchain miner and does not use Ethereum consensus.

## Implemented scaffold

- FastAPI coordinator with an in-memory job queue and in-memory job state.
- HMAC-SHA256 job manifest signing on the coordinator.
- Worker registration stored in SQLite.
- Result submissions and simulated reward points stored in SQLite.
- A local CPU demo worker that executes a deterministic SHA-256 task.
- A Vulkan device-enumeration diagnostic helper.
- Basic tests for worker registration, job claiming, deterministic result validation, and wrong-answer rejection.

**Important implementation limits:** jobs and leases are currently held in process memory and are lost when the API restarts. The demo worker currently does not independently verify the HMAC signature. The coordinator does not yet implement authenticated clients, durable job leasing, lease expiry/requeue, multi-process concurrency control, or a real AI inference adapter. These are explicit next tasks, not completed features.

## Safety and correctness boundaries

The HMAC signature authenticates a manifest only to a party that has the shared secret and actually verifies it. The current demo worker does not yet verify it. Result hashes are integrity identifiers, not proofs of execution. The coordinator is trusted in this MVP. Rewards are simulated ledger points only; no token minting, private-key handling, wallet signing, or on-chain transfer is implemented.

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
