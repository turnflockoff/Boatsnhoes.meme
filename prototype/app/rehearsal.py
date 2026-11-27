"""Two-worker rehearsal for Track F: one command that walks a coordinator through every kind
of good and bad submission and writes down what it answered.

Track F asks to "run two workers, show dispatch to both, show accepted and rejected receipts,
and test duplicate, malformed, expired and replayed submissions". This tool does the
coordinator half of that list from the outside, over HTTP, exactly like real workers would:

    # Anywhere, no setup: a throwaway in-process coordinator with a temporary database
    python -m app.rehearsal --local --out rehearsal.json

    # Against a running coordinator (for example on the rig, before the real workers start).
    # Use a SEPARATE test database: the rehearsal enrolls two workers and queues jobs.
    OPERATOR_TOKEN=... python -m app.rehearsal --url http://127.0.0.1:8000 \\
        --allow-writes --out rehearsal.json

What it does, in order:
1. Refuses to start if --out exists, or (with --url) without --allow-writes, without
   OPERATOR_TOKEN in the environment, or while the coordinator has queued jobs (rehearsal
   workers would otherwise take someone's real work).
2. Pins the coordinator's public key, enrolls two workers (`<id>-gpu0`, `<id>-gpu1`) with
   one-time codes, and queues small `demo:sha256` jobs one at a time.
3. Runs about 20 checks. Each one says what it sent, which answer it expected (status code)
   and what it got. Honest results must be accepted; duplicates must be paid once; another
   worker's, malformed, replayed, wrong, late and unauthenticated submissions must be refused;
   a tampered or expired job must be refused by the worker's own check.
4. Revokes both rehearsal workers and writes a versioned JSON report.

It never sends the operator token or worker keys anywhere but the coordinator, and never
writes them into the report. The operator token is read from the environment only, so it
doesn't end up in shell history or the process list.

What it proves: how this coordinator answers each case. What it doesn't: anything about the
GPUs. The jobs are tiny SHA-256 sums done on the CPU. `proves_gpu` and `proves_inference`
are always false; running two real workers on the two cards is still Track F's own step.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app import schemas
from app.operator_client import OperatorClient, check_operator_url
from app.security import sha256_text
from app.signing import fingerprint, parse_public_key
from app.worker_guard import JOB_ID_PATTERN, say

REHEARSAL_VERSION = "bnh-rehearsal/1"
TASK = "demo:sha256"
EXPIRY_TTL = 10  # the shortest job lifetime the coordinator allows (JobCreate.ttl_seconds)
NOTE = ("Software rehearsal: tiny SHA-256 jobs run on the CPU by this tool, acting as two "
        "workers. It shows how the coordinator answers good and bad submissions. It says "
        "nothing about the GPUs or AI inference.")


class RehearsalAbort(Exception):
    """Something outside the rehearsal's control happened; stop and clean up."""


class Rehearsal:
    """Drives one coordinator through the checks. `client` is any httpx.Client pointed at
    the coordinator (a FastAPI TestClient works too). `now` gives worker timestamps and
    `wait(seconds)` lets time pass (real sleep, or a test clock moving forward)."""

    def __init__(self, client: httpx.Client, operator_token: str, *,
                 now: Callable[[], float] = time.time,
                 wait: Callable[[float], None] = time.sleep,
                 run_id: str | None = None) -> None:
        self.client = client
        self.op = {"X-Operator-Token": operator_token}
        self.now = now
        self.wait = wait
        self.run_id = run_id or f"rehearsal-{secrets.token_hex(3)}"
        self.workers = [f"{self.run_id}-gpu0", f"{self.run_id}-gpu1"]
        self.keys: dict[str, str] = {}
        self.jobs: dict[str, dict[str, Any]] = {}  # job_id -> envelope, rehearsal jobs only
        self.coordinator_key = ""
        self.checks: list[dict[str, Any]] = []

    # ---- plumbing -------------------------------------------------------------------

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One request; waits and retries a few times if the coordinator says 429."""
        for _ in range(5):
            response = self.client.request(method, url, **kwargs)
            if response.status_code != 429:
                return response
            try:
                pause = int(response.headers.get("Retry-After", "5"))
            except ValueError:
                pause = 5
            self.wait(max(1, min(pause, 60)))
        return response

    def auth(self, worker: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[worker]}"}

    def check(self, name: str, what: str, expected: Any, got: Any, passed: bool,
              detail: str = "") -> bool:
        self.checks.append({"name": name, "what": what, "expected": expected, "got": got,
                            "passed": bool(passed), "detail": detail})
        return bool(passed)

    def expect_status(self, name: str, what: str, response: httpx.Response, expected: int,
                      extra_ok: bool = True) -> bool:
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict) and "detail" in body:
                detail = say(str(body["detail"]))
        except ValueError:
            detail = say(response.text)
        return self.check(name, what, expected, response.status_code,
                          response.status_code == expected and extra_ok, detail)

    def queue(self, payload: str, ttl: int = 600) -> str:
        response = self.request("POST", "/v1/jobs", headers=self.op,
                                json={"task_type": TASK, "payload": payload, "ttl_seconds": ttl})
        if response.status_code != 200:
            raise RehearsalAbort(f"could not queue a job ({response.status_code}: "
                                 f"{say(response.text)})")
        job = response.json()
        if not (isinstance(job, dict) and isinstance(job.get("job_id"), str)
                and JOB_ID_PATTERN.fullmatch(job["job_id"])):
            raise RehearsalAbort("the coordinator queued a job but sent no usable job id")
        self.jobs[job["job_id"]] = job
        return str(job["job_id"])

    def claim(self, worker: str, expected_job: str) -> dict[str, Any]:
        response = self.request("POST", "/v1/jobs/next", params={"worker_id": worker},
                                headers=self.auth(worker))
        if response.status_code != 200:
            raise RehearsalAbort(f"{worker} could not claim its job ({response.status_code})")
        job = response.json()
        if job.get("job_id") != expected_job:
            # Someone else's job. Don't run it; revoking this worker hands it back.
            raise RehearsalAbort("another job was queued during the rehearsal and a "
                                 "rehearsal worker was handed it; stopped without running "
                                 "it. Use a coordinator nobody else is using.")
        return dict(job)

    def receipt(self, worker: str, job: dict[str, Any], result: str | None = None,
                **overrides: Any) -> dict[str, Any]:
        """What an honest worker sends for `job`, with `overrides` applied on top."""
        result = sha256_text(job["payload"]) if result is None else result
        t = int(self.now())
        body = {"worker_id": worker, "result": result, "result_hash": sha256_text(result),
                "started_at": t, "completed_at": t,
                **schemas.receipt_fields(job, job.get("model_sha256"))}
        body.update(overrides)
        return body

    def submit(self, worker: str, job_id: str, body: Any, raw: bool = False) -> httpx.Response:
        kwargs: dict[str, Any] = {"content": body, "headers": self.auth(worker)
                                  | {"Content-Type": "application/json"}} if raw \
            else {"json": body, "headers": self.auth(worker)}
        return self.request("POST", f"/v1/jobs/{job_id}/submit", **kwargs)

    def status_of(self, job_id: str) -> str:
        response = self.request("GET", f"/v1/jobs/{job_id}")
        return str(response.json().get("status")) if response.status_code == 200 else "?"

    def total_points(self) -> int:
        # Operator view: since `ledger-pages-v1` the public totals are taken once per window.
        return int(self.request("GET", "/v1/ledger", params={"limit": 1}, headers=self.op
                                ).json()["total_simulated_points"])

    # ---- steps ----------------------------------------------------------------------

    def preflight(self) -> None:
        """Refuse to run where rehearsal workers could take someone's queued work."""
        # Operator view: the public job counts are batched (`public-counts-v1`) and could
        # still say 0 minutes after someone queued real work.
        response = self.request("GET", "/v1/ledger", params={"limit": 1}, headers=self.op)
        if response.status_code in (401, 403):
            raise RehearsalAbort(f"the operator token was refused ({response.status_code}); "
                                 "check OPERATOR_TOKEN")
        if response.status_code != 200:
            raise RehearsalAbort(f"could not read /v1/ledger ({response.status_code})")
        queued = int(response.json().get("jobs", {}).get("queued", 0))
        if queued:
            raise RehearsalAbort(f"the coordinator has {queued} queued job(s); the rehearsal "
                                 "workers could take them. Run it against an empty test "
                                 "coordinator (its own DATABASE_PATH).")
        health = self.request("GET", "/v1/operator/health", headers=self.op)
        if health.status_code != 200:
            raise RehearsalAbort(f"the operator token was refused ({health.status_code}); "
                                 "check OPERATOR_TOKEN")
        if health.json().get("pause", {}).get("paused"):
            # `pause-v1`: every claim would get 503 and the run would only report failures.
            raise RehearsalAbort("the coordinator is paused (python -m app.incident status); "
                                 "resume it first, or rehearse against a test coordinator")

    def setup(self) -> None:
        from demo_worker import check_manifest  # noqa: F401  (fail early if it's missing)

        response = self.request("GET", "/v1/coordinator/key")
        key = str(response.json().get("public_key", "")) if response.status_code == 200 else ""
        try:
            parse_public_key(key)
            ok = True
        except ValueError:
            ok = False
        self.check("coordinator_key", "The coordinator publishes a valid Ed25519 public key "
                   "for workers to pin.", "valid key", "valid key" if ok else "missing/bad", ok)
        if not ok:
            raise RehearsalAbort("no usable coordinator key")
        self.coordinator_key = key.strip().lower()
        for n, worker in enumerate(self.workers):
            code = self.request("POST", "/v1/enrollments", headers=self.op,
                                json={"owner": f"{self.run_id}-owner{n}", "ttl_seconds": 600})
            if code.status_code != 200:
                raise RehearsalAbort(f"could not create an enrollment code ({code.status_code})")
            reg = self.request("POST", "/v1/workers/register", json={
                "worker_id": worker, "capabilities": [TASK],
                "enrollment_code": code.json()["enrollment_code"]})
            if not self.expect_status(f"enroll_{worker.rsplit('-', 1)[1]}",
                                      f"Worker {worker} registers with a one-time code.",
                                      reg, 200):
                raise RehearsalAbort("a rehearsal worker could not register")
            self.keys[worker] = str(reg.json()["worker_key"])

    def run_checks(self) -> None:
        from demo_worker import ManifestError, check_manifest

        a, b = self.workers
        # 1. Dispatch to both workers, both honest results accepted.
        j1 = self.queue(f"{self.run_id} job 1")
        job1 = self.claim(a, j1)
        j2 = self.queue(f"{self.run_id} job 2")
        job2 = self.claim(b, j2)
        problems = []
        for job in (job1, job2):
            try:
                check_manifest(job, self.coordinator_key, now=self.now())
            except ManifestError as exc:
                problems.append(f"{job['job_id']}: {exc}")
        self.check("signed_jobs_verify", "Both workers check the coordinator's signature, "
                   "payload hash and deadline before running anything.", "no problems",
                   problems or "no problems", not problems)
        before = self.total_points()
        r1 = self.submit(a, j1, self.receipt(a, job1))
        r2 = self.submit(b, j2, self.receipt(b, job2))
        self.check("dispatch_both_accepted", "Each worker got its own job, ran it and had the "
                   "honest answer accepted.", [200, 200], [r1.status_code, r2.status_code],
                   r1.status_code == r2.status_code == 200)
        paid = self.total_points() - before

        # 2. The worker refuses a job someone changed on the way.
        tampered = dict(job1, payload=job1["payload"] + " (changed)")
        tampered["payload_hash"] = sha256_text(tampered["payload"])
        try:
            check_manifest(tampered, self.coordinator_key, now=self.now())
            refused = "ran it"
        except ManifestError as exc:
            refused = str(exc)
        self.check("tampered_job_refused", "A job whose payload was changed after signing is "
                   "refused by the worker before running.", "refused (bad signature)",
                   refused, "bad signature" in refused)

        # 3. Duplicates and replays of a finished job.
        dup = self.submit(a, j1, self.receipt(a, job1))
        is_replay = dup.status_code == 200 and dup.json().get("idempotent_replay") is True
        self.expect_status("duplicate_is_idempotent", "The same worker sends the same answer "
                           "again (a network retry): accepted as a replay, not paid twice.",
                           dup, 200, is_replay and self.total_points() - before == paid)
        self.expect_status("changed_answer_after_accept", "The same worker sends a different "
                           "answer for a job already accepted.",
                           self.submit(a, j1, self.receipt(a, job1, result="0" * 64)), 409)
        self.expect_status("other_worker_replays_finished_job", "The other worker sends the "
                           "accepted answer for a job it never had.",
                           self.submit(b, j1, self.receipt(b, job1)), 409)

        # 4. A job leased to worker A: everyone else and every malformed receipt is refused.
        j3 = self.queue(f"{self.run_id} job 3")
        job3 = self.claim(a, j3)
        self.expect_status("wrong_worker", "Worker B submits the right answer for a job "
                           "leased to worker A.", self.submit(b, j3, self.receipt(b, job3)), 403)
        self.expect_status("result_hash_mismatch", "The result doesn't match its own hash.",
                           self.submit(a, j3, self.receipt(a, job3, result_hash="f" * 64)), 422)
        missing = self.receipt(a, job3)
        missing.pop("result")
        self.expect_status("missing_field", "A receipt with no result at all.",
                           self.submit(a, j3, missing), 422)
        self.expect_status("not_json", "A body that isn't JSON.",
                           self.submit(a, j3, b"{not json", raw=True), 422)
        t = int(self.now())
        self.expect_status("bad_timestamps", "Finished before it started.",
                           self.submit(a, j3, self.receipt(a, job3, started_at=t,
                                                           completed_at=t - 5)), 422)
        self.expect_status("unknown_receipt_version", "A receipt layout this coordinator "
                           "doesn't know.", self.submit(a, j3, self.receipt(
                               a, job3, receipt_version="bnh-receipt/99")), 422)
        self.expect_status("no_key", "A submission with no worker key.",
                           self.request("POST", f"/v1/jobs/{j3}/submit",
                                        json=self.receipt(a, job3)), 401)
        self.expect_status("wrong_key", "A submission with a made-up worker key (counts once "
                           "toward the lock-out limit).", self.request(
                               "POST", f"/v1/jobs/{j3}/submit", json=self.receipt(a, job3),
                               headers={"Authorization": "Bearer bnhw_" + "0" * 40}), 401)
        self.expect_status("unknown_job", "A receipt for a job id that doesn't exist.",
                           self.submit(a, "no-such-job-" + self.run_id,
                                       self.receipt(a, job3)), 404)

        # 5. Replay a valid receipt from one job onto another job.
        j4 = self.queue(f"{self.run_id} job 4")
        job4 = self.claim(a, j4)
        replay = self.submit(a, j4, self.receipt(a, job3))
        self.expect_status("receipt_replayed_on_other_job", "Worker A sends job 3's valid "
                           "receipt as the answer to job 4.", replay, 422,
                           self.status_of(j4) == "leased")

        # 6. A wrong answer is rejected and the job goes back in the queue; B then does it.
        wrong = self.submit(a, j4, self.receipt(a, job4, result=sha256_text("wrong")))
        self.expect_status("wrong_answer_requeued", "Worker A sends a well-formed but wrong "
                           "answer.", wrong, 422, self.status_of(j4) == "queued")
        job4b = self.claim(b, j4)
        self.expect_status("requeued_job_done_by_other_worker", "Worker B picks the requeued "
                           "job up and its honest answer is accepted.",
                           self.submit(b, j4, self.receipt(b, job4b)), 200)

        # 7. A job nobody has claimed yet can't be answered.
        j5 = self.queue(f"{self.run_id} job 5")
        self.expect_status("not_leased", "Worker A answers a queued job before claiming it.",
                           self.submit(a, j5, self.receipt(a, self.jobs[j5])), 409)
        job5 = self.claim(a, j5)
        self.submit(a, j5, self.receipt(a, job5))
        self.expect_status("job3_finished", "Worker A finally sends job 3's honest answer, "
                           "after all the refused attempts.",
                           self.submit(a, j3, self.receipt(a, job3)), 200)

        # 8. A late answer is refused by the coordinator and the job by the worker.
        j6 = self.queue(f"{self.run_id} job 6", ttl=EXPIRY_TTL)
        job6 = self.claim(b, j6)
        self.wait(EXPIRY_TTL + 1)
        late = self.submit(b, j6, self.receipt(b, job6))
        self.expect_status("expired_submission", f"Worker B answers {EXPIRY_TTL + 1} s after "
                           "a job with a 10 s deadline.", late, 410,
                           self.status_of(j6) == "expired")
        try:
            check_manifest(job6, self.coordinator_key, now=self.now())
            seen = "would run it"
        except ManifestError as exc:
            seen = str(exc)
        self.check("expired_job_refused_by_worker", "The worker's own check refuses a job "
                   "whose deadline has passed.", "refused (expired)", seen, "expired" in seen)

        # 9. The books still add up and nothing is left half done.
        # Operator view: since `ledger-pages-v1` the public one repeats the check taken at
        # the start of the counts window, which could be from before this run.
        ledger = self.request("GET", "/v1/ledger", params={"limit": 1}, headers=self.op).json()
        problems = ledger.get("consistency_problems", [])
        self.check("ledger_consistent", "The coordinator's own consistency checks find no "
                   "problems after all of the above.", [], problems, problems == [])
        open_jobs = {j: s for j in self.jobs if (s := self.status_of(j))
                     not in ("accepted", "expired")}
        self.check("no_jobs_left_open", "Every rehearsal job ended accepted or expired.", {},
                   open_jobs, not open_jobs)

    def cleanup(self) -> None:
        revoked = []
        for worker in self.keys:
            response = self.request("POST", f"/v1/workers/{worker}/revoke", headers=self.op)
            if response.status_code == 200:
                revoked.append(worker)
        self.check("workers_revoked", "Both rehearsal workers are revoked at the end, so their "
                   "keys are useless afterwards.", sorted(self.keys), sorted(revoked),
                   sorted(revoked) == sorted(self.keys))

    def run(self) -> dict[str, Any]:
        aborted = None
        try:
            self.preflight()
            self.setup()
            self.run_checks()
        except RehearsalAbort as exc:
            aborted = str(exc)
        except httpx.HTTPError as exc:  # incl. a reply over the limits (`operator-client-v1`)
            aborted = f"lost contact with the coordinator ({say(str(exc)) or type(exc).__name__})"
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            aborted = f"the coordinator's reply had an unexpected shape ({type(exc).__name__})"
        finally:
            if self.keys:
                try:
                    self.cleanup()
                except (httpx.HTTPError, ValueError) as exc:
                    aborted = aborted or (f"lost contact while revoking the rehearsal workers "
                                          f"({type(exc).__name__}); revoke them by hand")
        passed = sum(1 for c in self.checks if c["passed"])
        return {
            "rehearsal_version": REHEARSAL_VERSION,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "coordinator_key_fingerprint": (fingerprint(self.coordinator_key)
                                            if self.coordinator_key else None),
            "workers": self.workers,
            "task_type": TASK,
            "checks": self.checks,
            "passed": passed,
            "failed": len(self.checks) - passed,
            "aborted": aborted,
            "all_passed": aborted is None and passed == len(self.checks) and passed > 0,
            "proves_gpu": False,
            "proves_inference": False,
            "note": NOTE,
        }


class _Clock:
    """Coordinator and worker clock for --local: starts at real time, moves only on wait()."""

    def __init__(self) -> None:
        self.t = float(int(time.time()))

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@contextmanager
def local_coordinator() -> Iterator[tuple[httpx.Client, str, _Clock]]:
    """A throwaway coordinator in this process, with its own temporary database and key."""
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    clock = _Clock()
    token = secrets.token_urlsafe(32)
    # The coordinator's own log lines would bury the summary; the report has every answer.
    quiet = logging.getLogger("bnh")
    old_level = quiet.level
    quiet.setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="bnh-rehearsal-") as tmp:
        settings = Settings(database_path=Path(tmp) / "rehearsal.sqlite3",
                            operator_token=token, audit_rate=0.0, audit_rate_new=0.0,
                            audit_rate_suspect=0.0)
        try:
            with TestClient(create_app(settings, clock=clock)) as client:
                yield client, token, clock
        finally:
            quiet.setLevel(old_level)


def summary(report: dict[str, Any]) -> str:
    total = len(report["checks"])
    lines = [f"Rehearsal {report['run_id']}: {report['passed']} of {total} checks passed."]
    for c in report["checks"]:
        if not c["passed"]:
            lines.append(f"  FAILED {c['name']}: expected {say(repr(c['expected']))}, "
                         f"got {say(repr(c['got']))}"
                         + (f" ({say(c['detail'])})" if c["detail"] else ""))
    if report["aborted"]:
        lines.append(f"  STOPPED: {say(report['aborted'], 400)}")
    lines.append("This tests the coordinator's rules only; it says nothing about the GPUs.")
    return "\n".join(lines)


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Never overwrites: the file is created only if it doesn't exist yet."""
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Two-worker rehearsal of good and bad submissions (Track F, software only)")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--local", action="store_true",
                       help="run against a throwaway in-process coordinator")
    where.add_argument("--url", help="a running coordinator: https://..., or "
                       "http://127.0.0.1:8000 on this machine")
    parser.add_argument("--allow-plain-http", action="store_true",
                        help="allow http:// to another machine (a test lab you control); the "
                             "operator token then travels unencrypted")
    parser.add_argument("--allow-writes", action="store_true",
                        help="needed with --url: the rehearsal enrolls workers and queues jobs")
    parser.add_argument("--out", type=Path, required=True, help="report file (never overwritten)")
    args = parser.parse_args(argv)

    if args.out.exists():
        print(f"Refusing: {args.out} already exists.", file=sys.stderr)
        return 2
    if args.local:
        with local_coordinator() as (client, token, clock):
            report = Rehearsal(client, token, now=clock, wait=clock.advance).run()
    else:
        if not args.allow_writes:
            print("Refusing: --url changes that coordinator's database (two workers, a few "
                  "jobs, simulated points). Point it at a test coordinator with its own "
                  "DATABASE_PATH and add --allow-writes.", file=sys.stderr)
            return 2
        token = os.getenv("OPERATOR_TOKEN") or ""
        if not token:
            print("Refusing: set OPERATOR_TOKEN in the environment (not on the command line).",
                  file=sys.stderr)
            return 2
        try:
            check_operator_url(args.url, args.allow_plain_http)
        except ValueError as exc:
            print(f"Refusing: {exc}", file=sys.stderr)
            return 2
        with OperatorClient(args.url, allow_plain_http=args.allow_plain_http) as client:
            report = Rehearsal(client, token).run()
        report["coordinator"] = args.url
    report.setdefault("coordinator", "in-process (temporary database)")
    write_report(args.out, report)
    print(summary(report))
    print(f"Report written to {args.out}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
