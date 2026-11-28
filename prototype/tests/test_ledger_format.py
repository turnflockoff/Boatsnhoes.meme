"""`ledger-format-v1` (app/ledger_format.py): which coordinator version may open the ledger.

Before (shown on the old code, docs/BUILD-LOG.md 0.5.68): an older coordinator opened a
ledger written by newer code without a word and skipped the newer rules (an aimed hidden
test went to the wrong rig, new work was taken during a pause, a restore ran under a
coordinator that didn't take the "in use" lock); today's code opened a "future" ledger the
same way; and two programs upgrading an old ledger at once crashed with "duplicate column
name" (7 of 80 opens), because the upgrade wasn't one step."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from app import backup, ledger, ledger_format, run_lock
from app.config import Settings
from app.ledger import MIGRATIONS, Ledger
from app.ledger_copy import export_copy
from app.main import create_app
from tests.auth_helpers import OP

PROTOTYPE = Path(__file__).resolve().parents[1]
T = 1_800_000_000

# The Phase 1 ledger (0.1, commit 37587b4): what a ledger from before every migration holds.
PHASE1_SCHEMA = """
CREATE TABLE workers (worker_id TEXT PRIMARY KEY, capabilities TEXT NOT NULL,
    registered_at INTEGER NOT NULL);
CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL, reward_points INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    signature TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN
        ('queued','leased','accepted','rejected','expired','failed')),
    worker_id TEXT, lease_expires_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX jobs_status_created ON jobs(status, created_at);
CREATE TABLE submissions (job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    worker_id TEXT NOT NULL, result_hash TEXT NOT NULL, reward_points INTEGER NOT NULL,
    accepted_at INTEGER NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, worker_id TEXT,
    event TEXT NOT NULL, detail TEXT, at INTEGER NOT NULL);
INSERT INTO workers VALUES ('wk-old', '["sha256"]', 1700000000);
INSERT INTO jobs VALUES ('job-old', 'sha256', 'x', 'h', 1, 1700009000, 'sig', 'accepted',
    'wk-old', NULL, 1, 3, 1700000100, 1700000200);
INSERT INTO submissions VALUES ('job-old', 'wk-old', 'r', 1, 1700000200);
INSERT INTO events(job_id, worker_id, event, detail, at)
    VALUES ('job-old', 'wk-old', 'job_accepted', NULL, 1700000150);
"""
PHASE1_NEWEST = 1700000200  # the newest time in it (the job's update / the result)


def private(tmp_path: Path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder / "db.sqlite3"


def phase1_ledger(tmp_path: Path) -> Path:
    path = private(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.executescript(PHASE1_SCHEMA)
    os.chmod(path, 0o600)
    return path


def fresh_ledger(tmp_path: Path) -> Path:
    path = private(tmp_path)
    Ledger(path).close()
    return path


def future_ledger(tmp_path: Path, version: int = ledger_format.FORMAT + 1) -> Path:
    """A ledger from a later version: one more rule (a column) and a higher format."""
    path = fresh_ledger(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("ALTER TABLE workers ADD COLUMN quarantined_at INTEGER")
        db.execute(f"PRAGMA user_version = {version}")
        db.execute("PRAGMA journal_mode=DELETE")  # so switching it to WAL would show
    return path


def user_version(path: Path) -> int:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        return ledger_format.read(db)


def columns(path: Path, table: str) -> list[str]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def layout_hash(path: Path) -> str:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        return ledger_format.fingerprint(db)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- the format number and the frozen layout --------------------------------------------
def test_a_fresh_ledger_says_its_format_and_matches_the_frozen_layout(tmp_path):
    path = fresh_ledger(tmp_path)
    assert user_version(path) == ledger_format.FORMAT
    assert layout_hash(path) == ledger_format.FINGERPRINTS[ledger_format.FORMAT], (
        "The ledger's tables, columns or indexes changed. Raise ledger_format.FORMAT, add the "
        "new fingerprint to FINGERPRINTS (keep the old ones), and say in the module what "
        "the new format adds. Older code must refuse the new ledger.")
    # A brand-new ledger has nothing to upgrade, so nothing about it is written.
    assert [e["event"] for e in Ledger(path).events()] == []


def test_every_format_has_a_fingerprint_and_old_ones_are_kept():
    assert sorted(ledger_format.FINGERPRINTS) == list(range(1, ledger_format.FORMAT + 1))
    assert all(len(h) == 64 for h in ledger_format.FINGERPRINTS.values())


def test_reopening_a_current_ledger_writes_nothing(tmp_path):
    path = fresh_ledger(tmp_path)
    for _ in range(3):
        Ledger(path).close()
    assert user_version(path) == ledger_format.FORMAT
    assert Ledger(path, read_only=True).events() == []


# -- upgrades ----------------------------------------------------------------------------
def test_a_phase1_ledger_is_upgraded_to_exactly_the_fresh_layout(tmp_path):
    old = phase1_ledger(tmp_path)
    assert user_version(old) == 0
    lg = Ledger(old)
    lg.close()
    assert user_version(old) == ledger_format.FORMAT
    assert layout_hash(old) == ledger_format.FINGERPRINTS[ledger_format.FORMAT]
    assert lg.get_job("job-old")["status"] == "accepted"  # the data is still there
    assert lg.check_consistency() == []


def test_the_upgrade_is_written_down_without_moving_the_ledger_clock(tmp_path):
    old = phase1_ledger(tmp_path)
    lg = Ledger(old)
    lg.close()
    ups = [e for e in lg.events() if e["event"] == ledger_format.UPGRADE_EVENT]
    assert len(ups) == 1
    detail = json.loads(ups[0]["detail"])
    assert detail["from"] == 0 and detail["to"] == ledger_format.FORMAT
    assert detail["rule"] == ledger_format.RULE
    assert ups[0]["at"] == PHASE1_NEWEST  # the newest time already there, not the server's
    assert lg.latest_time() == PHASE1_NEWEST
    Ledger(old).close()  # a second open doesn't upgrade again
    assert [e["event"] for e in lg.events()].count(ledger_format.UPGRADE_EVENT) == 1


def test_an_upgrade_that_fails_part_way_leaves_the_old_ledger_as_it_was(tmp_path, monkeypatch):
    old = phase1_ledger(tmp_path)
    before = {t: columns(old, t) for t in ("workers", "jobs", "submissions")}
    broken = dict(MIGRATIONS)
    broken["jobs"] = MIGRATIONS["jobs"] + (("zz_breaks", "TEXT NOT NULL"),)  # needs a default
    monkeypatch.setattr(ledger, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        Ledger(old)
    assert {t: columns(old, t) for t in before} == before  # not one column added
    assert user_version(old) == 0
    with closing(sqlite3.connect(old)) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='enrollments'"
                          ).fetchone() is None
    assert run_lock.exclusive(old).held  # the failed open let go of its "in use" lock
    monkeypatch.setattr(ledger, "MIGRATIONS", MIGRATIONS)
    Ledger(old).close()
    assert user_version(old) == ledger_format.FORMAT


def test_the_old_upgrade_crashed_when_two_programs_did_it_at_once(tmp_path):
    """Replays the old way, step by step: A looks for missing columns, B upgrades, then A
    adds a column B already added."""
    old = phase1_ledger(tmp_path)
    a = sqlite3.connect(old, isolation_level=None)
    missing = [c for c, _ in MIGRATIONS["workers"]
               if c not in {r[1] for r in a.execute("PRAGMA table_info(workers)")}]
    assert missing
    Ledger(old).close()  # program B
    with pytest.raises(sqlite3.OperationalError, match="duplicate column name"):
        a.execute(f"ALTER TABLE workers ADD COLUMN {missing[0]} TEXT")
    a.close()


def _open(path: str, barrier, results) -> None:
    barrier.wait()
    try:
        Ledger(Path(path)).close()
        results.put("ok")
    except Exception as exc:  # noqa: BLE001 - reported to the test
        results.put(f"{type(exc).__name__}: {exc}")


def test_two_programs_upgrading_at_the_same_moment_both_start(tmp_path):
    ctx = multiprocessing.get_context("spawn")  # fork from a threaded test run can hang
    for i in range(4):
        old = phase1_ledger(tmp_path / f"t{i}")
        barrier, results = ctx.Barrier(3), ctx.Queue()
        procs = [ctx.Process(target=_open, args=(str(old), barrier, results))
                 for _ in range(3)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        assert sorted(results.get(timeout=5) for _ in procs) == ["ok"] * 3
        events = Ledger(old, read_only=True).events()
        assert [e["event"] for e in events].count(ledger_format.UPGRADE_EVENT) == 1


# -- a ledger newer than the code is refused ------------------------------------------------
def test_todays_code_used_to_open_a_future_ledger_without_a_word(tmp_path):
    """The forward case: nothing in the file said which version wrote it, so the old open
    (SCHEMA plus migrations) runs on it and ignores the column it doesn't know."""
    path = future_ledger(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        for statement in Ledger._SCHEMA_STATEMENTS:
            db.execute(statement)
    assert "quarantined_at" in columns(path, "workers")  # still there, still ignored


def test_the_coordinator_refuses_a_ledger_from_a_newer_version(tmp_path):
    path = future_ledger(tmp_path)
    before = sha256(path)
    with pytest.raises(ledger_format.LedgerTooNew) as err:
        Ledger(path)
    msg = str(err.value)
    assert f"format {ledger_format.FORMAT + 1}" in msg
    assert f"format {ledger_format.FORMAT} or older" in msg
    assert "app.backup restore" in msg
    assert sha256(path) == before  # not a byte changed
    assert not Path(f"{path}-wal").exists()  # not even switched to WAL
    assert run_lock.exclusive(path).held  # and its "in use" lock was let go


def test_create_app_does_not_start_on_a_newer_ledger(tmp_path):
    path = future_ledger(tmp_path)
    settings = Settings(database_path=path, operator_token=OP["X-Operator-Token"],
                        signing_key_path=tmp_path / "sign.key")
    with pytest.raises(ledger_format.LedgerTooNew):
        create_app(settings)


def test_read_only_tools_refuse_a_newer_ledger_too(tmp_path):
    path = future_ledger(tmp_path)
    with pytest.raises(ledger_format.LedgerTooNew):
        Ledger(path, read_only=True)


def test_a_ledger_copy_from_a_newer_version_is_refused(tmp_path):
    src = fresh_ledger(tmp_path)
    copy = tmp_path / "copy.sqlite3"
    export_copy(src, copy, T - 100, T)
    assert user_version(copy) == ledger_format.FORMAT  # a copy says its format too
    Ledger(copy, untrusted=True).check_consistency()
    with closing(sqlite3.connect(copy)) as db:
        db.execute(f"PRAGMA user_version = {ledger_format.FORMAT + 1}")
    with pytest.raises(ledger_format.LedgerTooNew, match="this ledger copy"):
        Ledger(copy, untrusted=True)


def test_a_backup_from_a_newer_version_is_not_put_back(tmp_path):
    live = fresh_ledger(tmp_path)
    backup.make_backup(live, tmp_path / "b.sqlite3")
    assert backup.vet(tmp_path / "b.sqlite3")["ledger_format"] == ledger_format.FORMAT
    newer = future_ledger(tmp_path / "other")
    nb = tmp_path / "newer-backup.sqlite3"  # a newer version's backup, copied as-is
    with closing(sqlite3.connect(newer)) as src, closing(sqlite3.connect(nb)) as out:
        src.backup(out)
    os.chmod(nb, 0o600)
    with pytest.raises(backup.BackupError, match="the backup is ledger format"):
        backup.vet(nb)
    before = sha256(live)
    with pytest.raises(backup.BackupError, match="newer coordinator"):
        backup.restore(nb, live, wall=lambda: T)
    assert sha256(live) == before


def test_an_older_backup_is_upgraded_when_it_is_put_back(tmp_path):
    live = fresh_ledger(tmp_path)
    lg = Ledger(live)
    lg.create_enrollment("c", "owner", T, T + 900)
    lg.close()
    b = tmp_path / "b.sqlite3"
    backup.make_backup(live, b)
    with closing(sqlite3.connect(b)) as db:
        db.execute("PRAGMA user_version = 0")  # as if made before `ledger-format-v1`
    assert backup.vet(b)["ledger_format"] == 0
    backup.restore(b, live, wall=lambda: T + 10)
    assert user_version(live) == ledger_format.FORMAT
    events = [e["event"] for e in Ledger(live, read_only=True).events()]
    assert ledger_format.UPGRADE_EVENT in events and backup.RESTORED_EVENT in events


# -- what the operator sees ----------------------------------------------------------------
def test_the_operator_health_page_shows_the_format(tmp_path):
    lg = Ledger(fresh_ledger(tmp_path))
    assert lg.health_snapshot(T)["ledger_format"] == ledger_format.FORMAT
    lg.close()


def _check(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "app.ledger_format", "check", "--db",
                           str(path)], cwd=PROTOTYPE, capture_output=True, text=True,
                          timeout=60)


def test_check_command_on_a_current_ledger(tmp_path):
    r = _check(fresh_ledger(tmp_path))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ledger_format"] == out["this_code_understands"] == ledger_format.FORMAT
    assert out["can_open"] and not out["will_upgrade"] and out["layout_matches"]


def test_check_command_on_an_old_ledger_changes_nothing(tmp_path):
    old = phase1_ledger(tmp_path)
    before = sha256(old)
    r = _check(old)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ledger_format"] == 0 and out["will_upgrade"] and not out["layout_matches"]
    assert sha256(old) == before and user_version(old) == 0


def test_check_command_lists_past_upgrades(tmp_path):
    old = phase1_ledger(tmp_path)
    Ledger(old).close()
    out = json.loads(_check(old).stdout)
    assert [(u["from"], u["to"]) for u in out["upgrades"]] == [(0, ledger_format.FORMAT)]


def test_check_command_on_a_newer_ledger_says_so(tmp_path):
    r = _check(future_ledger(tmp_path))
    assert r.returncode == 1
    assert json.loads(r.stdout)["can_open"] is False
    assert "newer coordinator" in r.stderr


def test_check_command_on_a_missing_or_broken_file(tmp_path):
    assert _check(tmp_path / "nope.sqlite3").returncode == 2
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"not a database at all" * 100)
    r = _check(junk)
    assert r.returncode == 2 and "not a readable ledger" in r.stderr


# -- switching an old-journal ledger (every restored one) to WAL ----------------------------
class FlakyDb:
    """Answers the WAL switch with `errors` first, then works."""

    def __init__(self, *errors: str) -> None:
        self.errors = list(errors)
        self.calls = 0

    def execute(self, sql: str):
        assert sql == "PRAGMA journal_mode=WAL"
        self.calls += 1
        if self.errors:
            raise sqlite3.OperationalError(self.errors.pop(0))
        return self

    def fetchall(self):
        return [("wal",)]


def test_the_wal_switch_waits_when_another_program_is_switching_too():
    db = FlakyDb("database is locked", "database is locked")
    Ledger._use_wal(db)
    assert db.calls == 3


def test_the_wal_switch_gives_up_after_its_wait_and_on_other_errors():
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        Ledger._use_wal(FlakyDb(*["database is locked"] * 1000), wait_seconds=0.1)
    db = FlakyDb("disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        Ledger._use_wal(db)
    assert db.calls == 1


def test_a_restored_ledger_is_in_the_old_journal_mode_and_opens_in_wal(tmp_path):
    live = fresh_ledger(tmp_path)
    backup.make_backup(live, tmp_path / "b.sqlite3")
    backup.restore(tmp_path / "b.sqlite3", live, wall=lambda: T)
    with closing(sqlite3.connect(live)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    Ledger(live).close()
    with closing(sqlite3.connect(live)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_newer_version_upgrading_in_between_is_caught_under_the_lock(tmp_path, monkeypatch):
    """The format is read again once the write lock is held: a newer coordinator that
    upgraded the ledger between our first look and our lock still stops us."""
    old = phase1_ledger(tmp_path)
    real = Ledger._use_wal

    def newer_version_upgrades_meanwhile(db, *a, **k):
        real(db, *a, **k)
        with closing(sqlite3.connect(old)) as other:
            other.execute(f"PRAGMA user_version = {ledger_format.FORMAT + 1}")
            other.commit()

    monkeypatch.setattr(Ledger, "_use_wal", staticmethod(newer_version_upgrades_meanwhile))
    with pytest.raises(ledger_format.LedgerTooNew):
        Ledger(old)
    assert columns(old, "workers") == ["worker_id", "capabilities", "registered_at"]
