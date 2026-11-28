"""`backup-restore-v1` (app/backup.py, app/run_lock.py): backing up and restoring the ledger.

Before (shown on the old code, docs/BUILD-LOG.md 0.5.67): a `cp` of the running ledger had
no rigs in it (recent writes live in the -wal file); SQLite's own backup was readable by
every user; copying a backup over the ledger after a crash replayed the crash's -wal into
it ("database disk image is malformed"); and a correct restore let a revoked rig log in
again, let a used enrollment code work a second time, and forgot which hidden tests had
been revealed and which periods were signed."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app import backup, ledger_role, run_lock
from app.config import Settings
from app.ledger import Ledger
from app.main import create_app
from tests.auth_helpers import OP, FirstPickRng, WorkerClient, enroll, enrollment_code, receipt
from tests.test_audits import T0, Clock, claim, sha

PROTOTYPE = Path(__file__).resolve().parents[1]


def wall(t: float = T0 + 10_000):
    return lambda: t


def make_app(tmp_path: Path, clock: Clock):
    settings = Settings(database_path=tmp_path / "data" / "db.sqlite3", audit_rate=0.0,
                        audit_rate_new=0.0, audit_rate_suspect=0.0,
                        operator_token=OP["X-Operator-Token"], public_settle_delay_seconds=0,
                        signing_key_path=tmp_path / "sign.key")
    return WorkerClient(create_app(settings, clock=clock, rng=FirstPickRng(1)))


def run_job(client: WorkerClient, worker: str, payload: str) -> str:
    assert client.post("/v1/jobs", json={"payload": payload}).status_code == 200
    job = claim(client, worker).json()
    r = client.post(f"/v1/jobs/{job['job_id']}/submit", json={
        "worker_id": worker, "result": sha(job["payload"]), "result_hash": sha(sha(job["payload"])),
        "started_at": T0, "completed_at": T0, **receipt(client, job["job_id"])})
    assert r.status_code == 200, r.text
    return job["job_id"]


def can_log_in(client: WorkerClient, worker: str, key: str) -> bool:
    r = client.post("/v1/jobs/next", params={"worker_id": worker},
                    headers={"Authorization": f"Bearer {key}"})
    # 404 = logged in, no work; 503 = logged in, coordinator paused (the key is checked first)
    return r.status_code in (200, 404, 503)


@pytest.fixture
def story(tmp_path: Path):
    """A running coordinator: two rigs and a spare code, then a backup, then after it a
    revocation, the spare code used, more work, a signed report, a shared copy, a pause."""
    clock = Clock()
    client = make_app(tmp_path, clock)
    enroll(client, "wk-a")
    enroll(client, "wk-bad")
    spare = enrollment_code(client, "owner-later")
    unused = enrollment_code(client, "owner-unused")
    run_job(client, "wk-a", "before the backup")
    live = tmp_path / "data" / "db.sqlite3"
    made = backup.make_backup(live, tmp_path / "backups" / "b1.sqlite3")
    clock.t += 60
    assert client.post("/v1/workers/wk-bad/revoke", headers=OP).status_code == 200
    r = client.post("/v1/workers/register", json={
        "worker_id": "wk-later", "enrollment_code": spare, "capabilities": ["echo"]})
    assert r.status_code == 200, r.text
    client.worker_keys["wk-later"] = r.json()["worker_key"]
    lost = run_job(client, "wk-a", "after the backup")
    run_job(client, "wk-later", "after the backup too")
    ledger: Ledger = client.app.state.ledger
    clock.t += 60
    ledger.record_signed_report(int(clock.t), T0, int(clock.t) - 1, "ab" * 32)
    ledger.record_copy(int(clock.t), int(clock.t) - 1, "cd" * 32)
    ledger.pause(int(clock.t), "suspected leak", "operator")
    signed_at = int(clock.t)
    return dict(client=client, clock=clock, live=live, backup=Path(made["file"]), made=made,
                spare=spare, unused=unused, lost=lost, signed_at=signed_at,
                bad_key=client.worker_keys["wk-bad"], tmp=tmp_path)


def stop(s) -> None:
    s["client"].app.state.ledger.close()
    s["client"].close()


def restart(s) -> WorkerClient:
    s["clock"].t += 20_000
    client = make_app(s["tmp"], s["clock"])
    client.worker_keys.update(s["client"].worker_keys)
    return client


# -- make -------------------------------------------------------------------------------------

def test_a_plain_copy_of_the_running_ledger_misses_recent_work(tmp_path: Path) -> None:
    """The old way: the main file alone, while the coordinator runs, had no rigs at all."""
    clock = Clock()
    client = make_app(tmp_path, clock)
    enroll(client, "wk-a")
    live = tmp_path / "data" / "db.sqlite3"
    shutil.copy2(live, tmp_path / "cp.sqlite3")
    with sqlite3.connect(tmp_path / "cp.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0
    made = backup.make_backup(live, tmp_path / "backups" / "b.sqlite3")
    assert made["rigs"] == 1 and made["format"] == backup.BACKUP_FORMAT


def test_the_old_way_of_restoring_let_a_revoked_rig_back_in(story) -> None:
    """Why `restore` carries revocations forward: a plain swap brought the key back.
    Since `ledger-role-v1` the swapped-in backup is refused at start (its header says it
    is a backup); with the mark cleared, as on a backup made before that rule or with
    `cp`, the old hole is still there, which is why `restore` is the way back."""
    stop(story)
    for side in backup._side_files(story["live"]):
        side.unlink()
    shutil.copy2(story["backup"], story["live"])
    with pytest.raises(ledger_role.NotLiveLedger, match="app.backup restore"):
        restart(story)
    with sqlite3.connect(story["live"]) as db:
        ledger_role.mark(db, ledger_role.LIVE)
    client = restart(story)
    assert can_log_in(client, "wk-bad", story["bad_key"])


def test_a_backup_is_owner_only_self_contained_and_checks_clean(story) -> None:
    path = story["backup"]
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert not list(path.parent.glob("*.partial*")) and not backup._side_files(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    info = backup.vet(path)
    assert info["consistency_problems"] == [] and info["results"] == 1
    assert story["made"]["sha256"] == backup._file_sha256(path)


def test_sqlites_own_backup_would_have_been_readable_by_everyone(tmp_path: Path) -> None:
    """Why `make` creates the file itself first: SQLite makes new files 0644."""
    live = tmp_path / "data" / "db.sqlite3"
    Ledger(live)
    with sqlite3.connect(live) as src, sqlite3.connect(tmp_path / "raw.sqlite3") as dst:
        src.backup(dst)
    assert os.stat(tmp_path / "raw.sqlite3").st_mode & 0o044
    with pytest.raises(backup.BackupError, match="read by other users"):
        backup.vet(tmp_path / "raw.sqlite3")


@pytest.mark.parametrize("case", ["exists", "same", "copy", "missing", "open-folder"])
def test_make_refuses_and_leaves_nothing(tmp_path: Path, case: str) -> None:
    live = tmp_path / "data" / "db.sqlite3"
    Ledger(live)
    out = tmp_path / "backups" / "b.sqlite3"
    src = live
    if case == "exists":
        out.parent.mkdir(mode=0o700)
        out.write_bytes(b"keep me")
    elif case == "same":
        out = live
    elif case == "copy":
        src = tmp_path / "copy.sqlite3"
        with sqlite3.connect(src) as db:
            db.execute("CREATE TABLE copy_info (format TEXT)")
    elif case == "missing":
        src = tmp_path / "nope.sqlite3"
    elif case == "open-folder":
        out.parent.mkdir()
        os.chmod(out.parent, 0o777)
    with pytest.raises(backup.BackupError):
        backup.make_backup(src, out)
    if case == "exists":
        assert out.read_bytes() == b"keep me"
    elif case != "same":
        assert not out.exists()
    assert not list(tmp_path.glob("**/*.partial*"))


# -- check ------------------------------------------------------------------------------------

@pytest.mark.parametrize("tamper,match", [
    ("CREATE TRIGGER t AFTER INSERT ON events BEGIN DELETE FROM events; END", "trigger"),
    ("CREATE VIEW v AS SELECT 1", "view"),
    ("CREATE TABLE extra (x)", "unexpected table"),
    ("CREATE INDEX sneaky ON jobs(payload)", "unexpected or changed index"),
    ("ALTER TABLE workers ADD COLUMN note TEXT", "columns a ledger doesn't have"),
    ("CREATE TABLE copy_info (format TEXT)", "shared ledger copy"),
    ("DROP TABLE enrollments", "missing the 'enrollments' table"),
])
def test_a_tampered_backup_is_refused(story, tamper: str, match: str) -> None:
    """A trigger or view in a restored ledger would run inside the live coordinator."""
    with sqlite3.connect(story["backup"]) as db:
        db.execute(tamper)
    with pytest.raises(backup.BackupError, match=match):
        backup.vet(story["backup"])
    stop(story)
    with pytest.raises(backup.BackupError, match=match):
        backup.restore(story["backup"], story["live"], wall=wall())


def test_a_damaged_or_foreign_backup_is_refused(story, tmp_path: Path) -> None:
    damaged = tmp_path / "damaged.sqlite3"
    data = bytearray(story["backup"].read_bytes())
    page = 4096
    data[page: 2 * page] = os.urandom(page)  # a whole table page of noise
    damaged.write_bytes(bytes(data))
    os.chmod(damaged, 0o600)
    with pytest.raises(backup.BackupError):
        backup.vet(damaged)
    text = tmp_path / "notes.txt"
    text.write_text("not a database")
    os.chmod(text, 0o600)
    with pytest.raises(backup.BackupError, match="not a ledger backup"):
        backup.vet(text)
    with pytest.raises(backup.BackupError, match="not found"):
        backup.vet(tmp_path / "missing.sqlite3")


# -- restore: the dry run ---------------------------------------------------------------------

def test_the_dry_run_changes_nothing_and_says_what_would_be_lost(story, capsys) -> None:
    before = backup._file_sha256(story["live"])
    files = sorted(p.name for p in story["live"].parent.iterdir())
    assert backup.main(["restore", str(story["backup"]), "--db", str(story["live"])]) == 0
    out = capsys.readouterr().out
    assert "Nothing was changed" in out
    assert "revocations would be carried forward: 1 (wk-bad)" in out
    assert "rigs that must enroll again: 1 (wk-later)" in out
    assert "lost (accepted after the backup): 2 jobs, 2 results" in out
    assert "the pause would be carried forward" in out
    assert "unused enrollment codes would be cancelled: 2" in out
    assert "WARNING: the signed report" in out
    assert backup._file_sha256(story["live"]) == before
    assert sorted(p.name for p in story["live"].parent.iterdir()) == files


# -- restore: the real thing ------------------------------------------------------------------

def test_restore_is_refused_while_the_coordinator_has_the_ledger_open(story) -> None:
    before = backup._file_sha256(story["live"])
    with pytest.raises(backup.BackupError, match="ledger is in use"):
        backup.restore(story["backup"], story["live"], wall=wall())
    assert backup._file_sha256(story["live"]) == before
    assert not list(story["live"].parent.glob("*.replaced-*"))
    assert not list(story["live"].parent.glob("*.restoring*"))


def test_nothing_opens_the_ledger_for_writing_during_a_restore(tmp_path: Path) -> None:
    live = tmp_path / "data" / "db.sqlite3"
    Ledger(live).close()
    with run_lock.exclusive(live):
        with pytest.raises(run_lock.LedgerBusy, match="restore from a backup is running"):
            Ledger(live)
        Ledger(live, read_only=True).status_counts()  # read-only views still work
    Ledger(live).close()  # and once the restore is done, it opens again
    assert os.stat(run_lock.lock_path(live)).st_mode & 0o777 == 0o600


def test_many_programs_share_the_ledger_but_any_one_blocks_a_restore(tmp_path: Path) -> None:
    live = tmp_path / "data" / "db.sqlite3"
    a, b = Ledger(live), Ledger(live)
    with pytest.raises(run_lock.LedgerBusy, match="in use"):
        run_lock.exclusive(live)
    a.close()
    with pytest.raises(run_lock.LedgerBusy):
        run_lock.exclusive(live)
    b.close()
    run_lock.exclusive(live).release()


def test_a_restore_keeps_revocations_and_a_used_code_stays_used(story) -> None:
    stop(story)
    done = backup.restore(story["backup"], story["live"], wall=wall())
    assert [r["worker_id"] for r in done["revocations"]] == ["wk-bad"]
    client = restart(story)
    assert not can_log_in(client, "wk-bad", story["bad_key"])
    assert can_log_in(client, "wk-a", client.worker_keys["wk-a"])
    client.app.state.ledger.resume(int(story["clock"].t), "restored", "operator")
    r = client.post("/v1/workers/register", json={
        "worker_id": "wk-thief", "enrollment_code": story["spare"], "capabilities": ["echo"]})
    assert r.status_code == 403, r.text
    # The rig that joined after the backup has to enroll again; its old key is gone.
    assert not can_log_in(client, "wk-later", client.worker_keys["wk-later"])
    assert done["rigs_to_reenroll"] == ["wk-later"]


def test_every_unused_code_from_the_backup_is_cancelled_and_new_ones_work(story) -> None:
    stop(story)
    done = backup.restore(story["backup"], story["live"], wall=wall())
    assert done["codes_cancelled"] == 2
    client = restart(story)
    client.app.state.ledger.resume(int(story["clock"].t), "restored", "operator")
    for code in (story["spare"], story["unused"]):
        r = client.post("/v1/workers/register", json={
            "worker_id": "wk-new", "enrollment_code": code, "capabilities": ["echo"]})
        assert r.status_code == 403, r.text
    enroll(client, "wk-new")


def test_signed_periods_shared_copies_and_the_pause_survive(story) -> None:
    before = Ledger(story["live"], read_only=True)
    revealed, latest = before.revealed_until(), before.latest_time()
    stop(story)
    backup.restore(story["backup"], story["live"], wall=wall())
    after = Ledger(story["live"], read_only=True)
    assert after.revealed_until() == revealed  # `test-reuse-v2` keeps refusing those inputs
    assert after.latest_time() >= latest >= story["signed_at"]  # nothing filed into signed time
    assert after.pause_state()["reason"] == "suspected leak"
    signed = [e for e in after.events() if e["event"] == "reward_report_signed"]
    assert len(signed) == 1 and signed[0]["at"] == story["signed_at"]


def test_the_restore_is_written_down_and_the_old_ledger_kept(story) -> None:
    stop(story)
    done = backup.restore(story["backup"], story["live"], wall=wall())
    folder = story["live"].parent
    replaced = folder / done["replaced"]
    assert replaced.exists() and os.stat(replaced).st_mode & 0o777 == 0o600
    with sqlite3.connect(replaced) as db:  # the old ledger, complete, -wal folded in
        assert db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 3
    assert not backup._side_files(replaced)
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        ["db.sqlite3", "db.sqlite3.lock", done["replaced"]])
    ev = [e for e in Ledger(story["live"], read_only=True).events()
          if e["event"] == backup.RESTORED_EVENT]
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["backup_sha256"] == story["made"]["sha256"]
    assert detail["lost_results"] == 2 and detail["revocations_carried"] == 1
    assert detail["codes_cancelled"] == 2 and detail["replaced"] == done["replaced"]
    assert done["lost_results"] == {"wk-a": 1, "wk-later": 1}
    assert done["signed_with_lost"] and done["signed_with_lost"][0]["results"] == 2
    assert Ledger(story["live"], read_only=True).check_consistency() == []


def test_a_crash_left_wal_is_folded_into_the_old_ledger_not_replayed_into_the_new(
        tmp_path: Path) -> None:
    """The old way corrupted the restored ledger: the crash's -wal was replayed into it."""
    live = tmp_path / "data" / "db.sqlite3"
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(PROTOTYPE)!r})
        from app.ledger import Ledger
        from app import backup, workers
        L = Ledger({str(live)!r})
        for i in range(30):
            workers.issue_code(L, f"owner-{{i}}", 3600, {T0} + i)
        backup.make_backup({str(live)!r}, {str(tmp_path / 'b' / 'b.sqlite3')!r})
        for i in range(30, 400):
            workers.issue_code(L, "owner-" + "x" * 50 + str(i), 3600, {T0} + i)
        os._exit(0)  # crash: no clean close, the -wal stays
    """)
    subprocess.run([sys.executable, "-c", script], check=True, cwd=PROTOTYPE)
    assert backup._side_files(live)  # the crash left a -wal behind
    # The old way: put the backup over the ledger, leave the -wal: SQLite replays it.
    broken = tmp_path / "old-way"
    broken.mkdir()
    for p in live.parent.iterdir():
        shutil.copy2(p, broken / p.name)
    shutil.copy2(tmp_path / "b" / "b.sqlite3", broken / live.name)
    with pytest.raises(sqlite3.DatabaseError, match="malformed"):
        with sqlite3.connect(broken / live.name) as db:
            db.execute("PRAGMA integrity_check").fetchall()
            db.execute("SELECT COUNT(*) FROM enrollments").fetchone()
    # The tool: folds the -wal into the old ledger first, then swaps.
    done = backup.restore(tmp_path / "b" / "b.sqlite3", live, wall=wall())
    with sqlite3.connect(live) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0] == 30
    with sqlite3.connect(live.parent / done["replaced"]) as db:
        assert db.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0] == 400


def test_restore_without_a_ledger_to_replace_warns_and_cancels_codes(story, capsys) -> None:
    stop(story)
    for p in list(story["live"].parent.iterdir()):
        p.unlink()  # the disk died
    monkey = time.time
    try:
        time.time = wall()  # type: ignore[assignment]
        assert backup.main(["restore", str(story["backup"]), "--db", str(story["live"]),
                            "--yes"]) == 0
    finally:
        time.time = monkey  # type: ignore[assignment]
    out = capsys.readouterr().out
    assert "there is no ledger to replace" in out and "cancelled: 2" in out
    assert Ledger(story["live"], read_only=True).check_consistency() == []


def test_restore_refuses_a_clock_far_behind_the_ledger_and_changes_nothing(story) -> None:
    stop(story)
    before = backup._file_sha256(story["live"])
    with pytest.raises(backup.BackupError, match="behind the newest time"):
        backup.restore(story["backup"], story["live"], wall=wall(T0 - 86_400))
    folder = story["live"].parent
    assert not list(folder.glob("*.replaced-*")) and not list(folder.glob("*.restoring*"))
    # The live ledger was only tidied (its -wal folded in); its contents are the same.
    with sqlite3.connect(story["live"]) as db:
        assert db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 3
    assert before  # (bytes may differ: the -wal now lives inside the file)


@pytest.mark.parametrize("case", ["same-file", "world-readable", "missing"])
def test_restore_refuses_a_bad_backup_argument(story, case: str) -> None:
    stop(story)
    src = story["backup"]
    if case == "same-file":
        src = story["live"]
    elif case == "world-readable":
        os.chmod(src, 0o644)
    elif case == "missing":
        src = src.with_name("gone.sqlite3")
    with pytest.raises(backup.BackupError):
        backup.restore(src, story["live"], wall=wall())
    assert not list(story["live"].parent.glob("*.replaced-*"))


def test_restoring_an_up_to_date_backup_loses_nothing(tmp_path: Path) -> None:
    clock = Clock()
    client = make_app(tmp_path, clock)
    enroll(client, "wk-a")
    run_job(client, "wk-a", "one")
    live = tmp_path / "data" / "db.sqlite3"
    made = backup.make_backup(live, tmp_path / "backups" / "b.sqlite3")
    client.app.state.ledger.close()
    done = backup.restore(Path(made["file"]), live, wall=wall())
    assert done["lost_jobs"] == 0 and done["lost_results"] == {}
    assert done["rigs_to_reenroll"] == [] and done["revocations"] == []
    assert Ledger(live, read_only=True).status_counts().get("accepted") == 1


def test_the_cli_make_and_check(tmp_path: Path, capsys) -> None:
    live = tmp_path / "data" / "db.sqlite3"
    Ledger(live).close()
    out = tmp_path / "backups" / "b.sqlite3"
    assert backup.main(["make", "--db", str(live), "--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["sha256"] == backup._file_sha256(out)
    assert backup.main(["check", str(out)]) == 0
    assert backup.main(["make", "--db", str(live), "--out", str(out)]) == 2
    assert "already exists" in capsys.readouterr().err
    os.chmod(out, 0o644)
    assert backup.main(["check", str(out)]) == 2
