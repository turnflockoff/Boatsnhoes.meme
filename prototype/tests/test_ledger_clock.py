"""`ledger-clock-v1` (app/ledger_clock.py): the coordinator never writes a time earlier than
one it already wrote, refuses to start on a clock far behind the ledger, and notices clock
steps while running.

Before: a server clock set back 3 minutes after period 1 was closed, signed and shared put a
new result inside period 1 (the signed report no longer matched, and `ledger_copy compare`
called the honest operator a back-dater); a server that rebooted in 2000 started anyway and
a crashed rig's lease stayed "leased" for 27 years."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app import incident, ledger_clock, ledger_copy, rewards, workers
from app.config import Settings, get_settings
from app.ledger import REPORT_SIGNED_EVENT, Ledger
from app.ledger_clock import ClockBehindError, LedgerClock, check_behind, tool_now
from app.ledger_compare import compare_copies
from app.ledger_copy import export_copy
from app.main import create_app
from tests.auth_helpers import FirstPickRng, WorkerClient, enroll, receipt
from tests.test_audits import OP, OPERATOR, T0, Clock, claim, sha

Y2000 = 946_684_800


def settings(tmp_path: Path, **extra) -> Settings:
    return Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=60, audit_rate=0.0,
                    audit_rate_new=0.0, audit_rate_suspect=0.0, operator_token=OPERATOR,
                    public_settle_delay_seconds=0, rate_limit_per_minute=0, **extra)


def start(tmp_path: Path, clock: Clock, keys: dict | None = None, steady=None, **extra):
    app = create_app(settings(tmp_path, **extra), clock=clock, rng=FirstPickRng(1),
                     steady_clock=steady)
    return WorkerClient(app, keys)


def submit(client, job_id: str, worker: str, result: str):
    t = client.app.state.now()
    return client.post(f"/v1/jobs/{job_id}/submit", json={
        "worker_id": worker, "result": result, "result_hash": sha(result),
        "started_at": t, "completed_at": t, **receipt(client, job_id)})


def job(client, worker: str, payload: str) -> str:
    assert client.post("/v1/jobs", json={"payload": payload, "reward_points": 3}
                       ).status_code == 200
    job_id = claim(client, worker).json()["job_id"]
    r = submit(client, job_id, worker, sha(payload))
    assert r.status_code == 200, r.text
    return job_id


def health(client) -> dict:
    r = client.get("/v1/operator/health", headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


def min_times(ledger: Ledger, after_event_id: int) -> int:
    """Smallest time written by anything after event `after_event_id`."""
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(ledger.path)) as db:
        return db.execute("SELECT MIN(at) FROM events WHERE id>?", (after_event_id,)
                          ).fetchone()[0]


def last_event_id(ledger: Ledger) -> int:
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(ledger.path)) as db:
        return db.execute("SELECT MAX(id) FROM events").fetchone()[0]


# -- the reported bug: a clock set back after a period was signed and shared ----------------
def test_clock_set_back_after_a_signed_shared_period_writes_nothing_into_it(
        tmp_path: Path) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    for w in ("wk-a", "wk-b"):
        enroll(client, w)
    job(client, "wk-a", "period one")
    until = T0 + 100
    clock.t = T0 + 120
    signed = client.get("/v1/rewards/report", params={"until": until, "sign": "true"},
                        headers=OP).json()
    old = tmp_path / "copy-1.sqlite3"
    export_copy(tmp_path / "db.sqlite3", old, until, int(clock.t))

    clock.t = T0 + 120 - 180  # NTP steps back 3 minutes; the coordinator restarts
    client = start(tmp_path, clock, client.worker_keys)
    assert client.app.state.now() == T0 + 120  # held at the newest recorded time
    late = job(client, "wk-b", "period two work")
    assert client.app.state.ledger.submission(late)["accepted_at"] >= until
    assert client.app.state.ledger.get_job(late)["created_at"] >= until
    # The signed period-1 report still matches the ledger.
    assert rewards.build_report(client.app.state.ledger, 0, until)["report_sha256"] \
        == signed["report_sha256"]
    assert rewards.verify_report(signed, client.app.state.ledger,
                                 client.app.state.signer.public_key_hex) == []
    clock.t = T0 + 400
    new = tmp_path / "copy-2.sqlite3"
    export_copy(tmp_path / "db.sqlite3", new, T0 + 300, int(clock.t))
    result = compare_copies(old, new)
    assert result["ok"], result["problems"]


def test_operator_health_says_time_is_held(tmp_path: Path) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    clock.t = T0 - 100
    client = start(tmp_path, clock, client.worker_keys)
    h = health(client)
    assert h["clock"]["rule"] == "ledger-clock-v1"
    assert h["clock"]["held_seconds"] == 100 and h["clock"]["held_at_start_seconds"] == 100
    assert h["clock"]["coordinator_time"] == T0 and h["clock"]["server_clock"] == T0 - 100
    assert any("holding its time" in w for w in h["warnings"])
    # Once real time passes the ledger, the coordinator follows the clock again.
    clock.t = T0 + 5
    h = health(client)
    assert h["clock"]["held_seconds"] == 0 and h["clock"]["coordinator_time"] == T0 + 5
    assert not any("holding its time" in w for w in h["warnings"])


# -- refusing to start on a clearly wrong clock ----------------------------------------------
def test_refuses_to_start_on_a_clock_reset_to_2000(tmp_path: Path) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    assert client.post("/v1/jobs", json={"payload": "x"}).status_code == 200
    assert claim(client, "wk-a").status_code == 200  # then the rig dies
    clock.t = Y2000
    with pytest.raises(ClockBehindError) as exc:
        start(tmp_path, clock)
    msg = str(exc.value)
    assert "2000-01-01" in msg and "behind the newest time" in msg
    assert "timedatectl" in msg and "CLOCK_BEHIND_LIMIT_SECONDS" in msg


def test_behind_limit_boundary_and_explicit_override(tmp_path: Path) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    clock.t = T0 - 300  # exactly the limit: starts, holds
    assert start(tmp_path, clock).app.state.now() == T0
    clock.t = T0 - 301
    with pytest.raises(ClockBehindError):
        start(tmp_path, clock)
    # Raising the limit on purpose lets it start, still holding time at the ledger's.
    c = start(tmp_path, clock, clock_behind_limit_seconds=3600)
    assert c.app.state.now() == T0
    # 0 = refuse any start behind the ledger.
    clock.t = T0 - 1
    with pytest.raises(ClockBehindError):
        start(tmp_path, clock, clock_behind_limit_seconds=0)


def test_empty_ledger_starts_at_any_time(tmp_path: Path) -> None:
    clock = Clock()
    clock.t = Y2000
    client = start(tmp_path, clock)
    assert client.app.state.now() == Y2000
    assert client.app.state.ledger.latest_time() is None or \
        client.app.state.ledger.latest_time() <= Y2000


def test_check_behind_edges() -> None:
    check_behind(100, None, 0)
    check_behind(100, 100, 0)
    check_behind(100, 150, 50)
    with pytest.raises(ClockBehindError):
        check_behind(100, 151, 50)
    check_behind(10**12, 5, 0)  # ahead of the ledger is always fine


# -- while running ----------------------------------------------------------------------------
def test_clock_set_back_while_running_never_goes_backwards(tmp_path: Path) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    clock.t = T0 + 500
    job(client, "wk-a", "first")
    mark = last_event_id(client.app.state.ledger)
    clock.t = T0 + 200  # set back 5 minutes while running
    second = job(client, "wk-a", "second")
    assert min_times(client.app.state.ledger, mark) == T0 + 500
    assert client.app.state.ledger.get_job(second)["created_at"] == T0 + 500
    clock.t = T0 + 600  # caught up: follows the clock again
    assert client.app.state.now() == T0 + 600


def test_follows_the_clock_forwards(tmp_path: Path) -> None:
    """A board that booted with an old clock and is then corrected by NTP must catch up,
    or every honest rig's result would look like it came from the future."""
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    clock.t = T0 + 3600
    assert client.post("/v1/jobs", json={"payload": "x"}).status_code == 200
    job_id = claim(client, "wk-a").json()["job_id"]
    assert submit(client, job_id, "wk-a", sha("x")).status_code == 200
    assert client.app.state.ledger.submission(job_id)["accepted_at"] == T0 + 3600


def test_never_decreases_under_concurrent_readers() -> None:
    values = iter([T0 + (i % 7) * 10 for i in range(10_000)])
    lock = threading.Lock()

    def wall() -> float:
        with lock:
            return float(next(values, T0))

    clk = LedgerClock(wall, wall, latest=None)
    seen: list[list[int]] = [[] for _ in range(4)]

    def read(out: list[int]) -> None:
        for _ in range(1500):
            out.append(clk())

    threads = [threading.Thread(target=read, args=(s,)) for s in seen]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in seen:
        assert s == sorted(s)
    assert max(max(s) for s in seen) == T0 + 60


def test_steps_are_noticed_with_a_steady_clock(tmp_path: Path) -> None:
    clock = Clock()
    steady = [1000.0]
    client = start(tmp_path, clock, steady=lambda: steady[0])
    enroll(client, "wk-a")
    assert health(client)["clock"]["steps_seen"] == 0
    # Normal time passing: both clocks move together, no step.
    clock.t += 30
    steady[0] += 30
    assert health(client)["clock"]["steps_seen"] == 0
    # Small drift under the threshold is not a step.
    clock.t += 40
    steady[0] += 1
    assert health(client)["clock"]["steps_seen"] == 0
    # A one-day jump ahead...
    clock.t += 86400
    steady[0] += 1
    h = health(client)
    assert h["clock"]["steps_seen"] == 1
    assert h["clock"]["recent_steps"][-1]["seconds"] == 86399
    assert any("jumped ahead" in w and "86399" in w for w in h["warnings"])
    # ...and set back again: held, and that step is shown too.
    clock.t -= 86400
    steady[0] += 1
    h = health(client)
    assert h["clock"]["steps_seen"] == 2
    assert h["clock"]["recent_steps"][-1]["seconds"] == -86401
    assert h["clock"]["held_seconds"] == 86400
    assert any("set back" in w and "86401" in w for w in h["warnings"])
    assert any("holding its time" in w for w in h["warnings"])


def test_an_injected_test_clock_is_never_seen_as_stepping(tmp_path: Path) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    clock.t += 10 * 86400
    h = health(client)
    assert h["clock"]["steps_seen"] == 0
    assert not any("jumped" in w for w in h["warnings"])


def test_real_clocks_use_the_monotonic_clock_for_steps() -> None:
    import time
    assert ledger_clock.default_steady(time.time) is time.monotonic
    fake = Clock()
    assert ledger_clock.default_steady(fake) is fake


def test_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR)
    s = get_settings()
    assert (s.clock_behind_limit_seconds, s.clock_step_warn_seconds) == (300, 60)
    monkeypatch.setenv("CLOCK_BEHIND_LIMIT_SECONDS", "3600")
    monkeypatch.setenv("CLOCK_STEP_WARN_SECONDS", "5")
    s = get_settings()
    assert (s.clock_behind_limit_seconds, s.clock_step_warn_seconds) == (3600, 5)
    for bad in ("-1", "abc", str(7 * 86400 + 1)):
        monkeypatch.setenv("CLOCK_BEHIND_LIMIT_SECONDS", bad)
        with pytest.raises(ValueError, match="CLOCK_BEHIND_LIMIT_SECONDS"):
            get_settings()
    monkeypatch.setenv("CLOCK_BEHIND_LIMIT_SECONDS", "300")
    monkeypatch.setenv("CLOCK_STEP_WARN_SECONDS", "0")
    with pytest.raises(ValueError, match="CLOCK_STEP_WARN_SECONDS"):
        get_settings()


# -- the newest recorded time ----------------------------------------------------------------
def test_latest_time_reads_every_table(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "db.sqlite3")
    assert ledger.latest_time() is None
    ledger.create_enrollment("h" * 64, "alice", T0, T0 + 10**6)
    assert ledger.latest_time() == T0  # expires_at is a deadline, not a recorded time
    ledger.revoke_worker("nobody", T0 + 5)  # unknown worker: nothing written
    assert ledger.latest_time() == T0
    ledger.record_copy(T0 + 50, T0, "0" * 64)
    assert ledger.latest_time() == T0 + 50
    ledger.record_signed_report(T0 + 70, 0, T0 + 60, "1" * 64)
    assert ledger.latest_time() == T0 + 70


def test_a_rig_asking_for_work_counts_as_a_recorded_time(tmp_path: Path) -> None:
    """An empty-queue poll writes no event, only the rig's `last_seen_at`."""
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    mark = last_event_id(client.app.state.ledger)
    clock.t = T0 + 500
    assert claim(client, "wk-a").status_code == 404
    assert last_event_id(client.app.state.ledger) == mark
    assert client.app.state.ledger.latest_time() == T0 + 500
    clock.t = T0 + 300
    assert start(tmp_path, clock).app.state.now() == T0 + 500


# -- a signed period's end is a recorded time ------------------------------------------------
def test_signing_records_the_period_end_even_on_a_quiet_ledger(tmp_path: Path) -> None:
    """The newest job is at T0, but a report for [0, T0+900) was signed at T0+1000. A
    restart with the clock at T0+800 is ahead of every job, yet inside the signed period."""
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    job(client, "wk-a", "only job")
    clock.t = T0 + 1000
    signed = client.get("/v1/rewards/report", params={"until": T0 + 900, "sign": "true"},
                        headers=OP).json()
    assert client.app.state.ledger.latest_time() == T0 + 1000
    clock.t = T0 + 800
    client = start(tmp_path, clock, client.worker_keys)
    later = job(client, "wk-a", "after restart")
    assert client.app.state.ledger.get_job(later)["created_at"] == T0 + 1000
    assert rewards.verify_report(signed, client.app.state.ledger) == []


def test_unsigned_reports_record_nothing(tmp_path: Path) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    before = client.app.state.ledger.latest_time()
    clock.t = T0 + 1000
    r = client.get("/v1/rewards/report", params={"until": T0 + 900}, headers=OP)
    assert r.status_code == 200
    assert client.app.state.ledger.latest_time() == before
    # A refused signature (period still open) records nothing either.
    r = client.get("/v1/rewards/report", params={"until": T0 + 5000, "sign": "true"},
                   headers=OP)
    assert r.status_code == 422
    assert client.app.state.ledger.latest_time() == before


def test_signed_event_is_left_out_of_that_periods_copy_and_kept_in_the_next(
        tmp_path: Path) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    job(client, "wk-a", "one")
    clock.t = T0 + 200
    assert client.get("/v1/rewards/report", params={"until": T0 + 100, "sign": "true"},
                      headers=OP).status_code == 200
    first = tmp_path / "c1.sqlite3"
    export_copy(tmp_path / "db.sqlite3", first, T0 + 100, int(clock.t))
    clock.t = T0 + 400
    second = tmp_path / "c2.sqlite3"
    export_copy(tmp_path / "db.sqlite3", second, T0 + 300, int(clock.t))
    from tests.test_ledger_copy import rows
    assert not rows(first, "SELECT id FROM events WHERE event=?", (REPORT_SIGNED_EVENT,))
    kept = rows(second, "SELECT at, detail FROM events WHERE event=?", (REPORT_SIGNED_EVENT,))
    assert [k["at"] for k in kept] == [T0 + 200]
    assert compare_copies(first, second)["ok"]


# -- the operator's own tools follow the same rule -------------------------------------------
def at(monkeypatch: pytest.MonkeyPatch, t: float) -> None:
    import time
    monkeypatch.setattr(time, "time", lambda: float(t))


def test_tool_now_holds_and_refuses() -> None:
    assert tool_now(None, wall=lambda: T0, limit=0) == T0
    assert tool_now(T0 + 50, wall=lambda: T0, limit=300) == T0 + 50
    assert tool_now(T0 - 50, wall=lambda: T0, limit=0) == T0
    with pytest.raises(ClockBehindError):
        tool_now(T0 + 301, wall=lambda: T0, limit=300)


def test_cli_tools_refuse_a_clock_far_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    job(client, "wk-a", "x")
    db = str(tmp_path / "db.sqlite3")
    mark = last_event_id(client.app.state.ledger)
    at(monkeypatch, Y2000)
    assert incident.main(["--db", db, "pause", "--reason", "test"]) == 2
    assert "behind the newest time" in capsys.readouterr().err
    assert workers.main(["--db", db, "enroll", "--owner", "bob"]) == 2
    assert workers.main(["--db", db, "revoke", "wk-a"]) == 2
    assert ledger_copy.main(["export", "--db", db, "--until", str(Y2000 - 10),
                             "--out", str(tmp_path / "c.sqlite3")]) == 2
    assert not (tmp_path / "c.sqlite3").exists()
    key = str(client.app.state.settings.resolved_signing_key_path())
    assert rewards.main(["report", "--db", db, "--sign", "--key-file", key,
                         "--until", str(Y2000 - 10)]) == 2
    assert last_event_id(client.app.state.ledger) == mark  # nothing written
    # Read-only views still work on a wrong clock.
    assert incident.main(["--db", db, "status"]) == 0
    assert workers.main(["--db", db, "list"]) == 0
    # An explicit, larger limit lets them run, still never writing before the ledger.
    monkeypatch.setenv("CLOCK_BEHIND_LIMIT_SECONDS", str(7 * 86400))
    at(monkeypatch, T0 - 600)
    assert incident.main(["--db", db, "pause", "--reason", "test"]) == 0
    assert min_times(client.app.state.ledger, mark) == T0


def test_cli_tools_write_at_the_held_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    clock.t = T0 + 500
    job(client, "wk-a", "x")
    db = str(tmp_path / "db.sqlite3")
    mark = last_event_id(client.app.state.ledger)
    at(monkeypatch, T0 + 400)  # 100 s behind the ledger: within the limit
    assert workers.main(["--db", db, "enroll", "--owner", "bob"]) == 0
    assert incident.main(["--db", db, "pause", "--reason", "test"]) == 0
    assert incident.main(["--db", db, "resume", "--note", "ok"]) == 0
    assert workers.main(["--db", db, "revoke", "wk-a"]) == 0
    assert min_times(client.app.state.ledger, mark) == T0 + 500


def test_cli_signing_records_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture) -> None:
    clock = Clock()
    client = start(tmp_path, clock)
    enroll(client, "wk-a")
    job(client, "wk-a", "x")
    db = str(tmp_path / "db.sqlite3")
    key = str(client.app.state.settings.resolved_signing_key_path())
    at(monkeypatch, T0 + 1000)
    out = tmp_path / "r.json"
    assert rewards.main(["report", "--db", db, "--sign", "--key-file", key,
                         "--until", str(T0 + 900), "--out", str(out)]) == 0
    from tests.test_ledger_copy import rows
    got = rows(tmp_path / "db.sqlite3", "SELECT at, detail FROM events WHERE event=?",
               (REPORT_SIGNED_EVENT,))
    assert len(got) == 1 and got[0]["at"] == T0 + 1000
    detail = json.loads(got[0]["detail"])
    assert detail == {"since": 0, "until": T0 + 900,
                      "report_sha256": json.loads(out.read_text())["report_sha256"]}
    # Unsigned CLI reports and reports on a shared copy write nothing.
    assert rewards.main(["report", "--db", db, "--until", str(T0 + 900)]) == 0
    assert len(rows(tmp_path / "db.sqlite3", "SELECT id FROM events WHERE event=?",
                    (REPORT_SIGNED_EVENT,))) == 1
