"""Threat model #1 and #8: hidden tests aimed at one rig or at a trust tier.

A normal hidden test (challenge) goes to whichever rig asks first, so the operator can't
make sure a brand-new rig, or one that was just caught, actually gets tested. An aimed test
goes only to the rig it names, or to a random active rig in the named `audit-tier-v1` tier,
and looks exactly like normal work to the rig that gets it.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.config import Settings
from app.ledger import Ledger, parse_target
from app.ledger_copy import export_copy
from app.main import create_app
from tests.auth_helpers import FirstPickRng, WorkerClient, enroll
from tests.test_audits import (
    OP,
    Clock,
    audit_status,
    claim,
    llm_payload,
    pinned,  # noqa: F401  (pytest fixture)
    submit,
)

pytestmark = pytest.mark.usefixtures("pinned")

EXPECTED = "The Flagship"


class NoAuditRng(FirstPickRng):
    """Picks are alphabetical (wk-b before wk-c) and audit sampling never fires, so the
    only re-checks in these tests are the hidden tests they create."""

    def random(self) -> float:
        return 0.999


def make(tmp_path: Path, workers=("wk-a", "wk-b", "wk-c"), new_until: int = 1,
         assign: int = 120, active: int = 300):
    clock = Clock()
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, operator_token=OP["X-Operator-Token"],
                        audit_rate=0.25, audit_rate_new=0.5, audit_rate_suspect=0.9,
                        audit_new_until=new_until, audit_assign_seconds=assign,
                        auditor_active_seconds=active, rate_limit_per_minute=0)
    app = create_app(settings, clock=clock, rng=NoAuditRng(1))
    client = WorkerClient(app)
    for w in workers:
        enroll(client, w)
    return client, clock, app.state.ledger


def aim(client: WorkerClient, prompt: str = "Name a ship.", ttl: int = 600, **target):
    return client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload(prompt),
        "expected_result": EXPECTED, "ttl_seconds": ttl, **target})


def take_test(client: WorkerClient, worker: str, answer: str = EXPECTED,
              prompt: str = "Warm-up ship.") -> str:
    """An ordinary first-come hidden test, taken by `worker`. Passing makes a rig
    established (new_until=1); failing makes it a suspect."""
    job_id = aim(client, prompt)
    assert job_id.status_code == 200, job_id.text
    job_id = job_id.json()["job_id"]
    assert claim(client, worker).json()["job_id"] == job_id
    assert submit(client, job_id, worker, answer).status_code == 200
    return job_id


def tier(client: WorkerClient, worker: str) -> str:
    return client.get(f"/v1/workers/{worker}/trust", headers=OP).json()["audit_tier"]


def health(client: WorkerClient) -> dict:
    r = client.get("/v1/operator/health", headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


# -- aimed at one named rig ---------------------------------------------------------------
def test_test_aimed_at_a_rig_goes_only_to_that_rig(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    r = aim(client, target_worker="wk-c")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"] == "worker:wk-c" and body["assigned_worker"] == "wk-c"
    # wk-a and wk-b ask first and as often as they like: nothing for them.
    for _ in range(3):
        assert claim(client, "wk-a").status_code == 404
        assert claim(client, "wk-b").status_code == 404
    job = claim(client, "wk-c").json()
    assert job["job_id"] == body["job_id"]
    assert submit(client, job["job_id"], "wk-c", EXPECTED).status_code == 200
    assert audit_status(ledger, job["job_id"]) == "passed-challenge"
    trust = client.get("/v1/workers/wk-c/trust", headers=OP).json()
    assert trust["challenges_passed"] == 1
    assert client.get("/v1/ledger").json()["consistency_problems"] == []


def test_named_rig_keeps_its_test_while_offline_and_nobody_else_gets_it(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, assign=60, active=300)
    job_id = aim(client, ttl=3000, target_worker="wk-b").json()["job_id"]
    clock.t += 1000  # long past assign_seconds and the active window: wk-b went quiet
    for _ in range(3):
        assert claim(client, "wk-a").status_code == 404
        assert claim(client, "wk-c").status_code == 404
    # No churn: one pick, no "not picked up" entries, no hand-over to someone else.
    names = [e["event"] for e in ledger.events(job_id)]
    assert names.count("test_assigned") == 1 and "test_assignment_expired" not in names
    assert claim(client, "wk-b").json()["job_id"] == job_id  # it comes back: still its test
    clock.t += 31  # and lets the lease run out: it goes back to wk-b, not to anyone else
    assert claim(client, "wk-a").status_code == 404
    assert claim(client, "wk-b").json()["job_id"] == job_id


def test_named_rig_that_never_comes_back_lets_the_test_expire(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    job_id = aim(client, ttl=100, target_worker="wk-b").json()["job_id"]
    clock.t += 101
    assert claim(client, "wk-a").status_code == 404
    assert ledger.get_job(job_id)["status"] == "expired"
    assert ledger.submission(job_id) is None


def test_bad_targets_are_refused(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    assert aim(client, target_worker="wk-nobody").status_code == 404
    assert client.post("/v1/workers/wk-c/revoke", headers=OP).status_code == 200
    assert aim(client, target_worker="wk-c").status_code == 409
    assert aim(client, target_worker="wk-a", target_tier="new").status_code == 422
    assert aim(client, target_tier="vip").status_code == 422
    # Operator-only, like every hidden test.
    r = client.post("/v1/challenges", json={"task_type": "llm:generate",
                                            "payload": llm_payload(), "expected_result": "x",
                                            "target_worker": "wk-a"})
    assert r.status_code == 403


def test_revoking_the_named_rig_leaves_the_test_waiting_with_a_warning(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = aim(client, target_worker="wk-b").json()["job_id"]
    assert health(client)["aimed_tests"] == {"open": 1, "waiting_for_rig": 0}
    assert client.post("/v1/workers/wk-b/revoke", headers=OP).status_code == 200
    assert claim(client, "wk-a").status_code == 404  # never handed to someone else
    assert ledger.get_job(job_id)["assigned_worker"] is None
    h = health(client)
    assert h["aimed_tests"] == {"open": 1, "waiting_for_rig": 1}
    assert any("aimed hidden test(s) waiting for a rig" in w for w in h["warnings"])


# -- aimed at a trust tier ----------------------------------------------------------------
def test_tier_new_skips_established_rigs(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, new_until=1)
    take_test(client, "wk-a")  # wk-a passes one independent check: established
    assert tier(client, "wk-a") == "established" and tier(client, "wk-b") == "new"
    body = aim(client, target_tier="new").json()
    assert body["target"] == "tier:new" and body["assigned_worker"] in ("wk-b", "wk-c")
    assert claim(client, "wk-a").status_code == 404
    other = "wk-c" if body["assigned_worker"] == "wk-b" else "wk-b"
    assert claim(client, other).status_code == 404  # same tier, but not its pick
    assert claim(client, body["assigned_worker"]).json()["job_id"] == body["job_id"]
    assert client.get("/v1/ledger").json()["consistency_problems"] == []


def test_tier_suspect_waits_for_a_suspect_then_goes_to_it(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = aim(client, prompt="Aimed ship.", target_tier="suspect").json()["job_id"]
    assert ledger.get_job(job_id)["assigned_worker"] is None  # nobody has a strike yet
    assert health(client)["aimed_tests"]["waiting_for_rig"] == 1
    # wk-c fails an ordinary hidden test: one strike, so it's a suspect from now on.
    take_test(client, "wk-c", answer="Made up answer")
    assert tier(client, "wk-c") == "suspect"
    assert ledger.get_job(job_id)["assigned_worker"] == "wk-c"
    assert claim(client, "wk-a").status_code == 404
    assert claim(client, "wk-c").json()["job_id"] == job_id
    assert submit(client, job_id, "wk-c", EXPECTED).status_code == 200
    assert audit_status(ledger, job_id) == "passed-challenge"
    assert health(client)["aimed_tests"] == {"open": 0, "waiting_for_rig": 0}


def test_tier_target_only_picks_active_rigs(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, active=300)
    clock.t += 400  # nobody has been seen for 400 s
    claim(client, "wk-c")  # only wk-c asks for work, so only wk-c is active
    body = aim(client, target_tier="new").json()
    assert body["assigned_worker"] == "wk-c"


def test_silent_tier_pick_is_handed_to_another_rig_in_the_tier(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, assign=60)
    body = aim(client, ttl=3000, target_tier="new").json()
    first = body["assigned_worker"]
    assert first == "wk-a"  # alphabetical pick in these tests
    clock.t += 61  # wk-a never picks it up
    assert claim(client, "wk-b").json()["job_id"] == body["job_id"]
    names = [e["event"] for e in ledger.events(body["job_id"])]
    assert names.count("test_assigned") == 2 and "test_assignment_expired" in names
    # A dodged test is recorded privately, but it isn't an audit: follow-through is unchanged.
    trust = client.get("/v1/workers/wk-a/trust", headers=OP).json()
    assert trust["audit_follow_through"]["assigned"] == 0
    assert trust["audit_follow_through"]["dropped"] == 0


# -- hidden, recorded, shareable ----------------------------------------------------------
def test_aimed_test_looks_like_normal_work_to_the_rig(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    assert client.post("/v1/jobs", json={"task_type": "llm:generate",
                                         "payload": llm_payload("Normal.")}).status_code == 200
    normal = claim(client, "wk-a").json()
    aim(client, target_worker="wk-a")
    test = claim(client, "wk-a").json()
    assert set(test) == set(normal)
    public = client.get(f"/v1/jobs/{test['job_id']}").json()
    names = {e["event"] for e in public["events"]}
    assert not names & {"test_assigned", "test_unassigned", "test_assignment_expired"}
    assert "target" not in public and "target" not in test


def test_ledger_copy_hides_what_a_test_was_aimed_at(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    job_id = aim(client, target_tier="new").json()["job_id"]
    worker = ledger.get_job(job_id)["assigned_worker"]
    assert submit(client, claim(client, worker).json()["job_id"], worker,
                  EXPECTED).status_code == 200
    until = int(clock.t) + 1
    clock.t += 10
    out = tmp_path / "copy.sqlite3"
    export_copy(ledger.path, out, until, int(clock.t))
    with closing(sqlite3.connect(out)) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE target IS NOT NULL").fetchone()[0] == 0
    assert b"tier:new" not in out.read_bytes()
    assert Ledger(out, read_only=True).check_consistency() == []


def test_copies_made_before_aimed_tests_still_check_cleanly(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    take_test(client, "wk-a")
    until = int(clock.t) + 1
    clock.t += 10
    out = tmp_path / "old-copy.sqlite3"
    export_copy(ledger.path, out, until, int(clock.t))
    with closing(sqlite3.connect(out)) as db:  # what a 0.5.20 copy looks like
        db.execute("ALTER TABLE jobs DROP COLUMN target")
        db.commit()
    assert Ledger(out, read_only=True).check_consistency() == []


def test_consistency_check_catches_a_test_taken_by_the_wrong_rig(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = aim(client, target_worker="wk-b").json()["job_id"]
    assert submit(client, claim(client, "wk-b").json()["job_id"], "wk-b",
                  EXPECTED).status_code == 200
    assert ledger.check_consistency() == []
    with closing(sqlite3.connect(ledger.path)) as db:  # someone edits the database
        db.execute("UPDATE jobs SET worker_id='wk-a' WHERE job_id=?", (job_id,))
        db.commit()
    problems = ledger.check_consistency()
    assert any("aimed hidden test taken by a worker it wasn't assigned to" in p
               for p in problems)
    assert any("hidden test aimed at one worker taken by another" in p for p in problems)


def test_only_hidden_tests_can_be_aimed(tmp_path: Path) -> None:
    _, _, ledger = make(tmp_path)
    assert parse_target(None) is None
    assert parse_target("worker:wk-a") == ("worker", "wk-a")
    assert parse_target("tier:suspect") == ("tier", "suspect")
    for bad in ("worker:", "tier:vip", "owner:x", "wk-a"):
        with pytest.raises(ValueError):
            parse_target(bad)
    fields = {"job_id": "j1", "task_type": "demo:sha256", "payload": "x", "payload_hash": "h",
              "reward_points": 1, "expires_at": 10**10, "schema_version": "bnh-job/2",
              "model_sha256": None}
    with pytest.raises(ValueError):
        ledger.create_job(fields, "sig", 2, 0, job_kind="work", target="worker:wk-a")


def test_older_database_gains_the_target_column(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    Ledger(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("ALTER TABLE jobs DROP COLUMN target")
        db.commit()
    Ledger(path)  # opening it again migrates in place
    with closing(sqlite3.connect(path)) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    assert "target" in cols
