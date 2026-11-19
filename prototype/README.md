# Boats N Hoes — Useful Compute MVP

This directory contains the first runnable, deliberately small software scaffold for the RX 470 useful-compute prototype. It is not a blockchain miner and does not use Ethereum consensus.

## Current implementation

- FastAPI coordinator with an in-memory job queue and worker registry.
- Signed job definitions using HMAC-SHA256 (prototype trust model: coordinator and worker share a secret).
- Worker registration, job acquisition, result submission, basic job/result validation, duplicate-submission rejection, and SQLite-backed result/reward ledger.
- A local CPU demo worker so the workflow can be exercised before GPU hardware support is validated.
- Optional inference runner interface; GPU execution is deliberately not enabled by default.
- Unit tests for job creation, worker lifecycle, idempotency and bad submissions.

## Safety and correctness boundaries

The HMAC signature authenticates the job envelope to a worker that knows the shared key. It does not prove that a GPU ran the model. Result hashes are integrity identifiers, not proofs of execution. The coordinator is a trusted central component in this MVP. Rewards are simulated ledger points only; no token minting, private-key handling, wallet signing, or on-chain transfer is implemented.

The reference repositories:
- https://github.com/MooreThreads/ethminer-musa
- https://github.com/simplechain-org/go-ethereum-pow

are research references only. Their PoW/consensus implementations are not integrated into this AI task execution MVP. The first is a GPU Ethash miner with vendor/backend-specific code; the second is an archived Go-Ethereum PoW fork. Neither provides verified AI inference for RX 470 by itself. Review their licenses and notices before any code reuse.

## Quick start (Ubuntu)

    cd prototype
    python3 -m venv .venv
    . .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env
    # Set a long random PROTOTYPE_SECRET in .env before use beyond localhost.
    uvicorn app.main:app --host 127.0.0.1 --port 8000

In a second terminal:

    cd prototype
    . .venv/bin/activate
    python demo_worker.py --url http://127.0.0.1:8000

Open http://127.0.0.1:8000/docs for the local API docs.

## Tests

    cd prototype
    . .venv/bin/activate
    pytest -q

## API outline

- `POST /v1/workers/register` — register a worker identifier.
- `POST /v1/jobs` — create a demo inference job.
- `POST /v1/jobs/next` — worker claims the next queued job.
- `POST /v1/jobs/{job_id}/submit` — submit a result and receipt.
- `GET /v1/ledger` — view simulated accepted-work entries.

## GPU path: intentionally gated

Before adding Vulkan inference, verify actual device IDs, driver binding and Vulkan visibility using:

    lsb_release -a
    uname -r
    lspci -nnk | grep -A3 -E 'VGA|Display'
    vulkaninfo --summary
    glxinfo -B
    ls -l /dev/dri/

Then run a standalone Vulkan smoke test on each GPU separately. RX 470 support in current official ROCm matrices is not assumed. Start with Mesa RADV/Vulkan only if the installed Linux stack exposes the devices successfully. Keep the 4 GB card to small tasks and benchmark the 8 GB card independently.

## Not implemented yet

- Actual model inference on RX 470.
- Durable queued-job leasing/expiry and multi-process concurrency guarantees.
- Public internet deployment, authentication hardening, rate limits, TLS, and abuse prevention.
- Independent task verifiers / audit sampling.
- Ethereum ERC-20 integration, reward distribution, and contracts.

Treat this as an engineering starter, not production infrastructure.
