"""Emergency pause for incidents (`pause-v1`): `POST /v1/operator/pause` and
`python -m app.incident`. While paused nothing new is handed out, created or enrolled,
in-flight results still count, and only someone on the server can resume."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import incident, workers
from app.config import Settings
from app.ledger import CoordinatorPaused, Ledger
from app.ledger_copy import export_copy
from app.main import create_app
from app.rehearsal import Rehearsal, local_coordinator
from tests.auth_helpers import WorkerClient, enroll, enrollment_code
from tests.test_assignment import assigned, make as make_assign, other
from tests.test_audits import (OP, OPERATOR, Clock, claim, llm_payload,
                               pinned, run_llm_job, submit)  # noqa: F401

REASON = "rig-b key may have leaked in a screenshot"
WS = ("wk-a", "wk-b", "wk-c")


def make(tmp_path: Path, clock: Clock | None = None):
    clock = clock or Clock()
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, audit_rate=0.0, audit_rate_new=0.0,
                        audit_rate_suspect=0.0, operator_token=OPERATOR,
                        rate_limit_per_minute=0)
    app = create_app(settings, clock=clock)
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b"):
        enroll(client, w)
    return client, clock, app.state.ledger


def pause(client, reason: str = REASON):
    return client.post("/v1/operator/pause", json={"reason": reason}, headers=OP)


def queue_job(client, payload: str = "hello"):
    return client.post("/v1/jobs", json={"payload": payload})


# -- what a pause stops, and what it doesn't -------------------------------------------
def test_pause_stops_claims_job_creation_and_enrollment(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    assert queue_job(client).status_code == 200
    code = enrollment_code(client, "owner-new")  # made before the pause
    r = pause(client)
    assert r.status_code == 200 and r.json()["already_paused"] is False

    got = claim(client, "wk-a")
    assert got.status_code == 503
    assert got.headers["Retry-After"] == "60"
    assert got.json()["paused_since"] == r.json()["since"]
    assert REASON not in got.text  # the reason is operator-only
    assert queue_job(client).status_code == 503  # operator token too
    challenge = client.post("/v1/challenges", headers=OP, json={
        "payload": "x", "expected_result": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db"
                                          "02258717921a4881"})
    assert challenge.status_code == 503
    assert client.post("/v1/enrollments", json={"owner": "x"}, headers=OP).status_code == 503
    reg = client.post("/v1/workers/register", json={
        "worker_id": "wk-new", "capabilities": ["demo:sha256"], "enrollment_code": code})
    assert reg.status_code == 503
    assert ledger.status_counts() == {"queued": 1}  # nothing leased, nothing added

    # Still allowed: revoking a rig, and a known rig updating what it can run.
    assert client.post("/v1/workers/wk-b/revoke", headers=OP).status_code == 200
    upd = client.post("/v1/workers/register",
                      json={"worker_id": "wk-a", "capabilities": ["demo:sha256"]},
                      headers={"Authorization": f"Bearer {client.worker_keys['wk-a']}"})
    assert upd.status_code == 200

    # After resume the code made before the pause still works (a refusal never uses it).
    assert ledger.resume(int(_now(client)), "checked", by="test") is not None
    reg = client.post("/v1/workers/register", json={
        "worker_id": "wk-new", "capabilities": ["demo:sha256"], "enrollment_code": code})
    assert reg.status_code == 200, reg.text
    assert claim(client, "wk-a").status_code == 200
    assert ledger.check_consistency() == []


def _now(client) -> int:
    return client.app.state.now()


def test_result_for_a_job_leased_before_the_pause_is_still_accepted(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    queue_job(client, "abc")
    job = claim(client, "wk-a").json()
    pause(client)
    import hashlib
    answer = hashlib.sha256(b"abc").hexdigest()
    r = submit(client, job["job_id"], "wk-a", answer)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert ledger.total_points() == 1


def test_pausing_twice_keeps_the_first_time_and_reason(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    first = pause(client).json()
    clock.t += 50
    again = pause(client, "second alarm").json()
    assert again["already_paused"] is True and again["since"] == first["since"]
    assert ledger.pause_state()["reason"] == REASON


def test_pause_needs_the_operator_token_and_there_is_no_resume_endpoint(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    assert client.post("/v1/operator/pause", json={"reason": "x"}).status_code in (401, 403)
    wrong = client.post("/v1/operator/pause", json={"reason": "x"},
                        headers={"X-Operator-Token": "w" * 40})
    assert wrong.status_code == 403
    assert ledger.pause_state() is None
    pause(client)
    for path in ("/v1/operator/resume", "/v1/operator/pause"):
        assert client.delete(path, headers=OP).status_code in (404, 405)
    assert client.post("/v1/operator/resume", headers=OP).status_code == 404
    assert ledger.pause_state() is not None


@pytest.mark.parametrize("reason", ["", "   ", "x" * 201, "bad\x00byte"])
def test_reason_must_be_short_printable_text(tmp_path: Path, reason: str) -> None:
    client, _, ledger = make(tmp_path)
    assert pause(client, reason).status_code == 422
    assert ledger.pause_state() is None


def test_pause_survives_a_restart(tmp_path: Path) -> None:
    client, clock, _ = make(tmp_path)
    queue_job(client)
    pause(client)
    restarted = WorkerClient(create_app(client.app.state.settings, clock=clock),
                             keys=client.worker_keys)
    assert claim(restarted, "wk-a").status_code == 503
    assert restarted.get("/health").json()["status"] == "paused"


def test_health_pages(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    client, clock, _ = make(tmp_path)
    caplog.set_level(logging.INFO)
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["accepting_work"] is True and h["paused_since"] is None
    op = client.get("/v1/operator/health", headers=OP).json()
    assert op["pause"] == {"rule": "pause-v1", "paused": False}
    since = pause(client).json()["since"]
    clock.t += 30
    r = client.get("/health")
    assert r.status_code == 200  # paused is not "down": results are still accepted
    assert r.json()["status"] == "paused" and r.json()["accepting_work"] is False
    assert r.json()["paused_since"] == since and REASON not in r.text
    op = client.get("/v1/operator/health", headers=OP).json()
    assert op["status"] == "attention"
    assert op["pause"] == {"rule": "pause-v1", "paused": True, "since": since,
                           "reason": REASON, "by": "api"}
    assert "PAUSED" in op["warnings"][0] and REASON in op["warnings"][0]
    assert "30 s" in op["warnings"][0]
    assert REASON not in caplog.text  # never in a log line
    assert any(getattr(rec, "by", None) == "api" for rec in caplog.records)


# -- audits and the pause ----------------------------------------------------------------
@pytest.mark.usefixtures("pinned")
def test_pause_longer_than_the_pick_window_is_not_a_drop(tmp_path: Path) -> None:
    client, clock, ledger = make_assign(tmp_path, WS, assign=60)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    pick = assigned(ledger, original)
    assert ledger.pause(int(clock.t), REASON, by="test")[0]
    clock.t += 200  # longer than the 60 s pick window, inside the job's 300 s deadline
    for w in WS:  # rigs keep asking during the pause; they stay "active"
        assert claim(client, w).status_code == 503
        assert ledger.get_worker(w)["last_seen_at"] == clock.t
    done = ledger.resume(int(clock.t), "all clear", by="test")
    assert done == {"paused_since": int(clock.t) - 200, "paused_seconds": 200,
                    "assignments_extended": 1}
    assert assigned(ledger, original) == pick  # same rig, fresh window
    audit = claim(client, pick)
    assert audit.status_code == 200
    assert submit(client, audit.json()["job_id"], pick, "The Salty Hoe").status_code == 200
    trust = client.get(f"/v1/workers/{pick}/trust", headers=OP).json()
    assert trust["audit_follow_through"]["dropped"] == 0
    assert ledger.check_consistency() == []


@pytest.mark.usefixtures("pinned")
def test_without_the_fresh_window_the_pick_would_be_counted(tmp_path: Path) -> None:
    """Control for the test above: the same wait with no pause is a 'not picked up'."""
    client, clock, ledger = make_assign(tmp_path, WS, assign=60)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    pick = assigned(ledger, original)
    clock.t += 61
    claim(client, other(WS, "wk-a", pick))
    trust = client.get(f"/v1/workers/{pick}/trust", headers=OP).json()
    assert trust["audit_follow_through"]["not_claimed"] == 1


@pytest.mark.usefixtures("pinned")
def test_audit_queued_during_a_pause_is_assigned_only_on_resume(tmp_path: Path) -> None:
    client, clock, ledger = make_assign(tmp_path, WS)
    client.post("/v1/jobs", json={"task_type": "llm:generate", "payload": llm_payload()})
    job = claim(client, "wk-a").json()
    ledger.pause(int(clock.t), REASON, by="test")
    assert submit(client, job["job_id"], "wk-a", "The Salty Hoe").status_code == 200
    assert assigned(ledger, job["job_id"]) is None  # queued, but nobody picked yet
    done = ledger.resume(int(clock.t) + 5, "ok", by="test")
    assert done["assignments_extended"] == 0
    assert assigned(ledger, job["job_id"]) in ("wk-b", "wk-c")
    assert ledger.check_consistency() == []


@pytest.mark.usefixtures("pinned")
def test_a_pick_that_already_took_its_job_gets_no_second_window(tmp_path: Path) -> None:
    client, clock, ledger = make_assign(tmp_path, WS, assign=60)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    pick = assigned(ledger, original)
    assert claim(client, pick).status_code == 200  # took it...
    ledger.pause(int(clock.t), REASON, by="test")
    clock.t += 31  # ...and its lease runs out during the pause
    ledger.sweep(int(clock.t))
    assert ledger.resume(int(clock.t), "ok", by="test")["assignments_extended"] == 0


# -- the CLI --------------------------------------------------------------------------------
def test_cli_pause_status_resume(tmp_path: Path, capsys: pytest.CaptureFixture,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    client, clock, ledger = make(tmp_path)
    # The CLI reads the server clock; this ledger lives at the tests' fake time
    # (`ledger-clock-v1` refuses a clock far behind the ledger).
    monkeypatch.setattr(incident.time, "time", lambda: float(clock.t))
    queue_job(client)
    db = ["--db", str(ledger.path)]
    assert incident.main(db + ["status"]) == 0
    assert "RUNNING" in capsys.readouterr().out
    assert incident.main(db + ["pause", "--reason", REASON]) == 0
    assert "PAUSED" in capsys.readouterr().out
    assert claim(client, "wk-a").status_code == 503  # the running app sees it at once
    assert incident.main(db + ["status", "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["paused"] is True and state["by"] == "cli" and state["reason"] == REASON
    assert incident.main(db + ["resume", "--note", "  "]) == 2
    assert incident.main(db + ["resume", "--note", "rig-b revoked"]) == 0
    assert "RESUMED" in capsys.readouterr().out
    assert claim(client, "wk-a").status_code == 200
    assert incident.main(db + ["resume", "--note", "again"]) == 1
    hist = incident.status(ledger, _now(client))["history"]
    assert [h["event"] for h in hist] == ["coordinator_paused", "coordinator_resumed"]
    assert hist[1]["note"] == "rig-b revoked"


def test_cli_never_creates_a_database(tmp_path: Path) -> None:
    missing = tmp_path / "typo.sqlite3"
    assert incident.main(["--db", str(missing), "pause", "--reason", "x"]) == 2
    assert not missing.exists()


def test_workers_cli_refuses_codes_while_paused(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    _, clock, ledger = make(tmp_path)
    monkeypatch.setattr(workers.time, "time", lambda: float(clock.t))
    ledger.pause(int(clock.t), REASON, by="test")
    assert workers.main(["--db", str(ledger.path), "enroll", "--owner", "x"]) == 3
    with pytest.raises(CoordinatorPaused):
        ledger.create_enrollment("h" * 64, "x", int(clock.t), int(clock.t) + 60)


# -- records ---------------------------------------------------------------------------------
def test_ledger_copy_keeps_that_a_pause_happened_but_not_why(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    pause(client)
    clock.t += 10
    ledger.resume(int(clock.t), "secret fix detail", by="test")
    clock.t += 10
    export_copy(ledger.path, tmp_path / "copy.sqlite3", int(clock.t), int(clock.t) + 1)
    with closing(sqlite3.connect(tmp_path / "copy.sqlite3")) as db:
        rows = db.execute("SELECT event, detail FROM events WHERE event LIKE 'coordinator_%' "
                          "ORDER BY id").fetchall()
    assert rows == [("coordinator_paused", None), ("coordinator_resumed", None)]
    raw = (tmp_path / "copy.sqlite3").read_bytes()
    assert REASON.encode() not in raw and b"secret fix detail" not in raw


def test_old_database_without_pause_events_runs_normally(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "db.sqlite3")
    assert ledger.pause_state() is None and ledger.pause_history() == []
    assert Ledger(ledger.path, read_only=True).pause_state() is None


def test_rehearsal_refuses_a_paused_coordinator() -> None:
    with local_coordinator() as (client, token, clock):
        client.app.state.ledger.pause(int(clock()), "upkeep", by="test")
        report = Rehearsal(client, token, now=clock, wait=clock.advance).run()
    assert "paused" in report["aborted"] and report["all_passed"] is False


def test_a_broken_pause_record_reports_down_not_a_crash(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    with closing(sqlite3.connect(ledger.path)) as db:  # a hand-edited, unreadable record
        db.execute("INSERT INTO events(event, detail, at) VALUES('coordinator_paused', "
                   "'not json', ?)", (int(clock.t),))
        db.commit()
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "down"
