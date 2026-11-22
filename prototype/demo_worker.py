"""Local demo worker: verifies each signed job manifest, runs the deterministic CPU task,
and submits a receipt. It refuses jobs whose signature, payload hash or deadline is wrong.

Usage:  PROTOTYPE_SECRET=... python demo_worker.py --once
"""
from __future__ import annotations

import argparse
import os
import time
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


def execute(job: dict[str, Any]) -> str:
    return tasks.get(job["task_type"]).execute(job["payload"])


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Local deterministic demo worker")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--worker-id", default="demo-worker-01")
    parser.add_argument("--once", action="store_true", help="claim at most one job")
    args = parser.parse_args()
    secret = os.getenv("PROTOTYPE_SECRET", "")
    if len(secret) < 24:
        raise SystemExit("Set PROTOTYPE_SECRET (same value as the coordinator) to verify jobs.")

    with httpx.Client(base_url=args.url, timeout=10) as client:
        client.post("/v1/workers/register", json={
            "worker_id": args.worker_id, "capabilities": sorted(supported_tasks())
        }).raise_for_status()
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
