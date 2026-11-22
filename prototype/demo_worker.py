"""Local demo worker: verifies each signed job manifest, runs the deterministic CPU task,
and submits a receipt. It refuses jobs whose signature, payload hash or deadline is wrong.

First run (the operator gives you a one-time code; see `python -m app.workers enroll`):
    PROTOTYPE_SECRET=... python demo_worker.py --worker-id rig-gpu0 --enrollment-code bnhe_...
Later runs reuse the saved key in ./data/worker-<id>.key:
    PROTOTYPE_SECRET=... python demo_worker.py --worker-id rig-gpu0 --once
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from app import tasks
from app.security import sha256_text, verify_manifest

SIGNED_FIELDS = ("job_id", "task_type", "payload", "payload_hash", "reward_points", "expires_at")


def supported_tasks() -> set[str]:
    """Task types this machine can actually run right now."""
    return {name for name, ex in tasks.EXECUTORS.items() if ex.available()[0]}


class ManifestError(Exception):
    """The job envelope failed verification; the worker must not execute it."""


def check_manifest(job: dict[str, Any], secret: str, now: float | None = None) -> None:
    missing = [k for k in (*SIGNED_FIELDS, "signature") if k not in job]
    if missing:
        raise ManifestError(f"missing fields: {', '.join(missing)}")
    fields = {k: job[k] for k in SIGNED_FIELDS}
    if not verify_manifest(fields, str(job["signature"]), secret):
        raise ManifestError("bad signature: job was not signed by this coordinator")
    if sha256_text(job["payload"]) != job["payload_hash"]:
        raise ManifestError("payload hash mismatch")
    if job["task_type"] not in supported_tasks():
        raise ManifestError(f"unsupported task type {job['task_type']!r}")
    if int(job["expires_at"]) <= int(now if now is not None else time.time()):
        raise ManifestError("job already expired")


def save_key(path: Path, key: str) -> None:
    """Write the key readable by this user only. Never overwrites an existing key file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")


def load_or_enroll(client: httpx.Client, worker_id: str, key_file: Path,
                   enrollment_code: str | None, capabilities: list[str]) -> str:
    """Return this worker's key. With a saved key, update capabilities using it.
    Without one, use the operator's one-time code to register and save the new key."""
    if key_file.exists():
        key = key_file.read_text().strip()
        response = client.post("/v1/workers/register", json={
            "worker_id": worker_id, "capabilities": capabilities},
            headers={"Authorization": f"Bearer {key}"})
        if response.status_code != 200:
            raise SystemExit(f"Saved key refused ({response.status_code}): "
                             f"{response.json().get('detail')}")
        return key
    if not enrollment_code:
        raise SystemExit(f"No key at {key_file}. Ask the operator for a one-time enrollment "
                         "code and pass --enrollment-code (or set BNH_ENROLLMENT_CODE).")
    response = client.post("/v1/workers/register", json={
        "worker_id": worker_id, "capabilities": capabilities,
        "enrollment_code": enrollment_code})
    if response.status_code != 200:
        raise SystemExit(f"Registration refused ({response.status_code}): "
                         f"{response.json().get('detail')}")
    key = str(response.json()["worker_key"])
    save_key(key_file, key)
    return key


def execute(job: dict[str, Any]) -> str:
    return tasks.get(job["task_type"]).execute(job["payload"])


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Local deterministic demo worker")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--worker-id", default="demo-worker-01")
    parser.add_argument("--once", action="store_true", help="claim at most one job")
    parser.add_argument("--enrollment-code", default=os.getenv("BNH_ENROLLMENT_CODE"),
                        help="one-time code from the operator (first run only)")
    parser.add_argument("--key-file", type=Path, default=None,
                        help="where this worker's key is kept (default ./data/worker-<id>.key)")
    args = parser.parse_args()
    key_file = args.key_file or Path("data") / f"worker-{args.worker_id}.key"
    secret = os.getenv("PROTOTYPE_SECRET", "")
    if len(secret) < 24:
        raise SystemExit("Set PROTOTYPE_SECRET (same value as the coordinator) to verify jobs.")

    with httpx.Client(base_url=args.url, timeout=10) as client:
        key = load_or_enroll(client, args.worker_id, key_file, args.enrollment_code,
                             sorted(supported_tasks()))
        client.headers["Authorization"] = f"Bearer {key}"
        while True:
            response = client.post("/v1/jobs/next", params={"worker_id": args.worker_id})
            if response.status_code == 404:
                print("No queued jobs.")
                return
            response.raise_for_status()
            job = response.json()
            try:
                check_manifest(job, secret)
            except ManifestError as exc:
                # Do not run it. The lease will time out and the coordinator will requeue it.
                print(f"Refused job {job.get('job_id')}: {exc}")
                if args.once:
                    return
                continue
            started = int(time.time())
            result = execute(job)
            completed = int(time.time())
            accepted = client.post(f"/v1/jobs/{job['job_id']}/submit", json={
                "worker_id": args.worker_id, "result": result, "result_hash": sha256_text(result),
                "started_at": started, "completed_at": completed,
            })
            print(accepted.status_code, accepted.json())
            if args.once:
                return


if __name__ == "__main__":
    main()
