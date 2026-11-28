"""`ledger-role-v1` (app/ledger_role.py): tools that write a record refuse a backup.

Before (shown on the old code, docs/BUILD-LOG.md 0.5.73): exporting a ledger copy from a
backup wrote the "this period's hidden tests were shared" record into the backup, so the
live coordinator accepted the same hidden test again; signing a report from a backup
recorded the signature in the backup; a backup copied over the ledger by hand started
silently and let a rig revoked after the backup log in again; and even read-only looks
(a report without --sign) switched the backup to WAL mode and changed its bytes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from app import backup, incident, ledger_copy, ledger_role, review, rewards, vouches, workers
from app.ledger import COPY_MADE_EVENT, REPORT_SIGNED_EVENT, Ledger
from app.ledger_copy import CopyError, export_copy
from tests.test_audits import OP, make, pinned  # noqa: F401
from tests.test_backup_restore import (  # noqa: F401
    can_log_in,
    make_app,
    restart,
    stop,
    story,
    wall,
)
from tests.test_hidden_test_reuse import hidden_test
from tests.test_ledger_copy import UNTIL, busy_period, rows


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def neighbours(path: Path) -> list[str]:
    """Files next to `path` that SQLite or the run lock would create."""
    return sorted(p.name for p in Path(path).parent.iterdir()
                  if p.name.startswith(Path(path).name) and p != Path(path))


def events(path: Path, name: str) -> int:
    with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1",
                                 uri=True)) as db:
        return db.execute("SELECT COUNT(*) FROM events WHERE event=?", (name,)).fetchone()[0]


@pytest.fixture
def backed_up(tmp_path: Path, pinned):  # noqa: F811
    """A coordinator with a busy closed period (a hidden test in it), then a backup."""
    client, clock, ledger = make(tmp_path)
    busy_period(client)
    clock.t += 200
    made = backup.make_backup(ledger.path, tmp_path / "backups" / "b1.sqlite3")
    return client, clock, ledger, Path(made["file"])


# -- the holes, replayed ----------------------------------------------------------------------

def test_every_copy_that_exists_keeps_its_hidden_tests_retired(backed_up, tmp_path) -> None:
    """Old code: the copy was made from the backup and its record went into the backup, so
    the live coordinator took the revealed hidden test again (200, not 409)."""
    client, clock, _ledger, b = backed_up
    out = tmp_path / "copy.sqlite3"
    try:
        export_copy(b, out, UNTIL, int(clock.t))
    except CopyError:
        assert not out.exists()
    else:
        assert hidden_test(client).status_code == 409


def test_copy_from_a_backup_is_refused_and_nothing_is_written(backed_up, tmp_path) -> None:
    client, clock, ledger, b = backed_up
    before = digest(b)
    out = tmp_path / "copy.sqlite3"
    with pytest.raises(CopyError, match="backup") as err:
        export_copy(b, out, UNTIL, int(clock.t))
    assert "live ledger" in str(err.value)
    assert not out.exists() and not list(tmp_path.glob("copy.sqlite3*"))
    assert digest(b) == before and neighbours(b) == []
    assert events(b, COPY_MADE_EVENT) == 0
    # From the live ledger it works, and the test is retired there.
    export_copy(ledger.path, out, UNTIL, int(clock.t))
    assert hidden_test(client).status_code == 409


def test_export_cli_on_a_backup_says_where_to_point_it(backed_up, tmp_path, capsys,
                                                       monkeypatch) -> None:
    _client, clock, _ledger, b = backed_up
    monkeypatch.setattr(time, "time", lambda: float(clock.t))
    before = digest(b)
    assert ledger_copy.main(["export", "--db", str(b), "--until", str(UNTIL),
                             "--out", str(tmp_path / "c.sqlite3")]) == 2
    err = capsys.readouterr().err
    assert "backup" in err and "live ledger" in err and "Traceback" not in err
    assert digest(b) == before and not (tmp_path / "c.sqlite3").exists()


def test_signing_a_report_from_a_backup_is_refused(backed_up, tmp_path, capsys,
                                                   monkeypatch) -> None:
    """Old code: returned 0 and recorded the signature in the backup, so the live ledger
    never learned the period was signed (`ledger-clock-v1` relies on that record)."""
    client, clock, ledger, b = backed_up
    monkeypatch.setattr(rewards.time, "time", lambda: float(clock.t))
    key = str(client.app.state.settings.resolved_signing_key_path())
    before = digest(b)
    out = tmp_path / "signed.json"
    assert rewards.main(["report", "--db", str(b), "--sign", "--key-file", key,
                         "--until", str(UNTIL), "--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert "backup" in err and "Traceback" not in err
    assert not out.exists()
    assert digest(b) == before and neighbours(b) == []
    assert events(b, REPORT_SIGNED_EVENT) == 0
    assert rows(ledger.path, "SELECT COUNT(*) AS n FROM events WHERE event=?",
                (REPORT_SIGNED_EVENT,))[0]["n"] == 0


def test_an_unsigned_report_on_a_backup_reads_it_without_changing_it(backed_up, tmp_path,
                                                                     monkeypatch) -> None:
    """Old code: the "read" opened the backup for writing, switched it to WAL mode and
    left -wal/-shm files next to it, so its sha256 changed."""
    _client, clock, ledger, b = backed_up
    monkeypatch.setattr(rewards.time, "time", lambda: float(clock.t))
    before = digest(b)
    out = tmp_path / "r.json"
    assert rewards.main(["report", "--db", str(b), "--until", str(UNTIL),
                         "--out", str(out)]) in (0, 1)
    assert out.exists()
    assert digest(b) == before and neighbours(b) == []
    same = tmp_path / "r-live.json"
    assert rewards.main(["report", "--db", str(ledger.path), "--until", str(UNTIL),
                         "--out", str(same)]) in (0, 1)
    a, c = json.loads(out.read_text()), json.loads(same.read_text())
    assert a["report_sha256"] == c["report_sha256"]  # same numbers as the live ledger
    assert rewards.main(["verify", str(out), "--db", str(b)]) == 0
    assert digest(b) == before and neighbours(b) == []


def test_a_backup_copied_into_place_by_hand_is_refused(story) -> None:
    """Old code: started without a word and the rig revoked after the backup logged in."""
    stop(story)
    for side in backup._side_files(story["live"]):
        side.unlink()
    kept = story["tmp"] / "the-real-ledger.sqlite3"
    shutil.copy2(story["live"], kept)
    shutil.copy2(story["backup"], story["live"])
    try:
        client = restart(story)
    except ledger_role.NotLiveLedger as exc:
        assert "app.backup restore" in str(exc)
    else:
        assert not can_log_in(client, "wk-bad", story["bad_key"])
    # The supported way still works and keeps the revocation.
    shutil.copy2(kept, story["live"])
    for side in backup._side_files(story["live"]):
        side.unlink()
    backup.restore(story["backup"], story["live"], wall=wall())
    client = restart(story)
    assert not can_log_in(client, "wk-bad", story["bad_key"])


def test_writing_tools_refuse_a_backup_with_one_line(backed_up, capsys, monkeypatch) -> None:
    _client, clock, _ledger, b = backed_up
    monkeypatch.setattr(time, "time", lambda: float(clock.t))
    before = digest(b)
    db = str(b)
    assert workers.main(["--db", db, "revoke", "wk-b"]) == 2
    assert workers.main(["--db", db, "enroll", "--owner", "bob"]) == 2
    assert incident.main(["--db", db, "pause", "--reason", "x"]) == 2
    assert incident.main(["--db", db, "resume", "--note", "x"]) == 2
    err = capsys.readouterr().err
    assert err.count("is a backup") == 4 and "Traceback" not in err
    assert digest(b) == before and neighbours(b) == []


def test_opening_a_backup_for_writing_changes_no_byte(backed_up) -> None:
    _client, _clock, _ledger, b = backed_up
    before = digest(b)
    with pytest.raises(ledger_role.NotLiveLedger, match="backup restore"):
        Ledger(b)
    assert digest(b) == before
    assert neighbours(b) == []  # no -wal, -shm, -journal or .lock


def test_the_mark_is_read_again_from_sqlite_itself(backed_up, monkeypatch) -> None:
    """Second fence: even if the plain-bytes look misses it, the open refuses before the
    WAL switch."""
    _client, _clock, _ledger, b = backed_up
    before = digest(b)
    monkeypatch.setattr(ledger_role, "of_file", lambda path: None)
    with pytest.raises(ledger_role.NotLiveLedger):
        Ledger(b)
    assert digest(b) == before
    assert not [n for n in neighbours(b) if n.endswith(("-wal", "-shm"))]


def test_read_only_looks_at_a_backup_still_work(backed_up, capsys) -> None:
    _client, _clock, _ledger, b = backed_up
    before = digest(b)
    assert Ledger(b, read_only=True).check_consistency() == []
    assert review.main(["--db", str(b)]) == 0
    assert vouches.main(["--db", str(b)]) in (0, 1)
    assert incident.main(["--db", str(b), "status"]) == 0
    assert backup.main(["check", str(b)]) == 0
    assert '"ledger_role": "backup"' in capsys.readouterr().out
    assert digest(b) == before


# -- marks --------------------------------------------------------------------------------------

def test_backup_make_marks_the_backup_and_not_the_ledger(backed_up) -> None:
    _client, _clock, ledger, b = backed_up
    assert ledger_role.of_file(b) == ledger_role.BACKUP
    assert ledger_role.of_file(ledger.path) == ledger_role.LIVE  # running, in WAL mode
    with closing(sqlite3.connect(ledger.path)) as db:
        assert ledger_role.of_db(db) == ledger_role.LIVE
    assert backup.vet(b)["ledger_role"] == "backup"
    # A backup of a backup is still a backup.
    again = backup.make_backup(b, b.parent / "b2.sqlite3")
    assert ledger_role.of_file(Path(again["file"])) == ledger_role.BACKUP


def test_restore_clears_the_mark_and_marks_the_ledger_it_moved_aside(story) -> None:
    stop(story)
    done = backup.restore(story["backup"], story["live"], wall=wall())
    assert ledger_role.of_file(story["live"]) == ledger_role.LIVE
    old = story["live"].with_name(done["replaced"])
    assert ledger_role.of_file(old) == ledger_role.REPLACED and done["replaced_marked"]
    assert ledger_role.of_file(story["backup"]) == ledger_role.BACKUP  # backup untouched
    with pytest.raises(ledger_role.NotLiveLedger, match="moved aside"):
        Ledger(old)
    client = restart(story)  # the restored ledger starts
    assert client.get("/health").status_code == 200
    # Going back to the old ledger is a restore too, and works.
    stop({"client": client})
    back = backup.restore(old, story["live"], wall=wall(story["clock"].t + 10))
    assert ledger_role.of_file(story["live"]) == ledger_role.LIVE
    assert back["backup"]["ledger_role"] == "replaced"


def test_restore_onto_a_dead_disk_gives_a_live_ledger(backed_up, tmp_path) -> None:
    client, _clock, ledger, b = backed_up
    ledger.close()
    client.close()
    fresh = tmp_path / "new-disk" / "db.sqlite3"
    done = backup.restore(b, fresh, wall=wall())
    assert done["replaced"] is None and done["replaced_marked"] is False
    assert ledger_role.of_file(fresh) == ledger_role.LIVE
    Ledger(fresh).close()


def test_an_unmarked_older_backup_still_restores(backed_up, tmp_path) -> None:
    """Backups made before this rule carry no mark (honest limit: so does a `cp`)."""
    client, _clock, ledger, b = backed_up
    with closing(sqlite3.connect(b)) as db:
        ledger_role.mark(db, ledger_role.LIVE)
    assert backup.vet(b)["ledger_role"] == "live"
    ledger.close()
    client.close()
    done = backup.restore(b, ledger.path, wall=wall())
    assert ledger_role.of_file(ledger.path) == ledger_role.LIVE
    assert ledger_role.of_file(ledger.path.with_name(done["replaced"])) == ledger_role.REPLACED


def test_a_file_from_another_program_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "data" / "db.sqlite3"
    Ledger(path).close()
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA application_id=12345")
    with pytest.raises(ledger_role.NotLiveLedger, match="another program"):
        Ledger(path)
    path.chmod(0o600)
    with pytest.raises(backup.BackupError, match="another program"):
        backup.vet(path)
    with pytest.raises(ValueError):
        with closing(sqlite3.connect(path)) as db:
            ledger_role.mark(db, 12345)


def test_copies_exported_from_the_live_ledger_are_live(backed_up, tmp_path) -> None:
    _client, clock, ledger, _b = backed_up
    out = tmp_path / "copy.sqlite3"
    export_copy(ledger.path, out, UNTIL, int(clock.t))
    assert ledger_role.of_file(out) == ledger_role.LIVE
    assert Ledger(out, untrusted=True).check_consistency() == []


def test_of_file_never_guesses(tmp_path: Path) -> None:
    assert ledger_role.of_file(tmp_path / "missing") is None
    assert ledger_role.of_file(tmp_path) is None
    (tmp_path / "empty").write_bytes(b"")
    assert ledger_role.of_file(tmp_path / "empty") is None
    (tmp_path / "text").write_bytes(b"x" * 200)
    assert ledger_role.of_file(tmp_path / "text") is None
    (tmp_path / "short").write_bytes(ledger_role.SQLITE_MAGIC + b"\0" * 10)
    assert ledger_role.of_file(tmp_path / "short") is None
    assert ledger_role.name(ledger_role.BACKUP) == "backup"
    assert ledger_role.name(7).startswith("unknown")
