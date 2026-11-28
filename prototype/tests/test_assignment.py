"""Threat model #4: audits and tie-breakers go to a randomly chosen active worker,
not to whoever asks first."""
from __future__ import annotations

import random
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app
from tests.auth_helpers import WorkerClient, enroll, let_lease_run_out
from tests.test_audits import (
    OP,
    OPERATOR,
    Clock,
    audit_status,
    claim,
    pinned,  # noqa: F401  (pytest fixture)
    run_llm_job,
    submit,
)

pytestmark = pytest.mark.usefixtures("pinned")


def make(tmp_path: Path, workers: tuple[str, ...], seed: int = 1, owners: dict | None = None,
         active: int = 300, assign: int = 120, **extra):
    clock = Clock()
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, audit_rate=1.0, operator_token=OPERATOR,
                        auditor_active_seconds=active, audit_assign_seconds=assign,
                        # These tests send hundreds of requests in a second to measure the
                        # random pick; request limits are tested in test_rate_limits.py.
                        rate_limit_per_minute=0, **extra)
    app = create_app(settings, clock=clock, rng=random.Random(seed))
    client = WorkerClient(app)
    for w in workers:
        enroll(client, w, owner=(owners or {}).get(w))
    return client, clock, app.state.ledger


def assigned(ledger, original: str) -> str | None:
    audit = next(a for a in ledger.audits() if a["audit_of"] == original)
    return audit["assigned_worker"]


def other(workers: tuple[str, ...], *skip: str) -> str:
    return next(w for w in workers if w not in skip)


def test_fast_poller_cannot_grab_an_audit_assigned_to_someone_else(tmp_path: Path) -> None:
    ws = ("wk-a", "wk-b", "wk-c")
    client, _, ledger = make(tmp_path, ws)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    chosen = assigned(ledger, original)  # picked the moment the audit was queued
    assert chosen in ("wk-b", "wk-c")
    loser = other(ws, "wk-a", chosen)
    # The other worker can ask as often as it likes; it never gets this audit.
    for _ in range(5):
        assert claim(client, loser).status_code == 404
    audit = claim(client, chosen).json()
    assert submit(client, audit["job_id"], chosen, "The Salty Hoe").status_code == 200
    assert audit_status(ledger, original) == "confirmed"
    assert client.get("/v1/ledger").json()["consistency_problems"] == []


def test_audits_are_spread_at_random_not_first_come(tmp_path: Path) -> None:
    """wk-b always asks first. First-come would hand it every audit."""
    ws = ("wk-a", "wk-b", "wk-c", "wk-d")
    client, _, ledger = make(tmp_path, ws, seed=4)
    counts: Counter[str] = Counter()
    for i in range(60):
        original = run_llm_job(client, "wk-a", f"boat {i}")
        for w in ("wk-b", "wk-c", "wk-d"):  # same order every round: wk-b is the racer
            r = claim(client, w)
            if r.status_code == 200:
                counts[w] += 1
                assert submit(client, r.json()["job_id"], w, f"boat {i}").status_code == 200
                break
        assert audit_status(ledger, original) == "confirmed"
    assert sum(counts.values()) == 60
    # Fair share is 20 each. Each bound fails by chance far less than once in a million runs.
    assert all(5 <= counts[w] <= 40 for w in ("wk-b", "wk-c", "wk-d")), counts
    assert client.get("/v1/ledger").json()["consistency_problems"] == []


def test_assignment_skips_inactive_revoked_and_same_owner_workers(tmp_path: Path) -> None:
    ws = ("wk-a", "wk-a2", "wk-b", "wk-old", "wk-gone")
    client, clock, ledger = make(tmp_path, ws, owners={"wk-a2": "owner-wk-a"}, active=300)
    clock.t += 400  # everyone enrolled 400s ago: nobody is active any more
    for w in ("wk-a", "wk-a2", "wk-b", "wk-gone"):
        claim(client, w)  # these four ask for work, so they're active again; wk-old isn't
    assert client.post("/v1/workers/wk-gone/revoke", headers=OP).status_code == 200
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    # Only wk-b is left: active, not revoked, different owner.
    assert assigned(ledger, original) == "wk-b"
    assert claim(client, "wk-a2").status_code == 404  # same owner as wk-a
    assert claim(client, "wk-b").status_code == 200


def test_silent_assignee_loses_the_audit_to_another_random_worker(tmp_path: Path) -> None:
    ws = ("wk-a", "wk-b", "wk-c")
    client, clock, ledger = make(tmp_path, ws, assign=60)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    first = assigned(ledger, original)
    second = other(ws, "wk-a", first)
    assert claim(client, second).status_code == 404  # still the first pick's turn
    clock.t += 61  # the first pick never showed up
    audit = claim(client, second)
    assert audit.status_code == 200 and assigned(ledger, original) == second
    assert submit(client, audit.json()["job_id"], second, "The Salty Hoe").status_code == 200
    assert audit_status(ledger, original) == "confirmed"


def test_audit_given_back_goes_to_someone_else(tmp_path: Path) -> None:
    ws = ("wk-a", "wk-b", "wk-c")
    client, clock, ledger = make(tmp_path, ws)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    first = assigned(ledger, original)
    assert claim(client, first).status_code == 200
    let_lease_run_out(ledger, clock, first)  # the audit goes back in the queue
    second = other(ws, "wk-a", first)
    assert claim(client, second).status_code == 200
    assert assigned(ledger, original) == second


def test_no_eligible_worker_waits_then_assigns_on_arrival(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, ("wk-a", "wk-b"),
                             owners={"wk-b": "owner-wk-a"})  # one person, two rigs
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert claim(client, "wk-b").status_code == 404
    assert assigned(ledger, original) is None
    enroll(client, "wk-z")
    assert claim(client, "wk-z").status_code == 200
    assert assigned(ledger, original) == "wk-z"


def test_tiebreaker_is_randomly_assigned_too(tmp_path: Path) -> None:
    ws = ("wk-a", "wk-b", "wk-c", "wk-d")
    client, _, ledger = make(tmp_path, ws)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    auditor = assigned(ledger, original)
    audit = claim(client, auditor).json()
    submit(client, audit["job_id"], auditor, "A different boat")
    tb = next(a for a in ledger.audits() if a["job_kind"] == "tiebreak")
    assert tb["assigned_worker"] in set(ws) - {"wk-a", auditor}
    loser = other(ws, "wk-a", auditor, tb["assigned_worker"])
    assert claim(client, loser).status_code == 404
    tb_job = claim(client, tb["assigned_worker"]).json()
    assert tb_job["job_id"] == tb["job_id"]


def test_assignment_events_stay_private(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, ("wk-a", "wk-b"))
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    audit = claim(client, "wk-b").json()
    assert any(e["event"] == "auditor_assigned" for e in ledger.events(audit["job_id"]))
    public = client.get(f"/v1/jobs/{audit['job_id']}").json()
    assert "assigned_worker" not in public
    assert all(e["event"] != "auditor_assigned" for e in public["events"])
    assert "assigned_worker" not in audit  # the job envelope doesn't carry it either
    assert original


def test_consistency_check_flags_an_audit_taken_by_the_wrong_worker(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, ("wk-a", "wk-b", "wk-c"))
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    chosen = assigned(ledger, original)
    audit = claim(client, chosen).json()
    submit(client, audit["job_id"], chosen, "The Salty Hoe")
    assert ledger.check_consistency() == []
    with sqlite3.connect(ledger.path) as db:  # tamper: pretend someone else was assigned
        db.execute("UPDATE jobs SET assigned_worker=? WHERE job_id=?",
                   (other(("wk-b", "wk-c"), chosen), audit["job_id"]))
    assert any("wasn't assigned" in p for p in ledger.check_consistency())
