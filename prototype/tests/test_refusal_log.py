"""Code audit, thirteenth pass (`refusal-log-v1`): what one enrolled rig can make the
coordinator write.

Before this pass, every refused submission added a row to the audit trail: a result for a
job someone else had already finished (`submission_conflict`), for a job leased to another
rig (`submission_wrong_worker`), or with a receipt that doesn't match the job
(`receipt_mismatch`, on *any* job id, since the receipt is checked before the lease).
Measured on a real uvicorn server: 3,000 refused requests from one rig wrote 3,000 rows,
180 bytes each. Inside the default limit of 240 requests a minute that is 345,600 rows
(about 62 MB) a day, forever, all of them on another rig's job if the sender likes. With a
day of them on one job, the public `GET /v1/jobs/{id}` for that job was 54 MB and took 1.5 s.

Now the same refusal (same job, same rig, same reason) is written once and repeats are only
counted in memory for the operator health page; `GET /v1/jobs/{id}` filters and caps its
events inside SQLite; and every accepted result's audit-rate lookup reads only the sending
rig's results through a new index instead of every result ever accepted.
"""
from __future__ import annotations

import asyncio
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from app import copy_guard
from app.config import Settings
from app.ledger import MAX_JOB_EVENTS, REFUSAL_EVENTS, WORKER_INDEXES, Ledger
from app.main import PRIVATE_EVENTS, REFUSAL_REPEAT_WARN, create_app
from app.rewards import build_report
from tests.auth_helpers import OP, FirstPickRng, WorkerClient, enroll, receipt
from tests.test_audits import T0, Clock, claim, sha, submit
from tests.test_copy_guard import honest, variant  # noqa: F401  (fixture)
from tests.test_audits import pinned  # noqa: F401  (fixture used by honest)
from tests.test_ledger_pages import add_results, plan


def make(tmp_path: Path, **overrides):
    clock = Clock()
    fields = dict(database_path=tmp_path / "db.sqlite3", audit_rate=0.0, audit_rate_new=0.0,
                  audit_rate_suspect=0.0, operator_token=OP["X-Operator-Token"])
    app = create_app(Settings(**(fields | overrides)), clock=clock, rng=FirstPickRng(1))
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b", "wk-c"):
        enroll(client, w)
    return client, clock, app.state.ledger


def finished_job(client: WorkerClient, payload: str = "hello", worker: str = "wk-a") -> str:
    """A demo job `worker` claimed and finished."""
    r = client.post("/v1/jobs", json={"task_type": "demo:sha256", "payload": payload})
    job_id = r.json()["job_id"]
    assert claim(client, worker).json()["job_id"] == job_id
    assert submit(client, job_id, worker, sha(payload)).status_code == 200
    return job_id


def leased_job(client: WorkerClient, payload: str = "busy", worker: str = "wk-a") -> str:
    r = client.post("/v1/jobs", json={"task_type": "demo:sha256", "payload": payload})
    job_id = r.json()["job_id"]
    assert claim(client, worker).json()["job_id"] == job_id
    return job_id


def bad_receipt(client: WorkerClient, job_id: str, worker: str, **override):
    return client.post(f"/v1/jobs/{job_id}/submit", json={
        "worker_id": worker, "result": "x", "result_hash": sha("x"), "started_at": T0,
        "completed_at": T0, **(receipt(client, job_id) | override)})


def events_of(ledger: Ledger, job_id: str, event: str) -> list[dict]:
    return [e for e in ledger.events(job_id) if e["event"] == event]


def total_events(ledger: Ledger) -> int:
    with sqlite3.connect(ledger.path) as db:
        return db.execute("SELECT COUNT(*) FROM events").fetchone()[0]


# -- repeats are written once ----------------------------------------------------------------

def test_a_repeated_conflict_is_written_once(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = finished_job(client)
    before = total_events(ledger)
    for _ in range(50):
        assert submit(client, job_id, "wk-b", sha("wrong")).status_code == 409
    assert len(events_of(ledger, job_id, "submission_conflict")) == 1
    assert total_events(ledger) == before + 1
    assert ledger.repeated_refusals() == {"wk-b": 49}


def test_a_repeated_wrong_worker_submission_is_written_once(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = leased_job(client)
    for _ in range(20):
        assert submit(client, job_id, "wk-b", sha("busy")).status_code == 403
    assert len(events_of(ledger, job_id, "submission_wrong_worker")) == 1
    assert ledger.repeated_refusals() == {"wk-b": 19}
    # The rig the job is leased to is not affected.
    assert submit(client, job_id, "wk-a", sha("busy")).status_code == 200


def test_receipt_mismatches_are_written_once_per_reason(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = leased_job(client)
    for _ in range(10):
        for field in ("payload_hash", "model_sha256"):
            r = bad_receipt(client, job_id, "wk-a", **{field: "c" * 64})
            assert r.status_code == 422 and field in r.json()["detail"]
    found = events_of(ledger, job_id, "receipt_mismatch")
    assert sorted(e["detail"] for e in found) == ["model_sha256 does not match the job",
                                                  "payload_hash does not match the job"]
    assert ledger.repeated_refusals() == {"wk-a": 18}
    assert ledger.get_job(job_id)["status"] == "leased"  # lease untouched, as before


def test_a_rig_cannot_pile_rows_onto_another_rigs_job(tmp_path: Path) -> None:
    """The receipt is checked before the lease, so any rig could aim mismatches at any job
    id (they're public on the ledger pages). Now each costs it one row at most."""
    client, _, ledger = make(tmp_path)
    job_id = finished_job(client)
    public_before = client.get(f"/v1/jobs/{job_id}").json()["events_total"]
    for _ in range(100):
        bad_receipt(client, job_id, "wk-b", payload_hash="d" * 64)
        submit(client, job_id, "wk-b", sha("wrong"))
    body = client.get(f"/v1/jobs/{job_id}").json()
    assert body["events_total"] == public_before + 2
    assert len(body["events"]) == body["events_total"]


def test_each_rig_and_each_job_still_gets_its_own_row(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    first, second = finished_job(client, "one"), finished_job(client, "two")
    for job_id in (first, second):
        for w in ("wk-b", "wk-c"):
            for _ in range(3):
                assert submit(client, job_id, w, sha("wrong")).status_code == 409
    for job_id in (first, second):
        assert sorted(e["worker_id"] for e in events_of(ledger, job_id,
                                                         "submission_conflict")) == ["wk-b",
                                                                                     "wk-c"]
    assert ledger.repeated_refusals() == {"wk-b": 4, "wk-c": 4}


def test_honest_replays_and_retries_are_unchanged(tmp_path: Path) -> None:
    """A worker re-sending its own accepted result (a dropped reply) still gets 200 and
    writes nothing; a wrong answer on a leased job still uses up an attempt each time."""
    client, _, ledger = make(tmp_path)
    job_id = finished_job(client)
    before = total_events(ledger)
    for _ in range(5):
        r = submit(client, job_id, "wk-a", sha("hello"))
        assert r.status_code == 200 and r.json()["idempotent_replay"] is True
    assert total_events(ledger) == before and ledger.repeated_refusals() == {}
    other = leased_job(client, "retry")
    assert submit(client, other, "wk-a", sha("wrong")).status_code == 422
    assert len(events_of(ledger, other, "result_rejected_requeued")) == 1
    assert claim(client, "wk-a").json()["job_id"] == other
    assert submit(client, other, "wk-a", sha("wrong")).status_code == 422
    assert len(events_of(ledger, other, "result_rejected_requeued")) == 2


def test_the_reward_report_counts_different_refusals_not_repeats(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = finished_job(client)
    for _ in range(30):
        submit(client, job_id, "wk-b", sha("wrong"))
        bad_receipt(client, job_id, "wk-b", payload_hash="e" * 64)
    report = build_report(ledger, 0, T0 + 1)
    wk_b = next(w for w in report["workers"] if w["worker_id"] == "wk-b")
    assert wk_b["activity"]["bad_submissions"] == 2
    assert ledger.check_consistency() == []


def test_only_refusal_events_take_the_once_only_path(tmp_path: Path) -> None:
    _, _, ledger = make(tmp_path)
    with ledger._tx() as db, pytest.raises(ValueError):
        ledger._refusal(db, T0, "result_accepted", "j", "wk-a")
    assert set(REFUSAL_EVENTS) == {"submission_conflict", "submission_wrong_worker",
                                   "receipt_mismatch"}
    # Other notes are still appended every time.
    for _ in range(3):
        ledger.note(T0, "something_else", "j", "wk-a", "same")
    assert len(events_of(ledger, "j", "something_else")) == 3


def test_repeat_counts_live_in_memory_only(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = finished_job(client)
    for _ in range(3):
        submit(client, job_id, "wk-b", sha("wrong"))
    assert ledger.repeated_refusals() == {"wk-b": 2}
    assert Ledger(ledger.path).repeated_refusals() == {}
    # ...and a restart still doesn't write the old refusal again.
    reopened = Ledger(ledger.path)
    with reopened._tx() as db:
        assert reopened._refusal(db, T0, "submission_conflict", job_id, "wk-b") is False


# -- operator health ---------------------------------------------------------------------------

def test_operator_health_shows_repeats_and_warns_at_the_threshold(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = finished_job(client)
    for _ in range(REFUSAL_REPEAT_WARN):  # first is written, the rest are repeats
        submit(client, job_id, "wk-b", sha("wrong"))
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["repeated_refusals"] == {"rule": "refusal-log-v1",
                                           "since_start": {"wk-b": REFUSAL_REPEAT_WARN - 1},
                                           "warn_at": REFUSAL_REPEAT_WARN}
    assert not any("re-sent a submission" in w for w in health["warnings"])
    submit(client, job_id, "wk-b", sha("wrong"))
    health = client.get("/v1/operator/health", headers=OP).json()
    warning = [w for w in health["warnings"] if "re-sent a submission" in w]
    assert len(warning) == 1 and "wk-b" in warning[0] and "wk-c" not in warning[0]


def test_repeats_are_not_shown_to_the_public(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    job_id = finished_job(client)
    for _ in range(5):
        submit(client, job_id, "wk-b", sha("wrong"))
    assert "repeated_refusals" not in client.get("/health").json()
    assert client.get("/v1/operator/health").status_code in (401, 403)
    assert "repeat" not in client.get(f"/v1/jobs/{job_id}").text


# -- the public job view -----------------------------------------------------------------------

def test_public_job_view_is_capped_and_says_how_many_there_are(tmp_path: Path) -> None:
    """A ledger written before this version can still hold a pile of rows on one job."""
    client, _, ledger = make(tmp_path)
    job_id = finished_job(client)
    own = client.get(f"/v1/jobs/{job_id}").json()["events"]
    with sqlite3.connect(ledger.path) as db:
        db.executemany("INSERT INTO events(job_id, worker_id, event, at) VALUES(?,?,?,?)",
                       [(job_id, "wk-b", "submission_conflict", T0)] * 5000)
    body = client.get(f"/v1/jobs/{job_id}").json()
    assert len(body["events"]) == MAX_JOB_EVENTS == body["events_limit"]
    assert body["events_total"] == len(own) + 5000
    assert body["events"][:len(own)] == own  # oldest first: the job's own story comes first


def test_public_job_view_still_hides_private_events(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = finished_job(client)
    with sqlite3.connect(ledger.path) as db:
        db.executemany("INSERT INTO events(job_id, event, at) VALUES(?,?,?)",
                       [(job_id, e, T0) for e in sorted(PRIVATE_EVENTS)])
    body = client.get(f"/v1/jobs/{job_id}").json()
    shown = {e["event"] for e in body["events"]}
    assert not shown & PRIVATE_EVENTS
    assert body["events_total"] == len(body["events"])
    assert {e["job_id"] for e in body["events"]} == {job_id}


# -- query plans (no timing) -------------------------------------------------------------------

def test_the_once_only_lookup_reads_one_jobs_events(tmp_path: Path) -> None:
    _, _, ledger = make(tmp_path)
    p = plan(ledger, "SELECT 1 FROM events WHERE job_id=? AND worker_id=? AND event=? "
                     "AND detail IS ? LIMIT 1", ("j", "w", "submission_conflict", None))
    assert "events_by_job" in p and "SCAN events" not in p, p


def test_public_job_events_use_the_job_index(tmp_path: Path) -> None:
    _, _, ledger = make(tmp_path)
    hidden = tuple(sorted(PRIVATE_EVENTS))
    marks = ",".join("?" * len(hidden))
    for sql in (f"SELECT * FROM events WHERE job_id=? AND event NOT IN ({marks}) "
                f"ORDER BY id LIMIT ?",
                f"SELECT COUNT(*) FROM events WHERE job_id=? AND event NOT IN ({marks})"):
        args = ("j", *hidden) + ((10,) if "LIMIT" in sql else ())
        p = plan(ledger, sql, args)
        assert "events_by_job" in p and "SCAN events" not in p, p


def test_a_rigs_audit_record_reads_only_its_own_results(tmp_path: Path) -> None:
    _, _, ledger = make(tmp_path)
    add_results(ledger, 50)
    with sqlite3.connect(ledger.path) as db:
        p = " | ".join(r[3] for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT w.worker_id, SUM(s.audit_status='confirmed') "
            "FROM workers w LEFT JOIN submissions s ON s.worker_id=w.worker_id "
            "WHERE w.worker_id=? GROUP BY w.worker_id", ("wk-a",)))
    assert "submissions_by_worker" in p and "SCAN submissions" not in p, p
    assert ledger.audit_history("wk-a")["checks_passed"] == 0


# -- older ledgers and copies ------------------------------------------------------------------

def test_an_older_ledger_gets_the_worker_index_when_opened(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    Ledger(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX submissions_by_worker")
    Ledger(path)
    with sqlite3.connect(path) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "submissions_by_worker" in names and len(WORKER_INDEXES) == 1


def test_ledger_copies_with_and_without_the_worker_index_both_pass(honest: dict) -> None:
    with sqlite3.connect(honest["copy"]) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "submissions_by_worker" in names
    copy_guard.vet(honest["copy"])
    old = variant(honest, "old.sqlite3", "DROP INDEX submissions_by_worker")
    copy_guard.vet(old)
    odd = variant(honest, "odd.sqlite3", "DROP INDEX submissions_by_worker",
                  "CREATE INDEX submissions_by_worker ON submissions(result)")
    with pytest.raises(copy_guard.UnsafeCopyError):
        copy_guard.vet(odd)


# -- a real server -----------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, WorkerClient]]:
    app = create_app(Settings(database_path=tmp_path / "db.sqlite3",
                              operator_token=OP["X-Operator-Token"], audit_rate=0.0,
                              audit_rate_new=0.0, audit_rate_suspect=0.0,
                              rate_limit_per_minute=100_000))
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b"):
        enroll(client, w)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", client
    finally:
        srv.should_exit = True
        thread.join(timeout=10)


def test_a_flood_of_refusals_on_a_real_server_writes_two_rows(server) -> None:
    """Sequential and all at once, over real connections: the database doesn't grow."""
    base, client = server
    ledger = client.app.state.ledger
    now = int(time.time())
    job = client.post("/v1/jobs", json={"task_type": "demo:sha256", "payload": "p"}).json()
    job_id = job["job_id"]
    assert claim(client, "wk-a").status_code == 200
    ok = client.post(f"/v1/jobs/{job_id}/submit", json={
        "worker_id": "wk-a", "result": sha("p"), "result_hash": sha(sha("p")),
        "started_at": now, "completed_at": now, **receipt(client, job_id)})
    assert ok.status_code == 200, ok.text
    before = total_events(ledger)
    headers = {"Authorization": f"Bearer {client.worker_keys['wk-b']}"}
    body = {"result": "x", "result_hash": sha("x"), "started_at": now, "completed_at": now,
            **receipt(client, job_id)}
    mismatch = dict(body, payload_hash="0" * 64)

    async def flood() -> list[int]:
        async with httpx.AsyncClient(base_url=base, timeout=30,
                                     limits=httpx.Limits(max_connections=100)) as c:
            replies = await asyncio.gather(*[
                c.post(f"/v1/jobs/{job_id}/submit", json=body if i % 2 else mismatch,
                       headers=headers) for i in range(200)])
            for i in range(200):
                replies.append(await c.post(f"/v1/jobs/{job_id}/submit",
                                            json=body if i % 2 else mismatch, headers=headers))
            return [r.status_code for r in replies]

    codes = asyncio.run(flood())
    assert codes.count(409) == 200 and codes.count(422) == 200
    assert total_events(ledger) == before + 2
    assert sum(ledger.repeated_refusals().values()) == 398
