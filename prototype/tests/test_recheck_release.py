"""Code audit, twentieth pass (`recheck-release-v1`, threat model #5): re-checks came out in
the second the result they re-check was accepted.

0.5.70 took the ledger-wide event ids off the public job page, and left this open: a re-check
was made inside the transaction that accepted the original result, so

  * its public `job_created` had the same second as the original's `result_accepted`
    (always; an ordinary job shares its second with someone's result only now and then),
  * its signed deadline was "accepted second + the original's window", so even without the
    job page, `expires_at - 300` (the usual window) *was* the accepted second,
  * and it could be taken at once, so the rig that got it knew it was made a moment ago.

Now a new re-check is stored *held*: no public event, not on the public job page, not in
the public counts, nobody can take it, and it doesn't expire. After a random wait
(RECHECK_DELAY_MIN/MAX_SECONDS, default 60-600 s when the coordinator starts normally) the
coordinator's release timer brings it out: its life starts then (`created_at`, the public
`job_created`), its deadline is counted from then and it is signed again over that deadline.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import ledger_copy
from app.config import Settings, get_settings
from app.ledger import Ledger
from app.main import create_app
from app.schemas import SIGNED_FIELDS
from app.signing import verify_manifest
from tests.auth_helpers import FirstPickRng, WorkerClient, enroll, flat_audit_rate
from tests.test_audits import (OPERATOR, T0, Clock, claim, llm_payload, pinned,  # noqa: F401
                               submit)

OP = {"X-Operator-Token": OPERATOR}
LO, HI = 60, 600
WINDOW = 300  # the default ttl of a queued job, so the default window of its re-check


def make(tmp_path: Path, lo: int = LO, hi: int = HI, **overrides):
    clock = Clock()
    fields = dict(database_path=tmp_path / "db.sqlite3", lease_seconds=30, max_attempts=2,
                  audit_rate=1.0, **flat_audit_rate(1.0), operator_token=OPERATOR,
                  public_settle_delay_seconds=0, public_counts_interval_seconds=0,
                  recheck_delay_min_seconds=lo, recheck_delay_max_seconds=hi)
    app = create_app(Settings(**(fields | overrides)), clock=clock, rng=FirstPickRng(1))
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b", "wk-c"):
        enroll(client, w)
    return client, clock, app


def run_original(client, clock, answer: str = "The Salty Hoe", prompt: str = "Name a boat.",
                 worker: str = "wk-a") -> tuple[str, int]:
    """Queue an AI job, have `worker` do it; returns (job id, second it was accepted)."""
    job_id = client.post("/v1/jobs", json={"task_type": "llm:generate",
                                           "payload": llm_payload(prompt)}).json()["job_id"]
    assert claim(client, worker).json()["job_id"] == job_id
    accepted = int(clock.t)
    assert submit(client, job_id, worker, answer).status_code == 200
    return job_id, accepted


def recheck_of(ledger: Ledger, original: str, kind: str = "audit") -> dict:
    return next(a for a in ledger.audits() if a["audit_of"] == original
                and a["job_kind"] == kind)


def created_at_public(client, job_id: str) -> int:
    events = client.get(f"/v1/jobs/{job_id}").json()["events"]
    return next(e["at"] for e in events if e["event"] == "job_created")


# -- the leak, shown with the hold switched off (the old behaviour) ------------------------
def test_without_the_hold_a_recheck_gives_away_the_second_of_its_original(pinned, tmp_path):
    """Guards the tests below: this is the 0.5.70 behaviour, and it really leaks."""
    client, clock, app = make(tmp_path, lo=0, hi=0)
    original, accepted = run_original(client, clock)
    clock.t += 7
    job = claim(client, "wk-b").json()
    assert job["payload"] == llm_payload() and job["job_id"] != original
    assert created_at_public(client, job["job_id"]) == accepted  # same second, every time
    assert job["expires_at"] - WINDOW == accepted  # and the signed deadline says it too
    assert app.state.ledger.next_release() is None


# -- held ----------------------------------------------------------------------------------
def test_a_new_recheck_is_held_out_of_sight(pinned, tmp_path):
    client, clock, app = make(tmp_path)
    ledger = app.state.ledger
    before = client.get("/health").json()
    original, accepted = run_original(client, clock)
    held = recheck_of(ledger, original)
    assert held["status"] == "queued" and LO <= held["release_at"] - accepted <= HI
    # Nobody can take it or see it.
    for w in ("wk-a", "wk-b", "wk-c"):
        assert claim(client, w).status_code == 404
    assert client.get(f"/v1/jobs/{held['job_id']}").status_code == 404
    # Nothing public was written for it: the original's second holds no job_created.
    assert [e["event"] for e in ledger.events(held["job_id"])] == ["recheck_held"]
    assert client.get("/health").json()["jobs"].get("queued", 0) == \
        before["jobs"].get("queued", 0)
    # The operator sees it, and no warning while it is on time.
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["audits"]["waiting_for_release"] == 1 and health["audits"]["open"] == 1
    assert health["audits"]["waiting_for_auditor"] == 0
    assert not any("release time" in w for w in health["warnings"])
    assert ledger.submission(original)["audit_status"] == "pending"


def test_it_comes_out_at_its_own_second_with_a_fresh_signed_deadline(pinned, tmp_path):
    client, clock, app = make(tmp_path)
    ledger, signer = app.state.ledger, app.state.signer
    original, accepted = run_original(client, clock)
    release_at = recheck_of(ledger, original)["release_at"]
    clock.t = release_at - 1
    assert app.state.release_held() == 0 and claim(client, "wk-b").status_code == 404
    clock.t = release_at + 2  # the timer runs a little late
    assert app.state.release_held() == 1
    assert app.state.release_held() == 0  # once only
    job = claim(client, "wk-b").json()
    assert job["payload"] == llm_payload()
    # Its life starts at the release: page, row and signed deadline all say so.
    assert created_at_public(client, job["job_id"]) == release_at + 2 != accepted
    stored = ledger.get_job(job["job_id"])
    assert stored["created_at"] == release_at + 2 and stored["release_at"] is None
    assert job["expires_at"] == release_at + 2 + WINDOW
    assert job["expires_at"] - WINDOW != accepted
    fields = {k: job[k] for k in SIGNED_FIELDS}
    assert verify_manifest(fields, job["signature"], signer.public_key_hex)
    # And the check still does its job.
    clock.t += 20
    assert submit(client, job["job_id"], "wk-b", "The Salty Hoe").status_code == 200
    assert ledger.submission(original)["audit_status"] == "confirmed"


def test_the_wait_is_random_and_within_the_limits(pinned, tmp_path):
    client, clock, app = make(tmp_path)
    waits = []
    for i in range(12):
        clock.t += 1
        original, accepted = run_original(client, clock, prompt=f"Name boat {i}.")
        waits.append(recheck_of(app.state.ledger, original)["release_at"] - accepted)
    assert all(LO <= w <= HI for w in waits)
    assert len(set(waits)) >= 10  # not one fixed delay a rig could subtract


def test_a_held_recheck_does_not_expire_and_the_operator_is_told_when_its_late(pinned,
                                                                               tmp_path):
    client, clock, app = make(tmp_path)
    ledger = app.state.ledger
    original, _ = run_original(client, clock)
    release_at = recheck_of(ledger, original)["release_at"]
    # The release timer is down for an hour: requests keep sweeping, nothing expires.
    clock.t = release_at + 3600
    assert claim(client, "wk-c").status_code == 404
    held = recheck_of(ledger, original)
    assert held["status"] == "queued" and held["release_at"] == release_at
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["audits"]["release_overdue"] == 1
    assert any("release timer isn't running" in w for w in health["warnings"])
    # When it does come out it gets its whole window, counted from then.
    assert app.state.release_held() == 1
    job = claim(client, "wk-c").json()  # the only rig still active after the hour
    assert job["expires_at"] == release_at + 3600 + WINDOW


def test_tie_breakers_are_held_too(pinned, tmp_path):
    client, clock, app = make(tmp_path)
    ledger = app.state.ledger
    original, _ = run_original(client, clock)
    clock.t = recheck_of(ledger, original)["release_at"]
    app.state.release_held()
    audit = claim(client, "wk-b").json()["job_id"]
    clock.t += 5
    disputed_at = int(clock.t)
    assert submit(client, audit, "wk-b", "Something else entirely").status_code == 200
    tb = recheck_of(ledger, original, "tiebreak")
    assert LO <= tb["release_at"] - disputed_at <= HI
    assert claim(client, "wk-c").status_code == 404
    clock.t = tb["release_at"]
    assert app.state.release_held() == 1
    job = claim(client, "wk-c").json()
    assert job["job_id"] == tb["job_id"] and job["expires_at"] == tb["release_at"] + WINDOW
    assert created_at_public(client, tb["job_id"]) == tb["release_at"] != disputed_at


def test_a_period_with_a_held_recheck_waits_before_it_can_be_shared(pinned, tmp_path):
    client, clock, app = make(tmp_path)
    original, _ = run_original(client, clock)
    clock.t += 1
    with pytest.raises(ledger_copy.CopyError, match="still queued"):
        ledger_copy.export_copy(app.state.settings.database_path, tmp_path / "copy.sqlite3",
                                until=int(clock.t), now=int(clock.t))


def test_the_ledger_itself_keeps_a_held_recheck_back(pinned, tmp_path):
    """Second fences in the ledger, below the coordinator's own checks: release_due never
    releases early, and a held re-check can't be leased even if it somehow had a pick."""
    client, clock, app = make(tmp_path)
    ledger = app.state.ledger
    original, _ = run_original(client, clock)
    held = recheck_of(ledger, original)

    def never(job, t):
        raise AssertionError("released early")
    assert ledger.release_due(held["release_at"] - 1, never) == 0
    with ledger._tx() as db:
        db.execute("UPDATE jobs SET assigned_worker='wk-b', assigned_until=? WHERE job_id=?",
                   (held["release_at"] + 999, held["job_id"]))
    assert ledger.claim_next("wk-b", 30, int(clock.t) + 1) is None


def test_only_rechecks_can_be_held(tmp_path):
    ledger = Ledger(tmp_path / "db.sqlite3")
    fields = {"job_id": "j1", "task_type": "demo:sha256", "payload": "x", "payload_hash": "h",
              "reward_points": 1, "expires_at": T0 + 300, "schema_version": "bnh-job/1",
              "model_sha256": None}
    with pytest.raises(ValueError, match="only re-checks"):
        with ledger._tx() as db:
            ledger._insert_job(db, fields, "sig", 2, T0, "work", release_at=T0 + 60)
    assert ledger.get_job("j1") is None


# -- the release timer ---------------------------------------------------------------------
def wait_until(check, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_the_timer_releases_rechecks_on_its_own_and_stops_with_the_app(pinned, tmp_path):
    client, clock, app = make(tmp_path, recheck_release_tick_seconds=0.02)
    ledger = app.state.ledger
    with client:  # runs the app's startup and shutdown, like uvicorn
        original, _ = run_original(client, clock)
        release_at = recheck_of(ledger, original)["release_at"]
        time.sleep(0.1)
        assert recheck_of(ledger, original)["release_at"] == release_at  # not yet due
        clock.t = release_at
        assert wait_until(lambda: recheck_of(ledger, original)["release_at"] is None)
        assert claim(client, "wk-b").json()["payload"] == llm_payload()
    import threading
    assert not any(t.name == "bnh-recheck-release" for t in threading.enumerate())


def test_the_timer_keeps_going_after_an_error(pinned, tmp_path):
    client, clock, app = make(tmp_path, recheck_release_tick_seconds=0.02)
    real, calls = app.state.release_held, []

    def flaky() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return real()
    app.state.release_held = flaky
    with client:
        original, _ = run_original(client, clock)
        clock.t = recheck_of(app.state.ledger, original)["release_at"]
        assert wait_until(lambda: recheck_of(app.state.ledger, original)["release_at"] is None)
    assert len(calls) >= 2


def test_no_timer_without_a_tick(pinned, tmp_path):
    import threading
    client, _, _ = make(tmp_path)
    with client:
        assert not any(t.name == "bnh-recheck-release" for t in threading.enumerate())


# -- settings ------------------------------------------------------------------------------
def test_the_coordinator_holds_rechecks_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    for name in ("RECHECK_DELAY_MIN_SECONDS", "RECHECK_DELAY_MAX_SECONDS",
                 "RECHECK_RELEASE_TICK_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    s = get_settings()
    assert (s.recheck_delay_min_seconds, s.recheck_delay_max_seconds) == (60, 600)
    assert s.recheck_release_tick_seconds == 1
    monkeypatch.setenv("RECHECK_DELAY_MAX_SECONDS", "0")  # one setting turns it off
    s = get_settings()
    assert (s.recheck_delay_min_seconds, s.recheck_delay_max_seconds) == (0, 0)
    monkeypatch.setenv("RECHECK_DELAY_MAX_SECONDS", "30")
    assert get_settings().recheck_delay_min_seconds == 30


def test_settings_refuse_a_minimum_above_the_maximum(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="RECHECK_DELAY_MIN_SECONDS"):
        Settings(database_path=tmp_path / "x", recheck_delay_min_seconds=10,
                 recheck_delay_max_seconds=5)
    with pytest.raises(ValueError, match="TICK"):
        Settings(database_path=tmp_path / "x", recheck_release_tick_seconds=-1)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("RECHECK_DELAY_MIN_SECONDS", "700")
    monkeypatch.setenv("RECHECK_DELAY_MAX_SECONDS", "600")
    with pytest.raises(ValueError, match="RECHECK_DELAY_MIN_SECONDS"):
        get_settings()
