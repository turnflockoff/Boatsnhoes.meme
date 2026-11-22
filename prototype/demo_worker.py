"""Local demo worker: verifies each signed job manifest, runs the deterministic CPU task,
and submits a versioned receipt. It refuses jobs whose signature, schema version, payload
hash, model pin or deadline is wrong.

Workers hold no shared secret. They check signatures with the coordinator's PUBLIC key,
pinned on the first run (or given up front with --coordinator-key / BNH_COORDINATOR_KEY).

First run (the operator gives you a one-time code; see `python -m app.workers enroll`):
    python demo_worker.py --worker-id rig-gpu0 --enrollment-code bnhe_...
Later runs reuse the saved worker key in ./data/worker-<id>.key and the pinned coordinator
key in ./data/coordinator.pub:
    python demo_worker.py --worker-id rig-gpu0 --once
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from app import schemas, tasks
from app.key_rotation import RotationError, follow_chain
from app.security import sha256_text
from app.signing import fingerprint, parse_public_key, verify_manifest

SIGNED_FIELDS = schemas.SIGNED_FIELDS


def supported_tasks() -> set[str]:
    """Task types this machine can actually run right now."""
    return {name for name, ex in tasks.EXECUTORS.items() if ex.available()[0]}


class ManifestError(Exception):
    """The job envelope failed verification; the worker must not execute it."""


def check_manifest(job: dict[str, Any], coordinator_key: str, now: float | None = None) -> None:
    """Raise ManifestError unless the job is signed by `coordinator_key` (hex Ed25519 public
    key), uses a manifest version this worker understands, its payload matches its hash,
    this machine can run it with the same pinned model the job names, and it hasn't expired."""
    if job.get("schema_version") != schemas.JOB_SCHEMA_VERSION:
        raise ManifestError(f"unsupported job schema_version {job.get('schema_version')!r}; "
                            f"this worker understands {schemas.JOB_SCHEMA_VERSION}")
    missing = [k for k in (*SIGNED_FIELDS, "signature") if k not in job]
    if missing:
        raise ManifestError(f"missing fields: {', '.join(missing)}")
    fields = {k: job[k] for k in SIGNED_FIELDS}
    if not verify_manifest(fields, str(job["signature"]), coordinator_key):
        raise ManifestError("bad signature: job was not signed by this coordinator")
    if sha256_text(job["payload"]) != job["payload_hash"]:
        raise ManifestError("payload hash mismatch")
    if job["task_type"] not in supported_tasks():
        raise ManifestError(f"unsupported task type {job['task_type']!r}")
    local = local_model_sha256(job)
    if local != job["model_sha256"]:
        raise ManifestError(f"model pin mismatch: job wants {job['model_sha256']}, this "
                            f"worker's registry has {local}")
    if int(job["expires_at"]) <= int(now if now is not None else time.time()):
        raise ManifestError("job already expired")


def local_model_sha256(job: dict[str, Any]) -> str | None:
    """The pinned model hash *this worker's* registry gives for the job (None if no model).
    The executor re-hashes the model file against the same pin before every run."""
    try:
        return tasks.get(job["task_type"]).model_sha256(job["payload"])
    except tasks.TaskError as exc:
        raise ManifestError(f"can't run this job here: {exc}") from exc


def save_key(path: Path, key: str) -> None:
    """Write the key readable by this user only. Never overwrites an existing key file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key + "\n")


def replace_key_file(path: Path, key: str) -> None:
    """Swap the pinned key in one step: write a private temporary file, then rename it."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    save_key(tmp, key)
    os.replace(tmp, path)


def load_or_pin_coordinator_key(client: httpx.Client, pin_file: Path,
                                given: str | None = None) -> str:
    """Return the coordinator public key this worker trusts.

    - `given` (from the operator, out of band) is the strongest option: used as-is.
    - Otherwise a key pinned on an earlier run is used. If the coordinator now serves a
      different key, the worker only moves to it when the coordinator also shows a chain of
      handovers from the pinned key to the new one, each signed by both keys (a planned
      rotation, see app/key_rotation.py). Anything else and the worker stops: a person has
      to decide whether this is still the real coordinator.
    - First run with neither: trust on first use. Fetch, print the fingerprint so the owner
      can compare it with the operator's, and save it (readable by this user only).
    """
    if given:
        try:
            parse_public_key(given)
        except ValueError as exc:
            raise SystemExit(f"Bad coordinator key: {exc}") from exc
        return given.strip().lower()
    response = client.get("/v1/coordinator/key")
    if response.status_code != 200:
        raise SystemExit(f"Could not fetch the coordinator key ({response.status_code}).")
    served = str(response.json().get("public_key", "")).strip().lower()
    try:
        parse_public_key(served)
    except ValueError as exc:
        raise SystemExit(f"Coordinator served a bad key: {exc}") from exc
    if pin_file.exists():
        pinned = pin_file.read_text().strip().lower()
        if served != pinned:
            try:
                path = follow_chain(response.json().get("rotations") or [], pinned, served)
            except (RotationError, AttributeError, TypeError) as exc:
                raise SystemExit(
                    f"Coordinator key CHANGED (pinned {fingerprint(pinned)}, now serving "
                    f"{fingerprint(served)}) without a valid signed handover ({exc}). "
                    f"Refusing to run jobs. If the operator really replaced the key, confirm "
                    f"the new fingerprint with them and delete {pin_file}.") from exc
            replace_key_file(pin_file, served)
            print(f"Coordinator key rotated: {' -> '.join(fingerprint(k) for k in path)} "
                  f"(each step signed by both keys). Pin updated in {pin_file}.")
            return served
        return pinned
    save_key(pin_file, served)
    print(f"First contact: pinned coordinator key {fingerprint(served)} in {pin_file}. "
          "Check this fingerprint with the operator.")
    return served


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
    parser.add_argument("--coordinator-key", default=os.getenv("BNH_COORDINATOR_KEY"),
                        help="coordinator public key (hex) from the operator; "
                             "default: pin it on first contact")
    parser.add_argument("--coordinator-key-file", type=Path,
                        default=Path("data") / "coordinator.pub",
                        help="where the pinned coordinator key is kept")
    args = parser.parse_args()
    key_file = args.key_file or Path("data") / f"worker-{args.worker_id}.key"

    with httpx.Client(base_url=args.url, timeout=10) as client:
        coordinator_key = load_or_pin_coordinator_key(client, args.coordinator_key_file,
                                                      args.coordinator_key)
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
                check_manifest(job, coordinator_key)
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
                **schemas.receipt_fields(job, local_model_sha256(job)),
            })
            print(accepted.status_code, accepted.json())
            if args.once:
                return


if __name__ == "__main__":
    main()
