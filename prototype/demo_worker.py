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

Everything the coordinator sends goes through app/worker_guard.py (`worker-input-v1`) first:
size and time limits on every reply, no compressed replies, no crashes on broken JSON,
nothing printed with terminal control codes, no worker key over plain http to another
machine (unless --allow-plain-http), and a growing wait after refused jobs.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from app import schemas, tasks
from app.key_rotation import RotationError, check_announcement, follow_chain
from app.security import sha256_text
from app.signing import fingerprint, parse_public_key, verify_manifest
from app.worker_guard import (CoordinatorError, Reply, check_job_id, check_url,
                              check_worker_key, refusal_wait, request_json, say)

SIGNED_FIELDS = schemas.SIGNED_FIELDS


def supported_tasks() -> set[str]:
    """Task types this machine can actually run right now."""
    return {name for name, ex in tasks.EXECUTORS.items() if ex.available()[0]}


class ManifestError(Exception):
    """The job envelope failed verification; the worker must not execute it."""


def check_manifest(job: dict[str, Any], coordinator_key: str, now: float | None = None) -> None:
    """Raise ManifestError unless the job is signed by `coordinator_key` (hex Ed25519 public
    key), uses a manifest version this worker understands, its payload matches its hash,
    this machine can run it with the same pinned model the job names, and it hasn't expired.
    Anything malformed is a ManifestError too, never a crash (`worker-input-v1`)."""
    if not isinstance(job, dict):
        raise ManifestError("not a job object")
    if job.get("schema_version") != schemas.JOB_SCHEMA_VERSION:
        raise ManifestError(f"unsupported job schema_version {say(repr(job.get('schema_version')), 40)}; "
                            f"this worker understands {schemas.JOB_SCHEMA_VERSION}")
    missing = [k for k in (*SIGNED_FIELDS, "signature") if k not in job]
    if missing:
        raise ManifestError(f"missing fields: {', '.join(missing)}")
    fields = {k: job[k] for k in SIGNED_FIELDS}
    if not verify_manifest(fields, str(job["signature"]), coordinator_key):
        raise ManifestError("bad signature: job was not signed by this coordinator")
    # Signed by the coordinator from here on, but still checked for shape before use.
    if not isinstance(job["payload"], str) or not isinstance(job["task_type"], str):
        raise ManifestError("payload and task type must be text")
    try:
        check_job_id(job["job_id"])
    except CoordinatorError as exc:
        raise ManifestError(str(exc)) from exc
    if type(job["expires_at"]) is not int:
        raise ManifestError("expires_at must be a whole number")
    if sha256_text(job["payload"]) != job["payload_hash"]:
        raise ManifestError("payload hash mismatch")
    if job["task_type"] not in supported_tasks():
        raise ManifestError(f"unsupported task type {say(repr(job['task_type']), 40)}")
    local = local_model_sha256(job)
    if local != job["model_sha256"]:
        raise ManifestError(f"model pin mismatch: job wants {say(job['model_sha256'], 80)}, this "
                            f"worker's registry has {local}")
    if int(job["expires_at"]) <= int(now if now is not None else time.time()):
        raise ManifestError("job already expired")


def local_model_sha256(job: dict[str, Any]) -> str | None:
    """The pinned model hash *this worker's* registry gives for the job (None if no model).
    The executor re-hashes the model file against the same pin before every run."""
    try:
        return tasks.get(job["task_type"]).model_sha256(job["payload"])
    except tasks.TaskError as exc:
        raise ManifestError(f"can't run this job here: {say(str(exc))}") from exc


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


def handover_pin_path(pin_file: Path) -> Path:
    """Where the pinned offline handover key is kept, next to the pinned coordinator key."""
    return pin_file.with_name("coordinator-handover.pub")


def load_handover_pin(pin_file: Path, given: str | None) -> str | None:
    """The offline handover key this worker trusts (`handover-key-v1`), or None.
    A key given by the operator out of band is pinned; it must agree with an earlier pin."""
    path = handover_pin_path(pin_file)
    pinned = path.read_text().strip().lower() if path.exists() else None
    if not given:
        return pinned
    try:
        parse_public_key(given)
    except ValueError as exc:
        raise SystemExit(f"Bad handover key: {exc}") from exc
    given = given.strip().lower()
    if pinned is not None and pinned != given:
        raise SystemExit(f"The handover key you gave ({fingerprint(given)}) differs from the "
                         f"one pinned in {path} ({fingerprint(pinned)}). Check with the "
                         f"operator; delete {path} only if they really replaced it.")
    if pinned is None:
        save_key(path, given)
    return given


def maybe_pin_handover_key(info: dict[str, Any], trusted: str, pin_file: Path,
                           pinned: str | None) -> None:
    """Pin the handover key the coordinator announces, if this worker has none yet and the
    announcement is signed by the coordinator key the worker already trusts. Never replaces
    a pinned handover key: that needs a person."""
    announced = info.get("handover_key")
    if announced is None:
        return
    try:
        handover = check_announcement(announced, trusted)
    except RotationError as exc:
        print(f"Warning: ignoring the coordinator's handover key announcement "
              f"({say(str(exc))}).")
        return
    if pinned is not None:
        if handover != pinned:
            print(f"Warning: the coordinator announces handover key {fingerprint(handover)}, "
                  f"but this worker pinned {fingerprint(pinned)}. Keeping the pinned one; "
                  "ask the operator.")
        return
    save_key(handover_pin_path(pin_file), handover)
    print(f"Pinned the coordinator's offline handover key {fingerprint(handover)} in "
          f"{handover_pin_path(pin_file)}. Key changes now also need this key's signature.")


def load_or_pin_coordinator_key(client: httpx.Client, pin_file: Path,
                                given: str | None = None,
                                handover_given: str | None = None) -> str:
    """Return the coordinator public key this worker trusts.

    - `given` (from the operator, out of band) is the strongest option: used as-is.
    - Otherwise a key pinned on an earlier run is used. If the coordinator now serves a
      different key, the worker only moves to it when the coordinator also shows a chain of
      handovers from the pinned key to the new one, each signed by both keys (a planned
      rotation, see app/key_rotation.py). If this worker has pinned the coordinator's
      offline handover key, every step must ALSO be signed by that key, so a stolen
      coordinator key file alone can't move the worker. Anything else and the worker stops:
      a person has to decide whether this is still the real coordinator.
    - First run with neither: trust on first use. Fetch, print the fingerprint so the owner
      can compare it with the operator's, and save it (readable by this user only).
    - Either way, once the worker trusts the served key, it pins the offline handover key
      the coordinator announces (signed by that key), if it has none yet.
    """
    if given:
        try:
            parse_public_key(given)
        except ValueError as exc:
            raise SystemExit(f"Bad coordinator key: {exc}") from exc
        return given.strip().lower()
    handover = load_handover_pin(pin_file, handover_given)
    try:
        response = request_json(client, "GET", "/v1/coordinator/key")
    except CoordinatorError as exc:
        raise SystemExit(f"Could not fetch the coordinator key: {exc}.") from exc
    if response.status_code != 200:
        raise SystemExit(f"Could not fetch the coordinator key ({response.status_code}).")
    info = response.json()
    served = str(info.get("public_key", "")).strip().lower()
    try:
        parse_public_key(served)
    except ValueError as exc:
        raise SystemExit(f"Coordinator served a bad key: {say(str(exc))}") from exc
    if pin_file.exists():
        pinned = pin_file.read_text().strip().lower()
        if served != pinned:
            try:
                path = follow_chain(info.get("rotations") or [], pinned, served, handover)
            except (RotationError, AttributeError, TypeError, KeyError) as exc:
                raise SystemExit(
                    f"Coordinator key CHANGED (pinned {fingerprint(pinned)}, now serving "
                    f"{fingerprint(served)}) without a valid signed handover "
                    f"({say(str(exc))}). "
                    f"Refusing to run jobs. If the operator really replaced the key, confirm "
                    f"the new fingerprint with them and delete {pin_file}.") from exc
            replace_key_file(pin_file, served)
            signed_by = "both keys" + (" and the offline handover key" if handover else "")
            print(f"Coordinator key rotated: {' -> '.join(fingerprint(k) for k in path)} "
                  f"(each step signed by {signed_by}). Pin updated in {pin_file}.")
        maybe_pin_handover_key(info, served, pin_file, handover)
        return served
    save_key(pin_file, served)
    print(f"First contact: pinned coordinator key {fingerprint(served)} in {pin_file}. "
          "Check this fingerprint with the operator.")
    maybe_pin_handover_key(info, served, pin_file, handover)
    return served


def load_or_enroll(client: httpx.Client, worker_id: str, key_file: Path,
                   enrollment_code: str | None, capabilities: list[str]) -> str:
    """Return this worker's key. With a saved key, update capabilities using it.
    Without one, use the operator's one-time code to register and save the new key."""
    if key_file.exists():
        key = key_file.read_text().strip()
        response = coordinator_call(client, "POST", "/v1/workers/register", json={
            "worker_id": worker_id, "capabilities": capabilities},
            headers={"Authorization": f"Bearer {key}"})
        if response.status_code != 200:
            raise SystemExit(f"Saved key refused ({response.status_code}): "
                             f"{response.detail()}")
        return key
    if not enrollment_code:
        raise SystemExit(f"No key at {key_file}. Ask the operator for a one-time enrollment "
                         "code and pass --enrollment-code (or set BNH_ENROLLMENT_CODE).")
    response = coordinator_call(client, "POST", "/v1/workers/register", json={
        "worker_id": worker_id, "capabilities": capabilities,
        "enrollment_code": enrollment_code})
    if response.status_code != 200:
        raise SystemExit(f"Registration refused ({response.status_code}): "
                         f"{response.detail()}")
    try:
        key = check_worker_key(response.json().get("worker_key"))
    except CoordinatorError as exc:
        raise SystemExit(f"Registration failed: {exc}. Nothing was saved.") from exc
    save_key(key_file, key)
    return key


MAX_BACKOFF_SECONDS = 60


def pause(seconds: float) -> None:
    """Every wait the worker does goes through here (tests replace it)."""
    time.sleep(seconds)


def coordinator_call(client: httpx.Client, method: str, url: str, **kwargs: Any) -> Reply:
    """One request through the worker's guards; a reply the worker won't use stops it with
    a plain message instead of a stack trace."""
    try:
        return request_json(client, method, url, **kwargs)
    except CoordinatorError as exc:
        raise SystemExit(f"Stopped: {exc}.") from exc


def retry_after_seconds(response: httpx.Response | Reply) -> int:
    """How long the coordinator asked us to wait after a 429 or 503 (1 to 60 s)."""
    try:
        wait = int(response.headers.get("Retry-After", "5"))
    except ValueError:
        wait = 5
    return max(1, min(wait, MAX_BACKOFF_SECONDS))


def post_with_backoff(client: httpx.Client, url: str, tries: int = 5,
                      sleep: Any = None, **kwargs: Any) -> Reply:
    """POST, and if the coordinator says "too many requests" (429), wait as long as it asks
    and try again, a few times. Safe for claims and for submits (submits are idempotent)."""
    for attempt in range(tries):
        response = coordinator_call(client, "POST", url, **kwargs)
        if response.status_code != 429 or attempt == tries - 1:
            return response
        wait = retry_after_seconds(response)
        print(f"Coordinator says slow down ({response.detail()}); waiting {wait} s.")
        (sleep or pause)(wait)
    return response


def execute(job: dict[str, Any]) -> str:
    return tasks.get(job["task_type"]).execute(job["payload"])


def run_job(job: dict[str, Any]) -> str | None:
    """Run the job, or return None (after saying why) if it can't be run here: a timeout,
    llama-cli failing, a bad LLM_* setting or a model file that no longer matches its pin.
    Nothing is submitted then; the lease runs out and the coordinator hands the job to
    another worker. The reason never contains the prompt."""
    try:
        return execute(job)
    except tasks.TaskError as exc:
        print(f"Could not run job {say(job.get('job_id'), 80)}: {say(str(exc))}")
        return None


def main(argv: list[str] | None = None) -> None:
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
    parser.add_argument("--handover-key", default=os.getenv("BNH_HANDOVER_KEY"),
                        help="coordinator's offline handover public key (hex) from the "
                             "operator; default: pin the one the coordinator announces")
    parser.add_argument("--allow-plain-http", action="store_true",
                        help="allow http:// to another machine (test labs only: the worker "
                             "key travels unencrypted)")
    parser.add_argument("--coordinator-key-file", type=Path,
                        default=Path("data") / "coordinator.pub",
                        help="where the pinned coordinator key is kept")
    args = parser.parse_args(argv)
    key_file = args.key_file or Path("data") / f"worker-{args.worker_id}.key"
    try:
        check_url(args.url, args.allow_plain_http)
    except CoordinatorError as exc:
        raise SystemExit(f"Stopped: {exc}") from exc

    with httpx.Client(base_url=args.url, timeout=10) as client:
        coordinator_key = load_or_pin_coordinator_key(client, args.coordinator_key_file,
                                                      args.coordinator_key, args.handover_key)
        key = load_or_enroll(client, args.worker_id, key_file, args.enrollment_code,
                             sorted(supported_tasks()))
        client.headers["Authorization"] = f"Bearer {key}"
        refusals = 0
        while True:
            response = post_with_backoff(client, "/v1/jobs/next",
                                         params={"worker_id": args.worker_id})
            if response.status_code == 404:
                print("No queued jobs.")
                return
            if response.status_code == 503:
                # `pause-v1`: the operator paused the coordinator (an incident or upkeep).
                # Not an error; a long-running worker would wait Retry-After and ask again.
                print(f"Coordinator is paused by its operator; try again in "
                      f"{retry_after_seconds(response)} s.")
                return
            if response.status_code != 200:
                raise SystemExit(f"Coordinator answered {response.status_code}: "
                                 f"{response.detail()}")
            job = response.json()
            try:
                check_manifest(job, coordinator_key)
            except ManifestError as exc:
                # Do not run it. The lease will time out and the coordinator will requeue it.
                print(f"Refused job {say(job.get('job_id'), 80)}: {say(str(exc))}")
                if args.once:
                    return
                refusals += 1
                wait = refusal_wait(refusals)
                print(f"Waiting {wait} s before asking again.")
                pause(wait)
                continue
            started = int(time.time())
            result = run_job(job)
            if result is None:
                if args.once:
                    return
                refusals += 1
                pause(refusal_wait(refusals))
                continue
            refusals = 0
            completed = int(time.time())
            accepted = post_with_backoff(client, f"/v1/jobs/{job['job_id']}/submit", json={
                "worker_id": args.worker_id, "result": result, "result_hash": sha256_text(result),
                "started_at": started, "completed_at": completed,
                **schemas.receipt_fields(job, local_model_sha256(job)),
            })
            print(accepted.status_code, say(json.dumps(accepted.json()), 300))
            if args.once:
                return


if __name__ == "__main__":
    main()
