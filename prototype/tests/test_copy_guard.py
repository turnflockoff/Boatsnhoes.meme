"""Opening a stranger's ledger copy (`copy-open-v1`, app/copy_guard.py).

Copies are published for anyone to download and re-check, so every way a SQLite file can be
more than rows (views, triggers, extra tables, hot journals, damaged pages, giant values)
must be refused before our checks read it, and reading must never change the file."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from app import copy_guard, ledger_copy, publish_report, rewards
from app.copy_guard import UnsafeCopyError
from app.ledger import Ledger
from app.ledger_compare import CompareError, compare_copies
from app.ledger_copy import _file_sha256, copy_info, export_copy
from tests.test_audits import make, pinned  # noqa: F401  (pytest fixture)
from tests.test_ledger_copy import UNTIL, busy_period, signed_report

# A view that shows honest numbers until a date, then different ones. Same file, same
# sha256: verifies today, says something else later (or on someone else's computer).
TIME_BOMB = (
    "CREATE VIEW submissions AS SELECT job_id, worker_id, result_hash, "
    "CASE WHEN date('now') >= '{when}' THEN reward_points + 1000 ELSE reward_points END "
    "AS reward_points, accepted_at, verifier_method, confidence, result, audit_status "
    "FROM real_submissions")


@pytest.fixture
def honest(tmp_path: Path, pinned) -> dict:  # noqa: F811
    client, clock, ledger = make(tmp_path)
    busy_period(client)
    clock.t += 200
    report = signed_report(client)
    path = tmp_path / "copy.sqlite3"
    export_copy(ledger.path, path, UNTIL, int(clock.t))
    return {"copy": path, "report": report, "ledger": ledger, "tmp": tmp_path,
            "key": client.app.state.signer.public_key_hex}


def variant(s: dict, name: str, *sql: str) -> Path:
    """A copy of the honest copy with `sql` run on it (as an attacker would)."""
    path = s["tmp"] / name
    shutil.copy(s["copy"], path)
    with closing(sqlite3.connect(path)) as db:
        for stmt in sql:
            db.execute(stmt)
        db.commit()
    return path


def time_bomb(s: dict, when: str, name: str = "bomb.sqlite3") -> Path:
    return variant(s, name, "ALTER TABLE submissions RENAME TO real_submissions",
                   TIME_BOMB.format(when=when))


def refused(path: Path, match: str = "") -> str:
    with pytest.raises(UnsafeCopyError, match=match) as err:
        copy_info(path)
    with pytest.raises(UnsafeCopyError):
        Ledger(path, untrusted=True)
    return str(err.value)


# --- honest copies ----------------------------------------------------------------------

def test_honest_copy_opens_and_checks_out(honest: dict) -> None:
    before = _file_sha256(honest["copy"])
    info = copy_info(honest["copy"])
    assert info["format"] == ledger_copy.COPY_FORMAT and info["until"] == UNTIL
    copy = Ledger(honest["copy"], untrusted=True)
    assert copy.read_only and copy.check_consistency() == []
    assert rewards.verify_report(honest["report"], copy, honest["key"]) == []
    assert _file_sha256(honest["copy"]) == before
    assert sorted(p.name for p in honest["tmp"].glob("copy.sqlite3*")) == ["copy.sqlite3"]


def test_live_ledgers_are_still_told_apart_from_copies(honest: dict, tmp_path: Path) -> None:
    assert copy_info(honest["ledger"].path) is None
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    assert copy_info(empty) is None
    # A live ledger mid-write in WAL mode (nothing checkpointed yet) is not a copy either.
    live = tmp_path / "wal.sqlite3"
    db = sqlite3.connect(live, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE jobs(x)")
    try:
        assert copy_info(live) is None
    finally:
        db.close()


def test_the_copy_check_cannot_write(honest: dict) -> None:
    with closing(copy_guard.connect(honest["copy"])) as db:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM jobs")
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(f"ATTACH DATABASE '{honest['tmp'] / 'x.db'}' AS x")
    assert not (honest["tmp"] / "x.db").exists()


def test_every_query_has_a_time_limit(honest: dict) -> None:
    slow = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT count(*) FROM c")
    with closing(copy_guard.connect(honest["copy"], seconds=0.3)) as db:
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="interrupt"):
            db.execute(slow).fetchone()
        assert time.monotonic() - start < 5


# --- code inside the file ---------------------------------------------------------------

def test_a_time_bomb_view_is_refused_everywhere(honest: dict, capsys) -> None:
    far, past = time_bomb(honest, "2999-01-01", "far.sqlite3"), time_bomb(
        honest, "2000-01-01", "past.sqlite3")
    # Why it matters: read the way a plain reader would, the same kind of file shows the
    # honest numbers until the date and inflated ones after it.
    plain = Ledger(far, read_only=True)
    assert rewards.verify_report(honest["report"], plain, honest["key"]) == []
    assert rewards.verify_report(honest["report"], Ledger(past, read_only=True),
                                 honest["key"]) != []
    # Every tool that reads a stranger's copy refuses it instead.
    msg = refused(far, "view")
    assert "'submissions'" in msg and "missing" in msg
    with pytest.raises(CompareError, match="view"):
        compare_copies(honest["copy"], far)
    os.environ.pop("BNH_COORDINATOR_KEY", None)
    report_file = honest["tmp"] / "report.json"
    report_file.write_text(json.dumps(honest["report"]))
    with pytest.raises(publish_report.PublishError, match="view"):
        publish_report.plan(report_file, far, honest["tmp"] / "reports", honest["key"],
                            now=UNTIL + 10 * 86400, min_wait_hours=0)
    code = rewards.main(["verify", str(report_file), "--db", str(far),
                         "--public-key", honest["key"]])
    assert code == 2 and "refusing to read" in capsys.readouterr().err


def test_a_slow_view_is_refused_before_it_runs(honest: dict) -> None:
    path = variant(honest, "slow.sqlite3", "ALTER TABLE workers RENAME TO w2",
                   "CREATE VIEW workers AS WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL "
                   "SELECT x + 1 FROM c) SELECT w2.* FROM w2, (SELECT max(x) FROM c)")
    start = time.monotonic()
    refused(path, "view")
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("sql, match", [
    (("CREATE TRIGGER t AFTER INSERT ON events BEGIN SELECT 1; END",), "trigger"),
    (("CREATE VIEW extra AS SELECT 1",), "view"),
    (("CREATE TABLE notes(x)",), "unexpected table"),
    (("DROP TABLE job_exclusions",), "missing"),
    (("ALTER TABLE jobs ADD COLUMN extra TEXT",), "different columns"),
    (("CREATE TABLE w2 AS SELECT worker_id, capabilities, registered_at, key_hash, "
      "last_seen_at, owner, revoked_at FROM workers", "DROP TABLE workers",
      "CREATE TABLE workers (worker_id TEXT PRIMARY KEY, capabilities TEXT NOT NULL, "
      "registered_at TEXT NOT NULL, key_hash TEXT, last_seen_at INTEGER, owner TEXT, "
      "revoked_at INTEGER)", "INSERT INTO workers SELECT * FROM w2", "DROP TABLE w2"),
     "different columns"),
    (("CREATE INDEX sneaky ON jobs(created_at)",), "index"),
    (("DROP INDEX jobs_status_created",
      "CREATE INDEX jobs_status_created ON jobs(status, created_at) WHERE 0",), "index"),
    (("DROP INDEX events_by_event", "CREATE INDEX events_by_event ON events(id, event)"),
     "index"),
    (("DELETE FROM copy_info",), "exactly one copy_info row"),
    (("INSERT INTO copy_info SELECT * FROM copy_info",), "exactly one copy_info row"),
    (("UPDATE copy_info SET until = 'soon'",), "malformed"),
    (("UPDATE copy_info SET note = x'41'",), "malformed"),
])
def test_anything_but_plain_ledger_tables_is_refused(honest: dict, sql: tuple, match: str
                                                     ) -> None:
    refused(variant(honest, "bad.sqlite3", *sql), match)


def test_a_virtual_table_is_refused(honest: dict) -> None:
    try:
        path = variant(honest, "fts.sqlite3", "CREATE VIRTUAL TABLE notes USING fts5(x)")
    except sqlite3.OperationalError:
        pytest.skip("this SQLite has no fts5")
    refused(path, "virtual table")


# --- damaged, oversized and odd files ---------------------------------------------------

def test_damaged_and_truncated_copies_are_refused_not_crashed(honest: dict) -> None:
    data = bytearray(honest["copy"].read_bytes())
    page = 4096
    assert len(data) > 3 * page
    for name, blob in (
        ("flipped.sqlite3", data[:2 * page] + bytes(b ^ 0x5A for b in data[2 * page:3 * page])
         + data[3 * page:]),
        ("short.sqlite3", data[:len(data) - page]),
        ("header-only.sqlite3", bytes(data[:100])),
    ):
        path = honest["tmp"] / name
        path.write_bytes(bytes(blob))
        with pytest.raises(UnsafeCopyError):
            copy_info(path)
            # Some damage only shows when the tables are read.
            Ledger(path, untrusted=True)


def test_not_a_sqlite_file_is_refused_before_sqlite_reads_it(honest: dict) -> None:
    junk = honest["tmp"] / "junk.sqlite3"
    junk.write_bytes(b"hello" * 2000)
    with pytest.raises(UnsafeCopyError, match="not a SQLite file"):
        copy_info(junk)
    with pytest.raises(UnsafeCopyError, match="not a SQLite file"):
        copy_guard.connect(junk)
    # The command-line tools say so plainly instead of printing a traceback.
    assert ledger_copy.main(["info", str(junk)]) == 2
    with pytest.raises(ledger_copy.CopyError, match="not a SQLite file"):
        export_copy(junk, honest["tmp"] / "out.sqlite3", UNTIL, UNTIL + 10)


def test_an_index_that_disagrees_with_its_table_is_refused(honest: dict) -> None:
    """Pages that parse fine but an index whose entries don't match the rows: a lookup by
    index and a full scan would give different answers. Only integrity_check sees it."""
    path = honest["tmp"] / "index.sqlite3"
    shutil.copy(honest["copy"], path)
    with closing(sqlite3.connect(path)) as db:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        root = db.execute("SELECT rootpage FROM sqlite_master WHERE name='events_by_event'"
                          ).fetchone()[0]
        name = db.execute("SELECT event FROM events ORDER BY id LIMIT 1").fetchone()[0]
    data = bytearray(path.read_bytes())
    start = (root - 1) * page_size
    at = data.find(name.encode(), start, start + page_size)
    assert at > 0
    data[at] = ord("Z")  # same length, still a valid text value, now a different key
    path.write_bytes(bytes(data))
    with closing(sqlite3.connect(path)) as db:  # the damage is invisible to a table scan
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] > 0
    refused(path, "damaged")


def test_a_wal_format_copy_leaves_no_files_behind(honest: dict) -> None:
    """A copy switched to WAL format would make a normal read-only open create `-wal` and
    `-shm` files beside it; the immutable open never does."""
    path = variant(honest, "wal.sqlite3", "PRAGMA journal_mode=WAL")
    for extra in (f"{path}-wal", f"{path}-shm"):
        Path(extra).unlink(missing_ok=True)
    before = _file_sha256(path)
    assert copy_info(path)["until"] == UNTIL
    assert Ledger(path, untrusted=True).check_consistency() == []
    assert sorted(p.name for p in honest["tmp"].glob("wal.sqlite3*")) == ["wal.sqlite3"]
    assert _file_sha256(path) == before


def test_every_tool_reads_a_wal_format_copy_without_leaving_files(
        honest: dict, capsys: pytest.CaptureFixture[str]) -> None:
    path = variant(honest, "wal.sqlite3", "PRAGMA journal_mode=WAL")
    before = _file_sha256(path)
    report_file = honest["tmp"] / "report.json"
    report_file.write_text(json.dumps(honest["report"]))
    assert rewards.main(["verify", str(report_file), "--db", str(path),
                         "--public-key", honest["key"]]) == 0
    assert compare_copies(honest["copy"], path)["ok"]
    entry = publish_report.plan(report_file, path, honest["tmp"] / "reports", honest["key"],
                                now=UNTIL + 10 * 86400, min_wait_hours=0)
    assert entry
    capsys.readouterr()
    assert sorted(p.name for p in honest["tmp"].glob("wal.sqlite3*")) == ["wal.sqlite3"]
    assert _file_sha256(path) == before


def test_size_limits(honest: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    big = variant(honest, "big.sqlite3",
                  f"UPDATE events SET detail = '{'x' * (2 * 1024 * 1024)}' "
                  "WHERE id = (SELECT min(id) FROM events)")
    refused(big, "too big|could not be read")
    monkeypatch.setattr(copy_guard, "MAX_COPY_BYTES", 1024)
    with pytest.raises(UnsafeCopyError, match="at most 1024 bytes"):
        copy_guard.vet(honest["copy"])
    with pytest.raises(UnsafeCopyError, match="not a regular file"):
        copy_info(honest["tmp"])
    with pytest.raises(FileNotFoundError):
        copy_info(honest["tmp"] / "missing.sqlite3")


def test_a_hot_journal_beside_the_copy_changes_nothing(honest: dict) -> None:
    """A crashed writer leaves a hot `-journal`; a normal open would roll it back and
    rewrite the copy. The immutable open ignores it and touches no file."""
    path = honest["tmp"] / "hot.sqlite3"
    shutil.copy(honest["copy"], path)
    script = (
        "import os, sqlite3, sys\n"
        "db = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "db.execute('PRAGMA journal_mode=DELETE'); db.execute('PRAGMA cache_size=1')\n"
        "db.execute('BEGIN IMMEDIATE')\n"
        "db.execute(\"UPDATE events SET detail = printf('%0500d', id)\")\n"
        "db.execute(\"UPDATE jobs SET payload = printf('%0900d', 1)\")\n"
        "os._exit(0)\n")
    subprocess.run([sys.executable, "-c", script, str(path)], check=True)
    journal = Path(f"{path}-journal")
    assert journal.exists() and journal.stat().st_size > 0
    before = {p.name: _file_sha256(p) for p in (path, journal)}
    try:
        copy_info(path)
        Ledger(path, untrusted=True).check_consistency()
    except UnsafeCopyError:
        pass  # a half-written file may well be refused; that's fine too
    assert {p.name: _file_sha256(p) for p in (path, journal)} == before
    # A plain open, by contrast, rolls the journal back: the file changes on disk.
    with closing(sqlite3.connect(path)) as db:
        db.execute("SELECT count(*) FROM jobs").fetchone()
    assert _file_sha256(path) != before[path.name]


def test_export_vets_its_own_output(honest: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a future schema change made honest copies fail the copy check, export says so
    instead of publishing a file nobody else could open."""
    client, clock, ledger = make(honest["tmp"] / "second")
    busy_period(client)
    clock.t += 200

    def broken(path):
        raise UnsafeCopyError("pretend the schema drifted")

    monkeypatch.setattr(copy_guard, "vet", broken)
    out = honest["tmp"] / "second-copy.sqlite3"
    with pytest.raises(ledger_copy.CopyError, match="copy check"):
        export_copy(ledger.path, out, UNTIL, int(clock.t))
    assert not out.exists()
