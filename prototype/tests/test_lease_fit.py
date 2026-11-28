"""`lease-fit-v1`: a job's lease must outlast the job's own time limit.

Found on the old code (replayed in `test_old_failure_honest_slow_runs_all_thrown_away`):
every lease was LEASE_SECONDS (default 60), while an `llm:generate` job may run for up to
`timeout_s` = 300 s, plus the time the worker spends re-hashing the model file first. An
honest run that took 61 s of a 120 s limit came back to "job is not awaiting a result" (or
"leased to a different worker", logged as a bad submission), was counted as a lease timeout
against the rig, and after MAX_ATTEMPTS honest rigs the job was `failed`. A job could also
be queued with a deadline (ttl_seconds 10) shorter than its own time limit (300 s).

Now the lease is max(LEASE_SECONDS, run limit + LEASE_GRACE_SECONDS), never past the job's
deadline, and a new job (or hidden test) whose ttl_seconds is shorter than that is refused.
Nothing here runs a model; the worker's run time is simulated by moving a fake clock.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app import model_registry, rewards, tasks
from app.config import Settings
from app.main import create_app
from app.model_registry import ModelEntry
from tests.auth_helpers import OP, OPERATOR, WorkerClient, enroll, receipt

T0 = 1_800_000_000
MODEL_ID = "tiny-test-model"
WEIGHTS = b"GGUF fake weights for tests"
GIB = 2**30


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"models": [
        {"model_id": MODEL_ID, "file_name": "tiny.gguf", "source_url": "https://example.invalid/t",
         "license": "Apache-2.0", "sha256": None, "size_bytes": None, "min_vram_mb": 1}]}))
    monkeypatch.setenv("MODEL_REGISTRY", str(reg))
    model = tmp_path / "tiny.gguf"
    model.write_bytes(WEIGHTS)
    model_registry.pin(MODEL_ID, model)
    return model


def make(tmp_path: Path, lease_seconds: int = 60, max_attempts: int = 3, audit_rate: float = 0.0):
    clock = Clock()
    settings = Settings(database_path=tmp_path / "db" / "ledger.sqlite3",
                        lease_seconds=lease_seconds, max_attempts=max_attempts,
                        audit_rate=audit_rate, audit_rate_new=audit_rate,
                        audit_rate_suspect=audit_rate, operator_token=OPERATOR,
                        public_settle_delay_seconds=0, rate_limit_per_minute=0)
    app = create_app(settings, clock=clock)
    client = WorkerClient(app)
    for w in ("rig-a", "rig-b", "rig-c"):
        enroll(client, w)
    return client, clock, app.state.ledger


def llm(timeout_s: int | None = 120, prompt: str = "Name a boat.") -> str:
    body = {"model_id": MODEL_ID, "prompt": prompt, "max_tokens": 64, "seed": 1}
    if timeout_s is not None:
        body["timeout_s"] = timeout_s
    return json.dumps(body)


def queue(client, payload: str, ttl: int = 600, task_type: str = "llm:generate"):
    return client.post("/v1/jobs", json={"task_type": task_type, "payload": payload,
                                         "ttl_seconds": ttl})


def claim(client, worker: str):
    return client.post("/v1/jobs/next", params={"worker_id": worker})


def submit(client, clock: Clock, job_id: str, worker: str, text: str, took: int):
    return client.post(f"/v1/jobs/{job_id}/submit", json={
        "worker_id": worker, "result": text,
        "result_hash": hashlib.sha256(text.encode()).hexdigest(),
        "started_at": int(clock.t) - took, "completed_at": int(clock.t),
        **receipt(client, job_id)})


def lease_end(ledger, job_id: str) -> int:
    return int(ledger.get_job(job_id)["lease_expires_at"])


def check_seconds(size: int) -> int:
    return -(-size * tasks.MODEL_CHECK_SECONDS_PER_GIB // GIB)


# -- the failure, replayed ------------------------------------------------------------------

def test_old_failure_honest_slow_runs_all_thrown_away(pinned, tmp_path: Path) -> None:
    """The exact old scenario: three honest rigs in turn, each run 61 s of a 120 s limit.
    Old code: 409 three times, three lease timeouts, job `failed`. Now the first one counts."""
    client, clock, ledger = make(tmp_path)
    job_id = queue(client, llm(120)).json()["job_id"]
    assert claim(client, "rig-a").status_code == 200
    assert lease_end(ledger, job_id) == T0 + 120 + check_seconds(len(WEIGHTS)) + 30
    clock.t += 61
    r = submit(client, clock, job_id, "rig-a", "The Salty Gull.", 61)
    assert r.status_code == 200, r.text
    assert ledger.get_job(job_id)["status"] == "accepted"
    names = [e["event"] for e in ledger.events(job_id)]
    assert "lease_timeout_requeued" not in names and "lease_timeout_failed" not in names


def test_slow_honest_rig_is_not_counted_as_a_bad_submission(pinned, tmp_path: Path) -> None:
    """Old code: another rig asking at +61 s took the job, and the honest one's result was
    `submission_wrong_worker`, a bad submission and a lease timeout in its reward report."""
    client, clock, ledger = make(tmp_path)
    job_id = queue(client, llm(120)).json()["job_id"]
    claim(client, "rig-a")
    clock.t += 61
    assert claim(client, "rig-b").status_code == 404  # still rig-a's
    clock.t += 50  # 111 s in, still inside the 120 s limit
    assert submit(client, clock, job_id, "rig-a", "The Salty Gull.", 111).status_code == 200
    report = rewards.build_report(ledger, T0, int(clock.t) + 1)
    (row,) = [w for w in report["workers"] if w["worker_id"] == "rig-a"]
    assert row["activity"] == {"leases": 1, "rejected": 0, "lease_timeouts": 0,
                               "bad_submissions": 0}
    assert row["accepted_results"] == 1 and report["checks"]["ok"]


def test_a_silent_rig_still_loses_the_job_when_its_lease_ends(pinned, tmp_path: Path) -> None:
    """Crash recovery is unchanged: just later, at the end of the fitted lease."""
    client, clock, ledger = make(tmp_path)
    job_id = queue(client, llm(120)).json()["job_id"]
    claim(client, "rig-a")
    end = lease_end(ledger, job_id)
    clock.t = end - 1
    assert claim(client, "rig-b").status_code == 404
    clock.t = end
    assert claim(client, "rig-b").json()["job_id"] == job_id
    clock.t += 1
    assert submit(client, clock, job_id, "rig-a", "late", 200).status_code == 403
    assert "lease_timeout_requeued" in [e["event"] for e in ledger.events(job_id)]


def test_lease_follows_each_jobs_own_limit(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    check = check_seconds(len(WEIGHTS))
    ids = [queue(client, llm(t, f"q{t}")).json()["job_id"] for t in (5, 60, 300)]
    ids.append(queue(client, llm(None, "default")).json()["job_id"])  # default 60
    limits = dict(zip(ids, (5, 60, 300, 60)))
    for _ in ids:
        job_id = claim(client, "rig-a").json()["job_id"]
        # 5 + check + 30 is under LEASE_SECONDS=60, so the plain lease wins there.
        assert lease_end(ledger, job_id) == T0 + max(60, limits.pop(job_id) + check + 30)
    assert limits == {}


def test_cpu_demo_jobs_keep_the_plain_lease(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, lease_seconds=45)
    job_id = queue(client, "hello", ttl=10, task_type="demo:sha256").json()["job_id"]
    claim(client, "rig-a")
    assert lease_end(ledger, job_id) == T0 + 10  # deadline first, as before
    job_id = queue(client, "hello2", ttl=600, task_type="demo:sha256").json()["job_id"]
    claim(client, "rig-a")
    assert lease_end(ledger, job_id) == T0 + 45


def test_longer_lease_seconds_still_wins(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, lease_seconds=900)
    job_id = queue(client, llm(120), ttl=3600).json()["job_id"]
    claim(client, "rig-a")
    assert lease_end(ledger, job_id) == T0 + 900


def test_lease_never_runs_past_the_jobs_deadline(pinned, tmp_path: Path) -> None:
    """A job handed out again late in its life gets only what is left of its deadline
    (the deadline is signed into the job; the lease can't move it)."""
    client, clock, ledger = make(tmp_path)
    needed = tasks.lease_needed("llm:generate", llm(120))
    job_id = queue(client, llm(120), ttl=needed + 20).json()["job_id"]
    claim(client, "rig-a")
    clock.t = lease_end(ledger, job_id)  # rig-a went silent
    assert claim(client, "rig-b").json()["job_id"] == job_id
    assert lease_end(ledger, job_id) == T0 + needed + 20 == ledger.get_job(job_id)["expires_at"]


# -- deadlines shorter than the job's own limit are refused ----------------------------------

def test_job_with_a_deadline_shorter_than_its_limit_is_refused(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    needed = tasks.lease_needed("llm:generate", llm(300))
    assert needed == 300 + check_seconds(len(WEIGHTS)) + 30
    before = ledger.status_counts()
    r = queue(client, llm(300), ttl=10)  # accepted by the old code
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert f"at least {needed}" in detail and "lease-fit-v1" in detail
    assert "Name a boat" not in detail  # never echoes the prompt
    r = queue(client, llm(300), ttl=needed - 1)
    assert r.status_code == 422
    assert ledger.status_counts() == before  # nothing stored
    assert queue(client, llm(300), ttl=needed).status_code == 200


def test_hidden_test_with_a_short_deadline_is_refused(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    needed = tasks.lease_needed("llm:generate", llm(120))
    body = {"task_type": "llm:generate", "payload": llm(120), "expected_result": "The Gull"}
    r = client.post("/v1/challenges", headers=OP, json=body | {"ttl_seconds": needed - 1})
    assert r.status_code == 422 and "lease-fit-v1" in r.json()["detail"]
    assert ledger.status_counts() == {}
    r = client.post("/v1/challenges", headers=OP, json=body | {"ttl_seconds": needed})
    assert r.status_code == 200, r.text


def test_cpu_demo_jobs_keep_their_short_deadlines(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    assert queue(client, "hello", ttl=10, task_type="demo:sha256").status_code == 200


def test_audit_copy_gets_the_same_fitted_lease(pinned, tmp_path: Path) -> None:
    """The re-run of a result keeps the original's window, which passed the check, and is
    leased by the same rule: an auditor taking 100 s of a 120 s limit is not a drop."""
    client, clock, ledger = make(tmp_path, audit_rate=1.0)
    job_id = queue(client, llm(120)).json()["job_id"]
    claim(client, "rig-a")
    assert submit(client, clock, job_id, "rig-a", "The Salty Gull.", 3).status_code == 200
    audit = next(a for a in ledger.audits() if a["audit_of"] == job_id)
    auditor = audit["assigned_worker"]
    got = claim(client, auditor).json()
    assert lease_end(ledger, got["job_id"]) >= int(clock.t) + 120 + 30
    clock.t += 100
    assert submit(client, clock, got["job_id"], auditor, "The Salty Gull.", 100).status_code == 200
    assert ledger.submission(job_id)["audit_status"] == "confirmed"
    follow = client.get(f"/v1/workers/{auditor}/trust", headers=OP).json()["audit_follow_through"]
    assert follow["completed"] == 1 and follow["lease_expired"] == 0 and follow["dropped"] == 0


# -- the rule itself ---------------------------------------------------------------------------

def entry(size: int | None, sha: str | None = "a" * 64) -> ModelEntry:
    return ModelEntry(MODEL_ID, "m.gguf", "https://example.invalid/m", "Apache-2.0", sha, size,
                      1, {})


@pytest.mark.parametrize("size,check", [(None, 0), (1, 1), (GIB, 30), (4 * GIB, 120),
                                        (4 * GIB + 1, 121), (8 * GIB, 240)])
def test_model_check_time_scales_with_the_pinned_size(size, check) -> None:
    task = tasks.LlmGenerateTask(registry={MODEL_ID: entry(size)})
    assert task.run_limit_seconds(llm(120)) == 120 + check
    assert task.run_limit_seconds(llm(None)) == 60 + check


@pytest.mark.parametrize("payload", [
    "not json", "[1, 2]", "null", "", json.dumps({"timeout_s": True}),
    json.dumps({"timeout_s": "120"}), json.dumps({"timeout_s": 4}), json.dumps({"timeout_s": 301}),
    json.dumps({"timeout_s": 1e9}), "[" * 5000, "[" * 100_000 + "]" * 100_000, json.dumps({"model_id": ["x"], "timeout_s": 999}),
])
def test_odd_payloads_get_the_longest_limit_and_never_raise(payload) -> None:
    task = tasks.LlmGenerateTask(registry={MODEL_ID: entry(GIB)})
    limit = task.run_limit_seconds(payload)
    assert limit >= tasks.LlmGenerateTask.MAX_TIMEOUT_S


def test_unknown_model_and_damaged_registry_add_no_check_time(monkeypatch, tmp_path) -> None:
    task = tasks.LlmGenerateTask(registry={})
    assert task.run_limit_seconds(llm(120)) == 120
    assert tasks.LlmGenerateTask(registry={MODEL_ID: entry(GIB)}).run_limit_seconds(
        json.dumps({"model_id": ["x"]})) == 60  # not a model name: default limit, no check
    bad = tmp_path / "registry.json"
    bad.write_text("{not json")
    monkeypatch.setenv("MODEL_REGISTRY", str(bad))
    assert tasks.LlmGenerateTask().run_limit_seconds(llm(120)) == 120


def test_lease_needed_per_task_type() -> None:
    assert tasks.lease_needed("demo:sha256", "anything") == 0
    assert tasks.lease_needed("no:such-type", "anything") == 0
    task = tasks.EXECUTORS["llm:generate"]
    payload = json.dumps({"model_id": "none-such", "prompt": "x", "timeout_s": 200})
    assert task.run_limit_seconds(payload) == 200
    assert tasks.lease_needed("llm:generate", payload) == 200 + tasks.LEASE_GRACE_SECONDS


def test_ledger_without_a_rule_keeps_the_plain_lease(tmp_path: Path) -> None:
    """`Ledger.claim_next` stays generic: without `lease_for` it leases LEASE_SECONDS."""
    from app.ledger import Ledger
    ledger = Ledger(tmp_path / "db" / "l.sqlite3")
    ledger.create_job({"job_id": "j1", "task_type": "llm:generate", "payload": llm(300),
                       "payload_hash": "0" * 64, "reward_points": 1, "expires_at": T0 + 3600,
                       "schema_version": "x", "model_sha256": None}, "sig", 3, T0)
    job = ledger.claim_next("rig-a", 60, T0)
    assert job is not None and job["lease_expires_at"] == T0 + 60
    ledger.close()
