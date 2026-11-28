"""Backing up and restoring the ledger (`backup-restore-v1`, code audit sixteenth pass).

The question for this pass: **what happens when the ledger is restored from a backup?**
Disks die and mistakes happen, so an operator will one day put an older copy of the ledger
back. Before this module there was no backup tool, and the obvious ways went wrong (shown
on the old code, docs/BUILD-LOG.md 0.5.67; tests/test_backup_restore.py replays each):

  * `cp ledger.sqlite3 backup.sqlite3` while the coordinator runs copied a file with
    **no rigs and no jobs in it**: SQLite keeps recent writes in the `-wal` side file until
    it folds them in, so the main file alone was hours behind, with no error;
  * SQLite's own `.backup` command wrote the copy readable by every user on the machine,
    and the ledger holds the answer to every hidden test;
  * copying a backup over the ledger after a crash, with the crash's `-wal` file still
    next to it, made SQLite replay that old `-wal` into the backup:
    "database disk image is malformed";
  * a correct restore quietly **undid security decisions**: a rig revoked after the
    backup could log in again, an enrollment code used after the backup worked a second
    time (for anyone who had seen it), and the records of shared ledger copies and signed
    reports were gone, so revealed hidden tests could be reused and new work could be
    filed into a period that was already signed.

Commands:

    python -m app.backup make --db ./data/prototype.sqlite3 --out ./backups/ledger-1.sqlite3
    python -m app.backup check ./backups/ledger-1.sqlite3
    python -m app.backup restore ./backups/ledger-1.sqlite3 --db ./data/prototype.sqlite3
    python -m app.backup restore ./backups/ledger-1.sqlite3 --db ./data/prototype.sqlite3 --yes

`make` takes a consistent snapshot while the coordinator keeps running (SQLite's backup
API, which includes the `-wal`), writes it owner-only (0600) as one self-contained file,
under a temporary name first, and checks it before giving it its real name.

`check` opens a backup without changing it and says whether it is safe to restore:
owner-only, a real SQLite file, nothing but the ledger's own tables and indexes (no
views, triggers or extra tables, which would run inside the live coordinator after a
restore), `integrity_check` ok, not a shared ledger copy.

`restore` without `--yes` changes nothing and prints what the restore would undo. With
`--yes`, and only when nothing else has the ledger open (app/run_lock.py), it:

  1. builds the restored ledger next to the live one from the backup;
  2. **carries forward** from the ledger it replaces whatever must never be undone:
     revocations, the records of shared ledger copies and signed reports (with their
     original times, so the clock rule still holds, app/ledger_clock.py), and a pause;
  3. cancels every enrollment code that is still unused in the backup (some may have
     been used after the backup; make new ones);
  4. writes a `ledger_restored` event saying which backup, and what was lost;
  5. moves the old ledger aside as `<ledger>.replaced-<time>` (owner-only, never deleted)
     together with its side files, so no stale `-wal` can be replayed into the restored
     one, and puts the restored ledger in its place in one step.

Honest limits: work accepted after the backup is gone from the restored ledger (the tool
says how many results, per rig; the replaced ledger is kept so you can look). Rigs that
enrolled after the backup have to enroll again. If the ledger being replaced is lost too
(dead disk), nothing can be carried forward: revoke again any rig you had revoked, and
check the public logbook (`reports/`) for periods already signed. A backup holds every
secret the ledger holds: keep it owner-only and encrypt it before it leaves the machine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from . import copy_guard, ledger_clock, run_lock, secret_file
from .ledger import COPY_MADE_EVENT, PAUSE_EVENTS, REPORT_SIGNED_EVENT, Ledger
from .screen import say

RULE = run_lock.RULE
BACKUP_FORMAT = "bnh-ledger-backup/1"
RESTORED_EVENT = "ledger_restored"
# Events that record something already shown to the outside world; never undone.
CARRIED_EVENTS = (COPY_MADE_EVENT, REPORT_SIGNED_EVENT)
INTEGRITY_ERRORS_SHOWN = 3
RIGS_SHOWN = 20


class BackupError(ValueError):
    """A backup can't be made, trusted or restored. Nothing was changed."""


# -- helpers --------------------------------------------------------------------------
def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ro(path: Path) -> sqlite3.Connection:
    """Read-only connection to a self-contained file (a backup): never writes, ignores
    any side file next to it."""
    db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1", uri=True,
                         isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def _side_files(path: Path) -> list[Path]:
    return [p for p in (Path(f"{path}{s}") for s in Ledger.SIDE_FILES) if p.exists()]


def _fsync_folder(folder: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _schema_problems(db: sqlite3.Connection) -> list[str]:
    """A backup may hold only the ledger's own tables and indexes. Older ledgers may miss
    columns (the coordinator adds them on start); extra ones, views and triggers aren't
    allowed: after a restore they would sit inside the live coordinator."""
    want = copy_guard.expected_schema()
    want_tables = {k: v for k, v in want["tables"].items() if k != "copy_info"}
    found = copy_guard._schema_of(db)
    problems = [f"has a {kind} named {say(name, 60)!r} (a ledger holds tables only)"
                for kind, name in found["others"]]
    problems += [f"has a virtual table {say(name, 60)!r}" for name in found["virtual"]]
    if "copy_info" in found["tables"]:
        problems.append("is a shared ledger copy (redacted), not a backup of the ledger")
    allowed = set(want_tables) | copy_guard.SQLITE_OWN_TABLES
    for name in sorted(set(found["tables"]) - allowed - {"copy_info"}):
        problems.append(f"has an unexpected table {say(name, 60)!r}")
    for name in ("workers", "enrollments", "jobs", "submissions", "events"):
        if name not in found["tables"]:
            problems.append(f"is missing the {name!r} table")
    for name in sorted(set(want_tables) & set(found["tables"])):
        extra = set(found["tables"][name]) - set(want_tables[name])
        if extra:
            problems.append(f"table {name!r} has columns a ledger doesn't have")
    for name, spec in sorted(found["indexes"].items()):
        table, _cols, _unique, partial = spec
        if name.startswith("sqlite_autoindex_"):
            if table not in want_tables or partial:
                problems.append(f"has an unexpected index {say(name, 60)!r}")
            continue
        if want["indexes"].get(name) != spec:
            problems.append(f"has an unexpected or changed index {say(name, 60)!r}")
    return problems


def _integrity(db: sqlite3.Connection) -> list[str]:
    rows = [r[0] for r in db.execute("PRAGMA integrity_check").fetchall()]
    return [] if rows == ["ok"] else [f"integrity check: {say(r, 120)}"
                                      for r in rows[:INTEGRITY_ERRORS_SHOWN]]


def _summary(db: sqlite3.Connection) -> dict[str, Any]:
    one = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    newest = one("SELECT MAX(m) FROM (SELECT MAX(at) AS m FROM events "
                 "UNION ALL SELECT MAX(updated_at) FROM jobs "
                 "UNION ALL SELECT MAX(accepted_at) FROM submissions)")
    return {"jobs": one("SELECT COUNT(*) FROM jobs"),
            "results": one("SELECT COUNT(*) FROM submissions"),
            "rigs": one("SELECT COUNT(*) FROM workers"),
            "newest_time": None if newest is None else int(newest)}


def vet(path: Path) -> dict[str, Any]:
    """Every check `check` runs. Returns the backup's summary; raises BackupError."""
    path = Path(path)
    try:
        secret_file.check_path(path, "the backup", secret=True, folder=False)
    except FileNotFoundError:
        raise BackupError(f"backup not found: {say(str(path), 160)}") from None
    except secret_file.UnsafeFileError as exc:
        raise BackupError(str(exc)) from None
    try:
        copy_guard.check_file(path, max_bytes=float("inf"))
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"not a ledger backup: {exc}") from None
    try:
        with closing(_ro(path)) as db:
            problems = _integrity(db) or _schema_problems(db)
            if problems:
                raise BackupError("the backup can't be trusted: " + "; ".join(problems))
            info = _summary(db)
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"not a readable ledger backup: {exc}") from None
    info["consistency_problems"] = Ledger(path, read_only=True).check_consistency()
    return info


# -- make -----------------------------------------------------------------------------
def make_backup(source: Path, dest: Path) -> dict[str, Any]:
    """Snapshot the ledger at `source` into a new owner-only file at `dest`."""
    source, dest = Path(source), Path(dest)
    if not source.exists():
        raise BackupError(f"ledger not found: {say(str(source), 160)}")
    if dest.exists() or dest.is_symlink():
        raise BackupError(f"{say(str(dest), 160)} already exists; pick a new file name")
    if dest.resolve() == source.resolve():
        raise BackupError("the backup must be a different file from the ledger")
    if copy_guard.has_copy_info(source):
        raise BackupError("that file is a shared ledger copy; back up the live ledger")
    try:
        secret_file.make_private_folder(dest.parent, "the backup folder")
    except secret_file.UnsafeFileError as exc:
        raise BackupError(str(exc)) from None
    partial = dest.with_name(dest.name + ".partial")
    for leftover in [partial, *(Path(f"{partial}{s}") for s in Ledger.SIDE_FILES)]:
        leftover.unlink(missing_ok=True)
    try:
        # Owner-only before SQLite writes a byte (SQLite on its own makes it 0644).
        secret_file.create_empty(partial, "the backup")
        # The backup API copies a consistent snapshot, `-wal` included, while the
        # coordinator keeps running; a plain `cp` of the main file misses the `-wal`.
        with closing(sqlite3.connect(source, timeout=10)) as src, \
                closing(sqlite3.connect(partial)) as out:
            src.backup(out)
        with closing(sqlite3.connect(partial, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal
        with open(partial, "rb+") as fh:
            os.fsync(fh.fileno())
        info = vet(partial)
        os.replace(partial, dest)
        _fsync_folder(dest.parent)
    except BackupError:
        raise
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"backup failed: {exc}") from None
    finally:
        for leftover in [partial, *(Path(f"{partial}{s}") for s in Ledger.SIDE_FILES)]:
            leftover.unlink(missing_ok=True)
    return {"format": BACKUP_FORMAT, "file": str(dest), "sha256": _file_sha256(dest),
            "bytes": dest.stat().st_size, **info}


# -- restore --------------------------------------------------------------------------
def _attach(db: sqlite3.Connection, path: Path, name: str) -> None:
    db.execute(f"ATTACH DATABASE ? AS {name}",
               (f"{Path(path).resolve().as_uri()}?mode=ro",))


def _compare(db: sqlite3.Connection, has_live: bool) -> dict[str, Any]:
    """What the restore keeps and what it loses. `main` is the restored ledger (or the
    backup itself on a dry run); `live` (attached) is the ledger being replaced."""
    plan: dict[str, Any] = {
        "unused_codes": db.execute("SELECT COUNT(*) FROM main.enrollments WHERE used_at IS "
                                   "NULL").fetchone()[0],
        "backup_paused": _paused(db, "main") is not None,
    }
    if not has_live:
        plan.update(live=False, revocations=[], carried_events=0, pause=None,
                    rigs_to_reenroll=[], lost_jobs=0, lost_results={}, signed_with_lost=[])
        return plan
    plan["live"] = True
    plan["revocations"] = [dict(r) for r in db.execute(
        "SELECT l.worker_id, l.revoked_at FROM live.workers l JOIN main.workers m "
        "USING(worker_id) WHERE l.revoked_at IS NOT NULL AND m.revoked_at IS NULL "
        "ORDER BY l.worker_id")]
    marks = ",".join("?" * len(CARRIED_EVENTS))
    plan["carried"] = [dict(r) for r in db.execute(
        f"SELECT event, detail, at FROM live.events l WHERE event IN ({marks}) AND NOT "
        f"EXISTS (SELECT 1 FROM main.events m WHERE m.event=l.event AND m.detail IS "
        f"l.detail AND m.at=l.at) ORDER BY id", CARRIED_EVENTS)]
    plan["carried_events"] = len(plan["carried"])
    live_pause, main_pause = _paused(db, "live"), _paused(db, "main")
    plan["pause"] = dict(live_pause) if live_pause is not None and main_pause is None else None
    plan["rigs_to_reenroll"] = [r[0] for r in db.execute(
        "SELECT worker_id FROM live.workers l WHERE key_hash IS NOT NULL AND revoked_at IS "
        "NULL AND NOT EXISTS (SELECT 1 FROM main.workers m WHERE m.worker_id=l.worker_id "
        "AND m.key_hash IS l.key_hash) ORDER BY worker_id")]
    plan["lost_jobs"] = db.execute(
        "SELECT COUNT(*) FROM live.jobs l WHERE NOT EXISTS (SELECT 1 FROM main.jobs m "
        "WHERE m.job_id=l.job_id)").fetchone()[0]
    lost = db.execute(
        "SELECT worker_id, COUNT(*) AS n, MIN(accepted_at) AS first, MAX(accepted_at) AS last "
        "FROM live.submissions l WHERE NOT EXISTS (SELECT 1 FROM main.submissions m WHERE "
        "m.job_id=l.job_id) GROUP BY worker_id ORDER BY worker_id").fetchall()
    plan["lost_results"] = {r["worker_id"]: r["n"] for r in lost}
    # A signed report whose period holds a result the restored ledger won't have can no
    # longer be re-made from it.
    plan["signed_with_lost"] = []
    for r in db.execute("SELECT detail FROM live.events WHERE event=? ORDER BY id",
                        (REPORT_SIGNED_EVENT,)):
        try:
            d = json.loads(r["detail"] or "{}")
            since, until = int(d["since"]), int(d["until"])
        except (ValueError, KeyError, TypeError):
            continue
        n = db.execute(
            "SELECT COUNT(*) FROM live.submissions l WHERE accepted_at>=? AND accepted_at<? "
            "AND NOT EXISTS (SELECT 1 FROM main.submissions m WHERE m.job_id=l.job_id)",
            (since, until)).fetchone()[0]
        if n:
            plan["signed_with_lost"].append({"since": since, "until": until, "results": n})
    return plan


def _paused(db: sqlite3.Connection, schema: str) -> sqlite3.Row | None:
    marks = ",".join("?" * len(PAUSE_EVENTS))
    row = db.execute(f"SELECT event, detail, at FROM {schema}.events WHERE event IN "
                     f"({marks}) ORDER BY id DESC LIMIT 1", PAUSE_EVENTS).fetchone()
    return row if row is not None and row["event"] == "coordinator_paused" else None


def _latest(db: sqlite3.Connection, schema: str) -> int | None:
    row = db.execute(
        f"SELECT MAX(m) FROM (SELECT MAX(at) AS m FROM {schema}.events "
        f"UNION ALL SELECT MAX(updated_at) FROM {schema}.jobs "
        f"UNION ALL SELECT MAX(created_at) FROM {schema}.jobs "
        f"UNION ALL SELECT MAX(accepted_at) FROM {schema}.submissions "
        f"UNION ALL SELECT MAX(registered_at) FROM {schema}.workers "
        f"UNION ALL SELECT MAX(last_seen_at) FROM {schema}.workers "
        f"UNION ALL SELECT MAX(revoked_at) FROM {schema}.workers "
        f"UNION ALL SELECT MAX(created_at) FROM {schema}.enrollments "
        f"UNION ALL SELECT MAX(used_at) FROM {schema}.enrollments)").fetchone()
    return None if row[0] is None else int(row[0])


def plan_restore(backup: Path, live: Path) -> dict[str, Any]:
    """Dry run: what `restore --yes` would carry forward and lose. Changes nothing."""
    backup, live = Path(backup), Path(live)
    info = vet(backup)
    with closing(_ro(backup)) as db:
        has_live = live.exists()
        if has_live:
            _attach(db, live, "live")
        plan = _compare(db, has_live)
    return {"backup": info, **plan}


def restore(backup: Path, live: Path, wall: Any = None) -> dict[str, Any]:
    """Put `backup` in place of the ledger at `live` (see the module docstring)."""
    backup, live = Path(backup), Path(live)
    if live.exists() and backup.resolve() == live.resolve():
        raise BackupError("the backup and the ledger are the same file")
    backup_sha = _file_sha256(backup) if backup.exists() else None
    info = vet(backup)
    try:
        secret_file.make_private_folder(live.parent, "the ledger")
    except secret_file.UnsafeFileError as exc:
        raise BackupError(str(exc)) from None
    try:
        lock = run_lock.exclusive(live)
    except run_lock.LedgerBusy as exc:
        raise BackupError(str(exc)) from None
    staged = live.with_name(live.name + ".restoring")
    staged_files = [staged, *(Path(f"{staged}{s}") for s in Ledger.SIDE_FILES),
                    run_lock.lock_path(staged)]
    try:
        has_live = live.exists()
        if has_live:
            # Fold the live ledger's -wal into it and drop the side files, so the file
            # moved aside is complete on its own and nothing stale is left to replay.
            # Safe: the exclusive lock means nobody else has it open.
            try:
                secret_file.check_path(live, "the ledger", secret=True, folder=False)
                with closing(sqlite3.connect(live, timeout=10, isolation_level=None)) as db:
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    db.execute("PRAGMA journal_mode=DELETE")
                    if _integrity(db):
                        raise BackupError("the ledger being replaced fails its integrity "
                                          "check, so nothing can be carried forward from "
                                          "it; move it aside yourself (keep it) and restore "
                                          "again")
            except (sqlite3.DatabaseError, secret_file.UnsafeFileError) as exc:
                raise BackupError(f"can't read the ledger being replaced ({exc}); move it "
                                  "aside yourself (keep it) and restore again") from None
            leftovers = _side_files(live)
            if leftovers:
                raise BackupError("the ledger being replaced still has side files after "
                                  f"folding them in ({', '.join(p.name for p in leftovers)})"
                                  "; is another program using it?")
        for f in staged_files:
            f.unlink(missing_ok=True)
        secret_file.create_empty(staged, "the restored ledger")
        with closing(_ro(backup)) as src, closing(sqlite3.connect(staged)) as out:
            src.backup(out)
        # Bring an older backup up to the current layout (adds missing columns/indexes).
        Ledger(staged).close()
        run_lock.lock_path(staged).unlink(missing_ok=True)
        # uri=True so the ATTACH below opens the replaced ledger read-only.
        with closing(sqlite3.connect(staged.resolve().as_uri(), uri=True,
                                     isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=DELETE")
            if has_live:
                _attach(db, live, "live")
            plan = _compare(db, has_live)
            latest = max(x for x in (_latest(db, "main"),
                                     _latest(db, "live") if has_live else None, 0)
                         if x is not None)
            try:
                now = ledger_clock.tool_now(latest or None, wall)
            except ledger_clock.ClockBehindError as exc:
                raise BackupError(str(exc)) from None
            db.execute("BEGIN IMMEDIATE")
            try:
                for r in plan["revocations"]:
                    db.execute("UPDATE main.workers SET revoked_at=? WHERE worker_id=? AND "
                               "revoked_at IS NULL", (r["revoked_at"], r["worker_id"]))
                    db.execute("INSERT INTO main.events(job_id, worker_id, event, detail, at) "
                               "VALUES(NULL, ?, 'worker_revoked', ?, ?)",
                               (r["worker_id"], json.dumps({"carried_by": RULE}),
                                r["revoked_at"]))
                for e in plan.get("carried", []):
                    db.execute("INSERT INTO main.events(job_id, worker_id, event, detail, at) "
                               "VALUES(NULL, NULL, ?, ?, ?)", (e["event"], e["detail"], e["at"]))
                if plan["pause"] is not None:
                    db.execute("INSERT INTO main.events(job_id, worker_id, event, detail, at) "
                               "VALUES(NULL, NULL, 'coordinator_paused', ?, ?)",
                               (plan["pause"]["detail"], plan["pause"]["at"]))
                cancelled = db.execute(
                    "UPDATE main.enrollments SET expires_at=? WHERE used_at IS NULL AND "
                    "expires_at>?", (now, now)).rowcount
                replaced_name = f"{live.name}.replaced-{now}" if has_live else None
                detail = {
                    "rule": RULE, "backup_sha256": backup_sha,
                    "backup_newest_time": info["newest_time"], "replaced": replaced_name,
                    "revocations_carried": len(plan["revocations"]),
                    "records_carried": plan["carried_events"],
                    "pause_carried": plan["pause"] is not None,
                    "codes_cancelled": cancelled,
                    "rigs_to_reenroll": len(plan["rigs_to_reenroll"]),
                    "lost_jobs": plan["lost_jobs"],
                    "lost_results": sum(plan["lost_results"].values()),
                }
                db.execute("INSERT INTO main.events(job_id, worker_id, event, detail, at) "
                           "VALUES(NULL, NULL, ?, ?, ?)",
                           (RESTORED_EVENT, json.dumps(detail, sort_keys=True), now))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            if has_live:
                db.execute("DETACH DATABASE live")
            problems = _integrity(db)
        problems = problems or Ledger(staged, read_only=True).check_consistency()
        if problems:
            raise BackupError("the restored ledger fails its checks, nothing was changed: "
                              + "; ".join(problems[:3]))
        with open(staged, "rb+") as fh:
            os.fsync(fh.fileno())
        if has_live:
            os.replace(live, live.with_name(replaced_name))  # kept, owner-only, never deleted
        os.replace(staged, live)
        _fsync_folder(live.parent)
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"restore failed, nothing was changed: {exc}") from None
    finally:
        for f in staged_files:
            f.unlink(missing_ok=True)
        lock.release()
    plan.pop("carried", None)
    if plan["pause"] is not None:
        plan["pause"] = json.loads(plan["pause"]["detail"] or "{}").get("reason", "")
    return {"backup": info, **plan, "codes_cancelled": cancelled, "restored_at": now,
            "replaced": replaced_name}


# -- command line ---------------------------------------------------------------------
def _when(t: int | None) -> str:
    return "-" if t is None else time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))


def _print_plan(plan: dict[str, Any], done: bool) -> None:
    b = plan["backup"]
    print(f"backup: {b['jobs']} jobs, {b['results']} results, {b['rigs']} rigs, "
          f"newest entry {_when(b['newest_time'])}")
    will = "" if done else "would be "
    if not plan["live"]:
        print("WARNING: there is no ledger to replace, so nothing can be carried forward. "
              "Revoke again any rig you had revoked after this backup, and check the "
              "public logbook (reports/) for periods already signed.")
    else:
        print(f"lost (accepted after the backup): {plan['lost_jobs']} jobs, "
              f"{sum(plan['lost_results'].values())} results")
        for rig, n in list(plan["lost_results"].items())[:RIGS_SHOWN]:
            print(f"  {say(rig, 64)}: {n} results")
        for s in plan["signed_with_lost"]:
            print(f"WARNING: the signed report for {_when(s['since'])} to {_when(s['until'])}"
                  f" counts {s['results']} results the restored ledger won't have. Don't "
                  "sign that period again; publish a note next to it instead.")
        revs = ", ".join(say(r["worker_id"], 64) for r in plan["revocations"][:RIGS_SHOWN])
        print(f"revocations {will}carried forward: {len(plan['revocations'])}"
              + (f" ({revs})" if revs else ""))
        print(f"records of shared copies and signed reports {will}carried forward: "
              f"{plan['carried_events']}")
        if plan["pause"] is not None:
            print(f"the pause {will}carried forward (the ledger stays paused)")
        rigs = ", ".join(say(w, 64) for w in plan["rigs_to_reenroll"][:RIGS_SHOWN])
        print(f"rigs that must enroll again: {len(plan['rigs_to_reenroll'])}"
              + (f" ({rigs})" if rigs else ""))
    if plan["backup_paused"]:
        print("the backup was taken while paused; resume with `python -m app.incident "
              "resume` when ready")
    n = plan.get("codes_cancelled", plan["unused_codes"])
    print(f"unused enrollment codes {will}cancelled: {n} (make new ones with "
          "`python -m app.workers enroll`)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup",
                                     description="Back up and restore the ledger safely.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make", help="consistent owner-only backup while the coordinator runs")
    mk.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    mk.add_argument("--out", type=Path, required=True)
    ck = sub.add_parser("check", help="is this backup safe to restore? (changes nothing)")
    ck.add_argument("backup", type=Path)
    rs = sub.add_parser("restore", help="put a backup in place of the ledger "
                                        "(dry run unless --yes; coordinator stopped)")
    rs.add_argument("backup", type=Path)
    rs.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    rs.add_argument("--yes", action="store_true", help="really restore")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "make":
            out = make_backup(args.db, args.out)
            print(json.dumps(out, indent=2, sort_keys=True))
            print("The backup holds every secret the ledger does (hidden-test answers, key "
                  "hashes): keep it owner-only, and encrypt it before it leaves this machine.",
                  file=sys.stderr)
            return 0
        if args.cmd == "check":
            info = vet(args.backup)
            print(json.dumps(info | {"sha256": _file_sha256(args.backup)}, indent=2,
                             sort_keys=True))
            if info["consistency_problems"]:
                print("WARNING: the backup fails the ledger's consistency check",
                      file=sys.stderr)
                return 1
            return 0
        if not args.yes:
            if args.backup.exists() and args.db.exists() \
                    and args.backup.resolve() == args.db.resolve():
                raise BackupError("the backup and the ledger are the same file")
            plan = plan_restore(args.backup, args.db)
            _print_plan(plan, done=False)
            print("Nothing was changed. Stop the coordinator, then run again with --yes.")
            return 0
        done = restore(args.backup, args.db)
        _print_plan(done, done=True)
        if done["replaced"]:
            print(f"The old ledger was kept as {say(done['replaced'], 160)} (owner-only).")
        print("Restored. Start the coordinator again.")
        return 0
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
