"""Shareable ledger copies (threat model #10, second step).

A signed reward report proves *who* issued the numbers, not that they are right. To check
the numbers, someone needs the ledger they came from. This module makes a redacted copy of
the ledger, up to the end of a closed period, that anyone can re-run a report against:

    python -m app.ledger_copy export --db ./data/prototype.sqlite3 --until 1800000000 \\
        --out ledger-copy.sqlite3
    python -m app.rewards verify report.json --db ledger-copy.sqlite3 --public-key <hex>
    python -m app.ledger_copy compare older-copy.sqlite3 newer-copy.sqlite3  # app/ledger_compare.py

The copy keeps the structure the checks need (which job re-ran which, who did each job,
which workers share an owner, when things happened, points and settlement) so both the
report and `check_consistency` give the same answers as on the coordinator. It removes
everything that is secret or not needed:

    removed     worker keys (hashes), enrollment codes, prompts/payloads, job signatures,
                answers and answer hashes, expected answers of hidden tests, what a hidden
                test was aimed at (a rig or a trust tier; trust tiers are operator-only),
                event details
                (they can contain owner names), worker capabilities and last-seen times
    replaced    owner labels -> owner-1, owner-2, ... (same owner = same label)
                payload hashes -> HMAC with a random key that is thrown away, so equal
                inputs still look equal inside the copy but can't be matched to live jobs
    left out    jobs created at or after `until` (except re-checks of earlier jobs),
                events at or after `until`, workers that joined later

Rules: the period must be over, every job in it must be finished (no queued or leased
jobs, no re-check still waiting), and the source ledger must pass its own consistency
check. Deleted data is wiped (`secure_delete` + `VACUUM`), and the file is written to a
temporary name first, so a failed export never leaves a half-redacted copy behind.

Trade-off (be honest about it): for the closed period, the copy shows which jobs were
hidden re-runs and hidden tests and how they came out. Not their prompts or answers, but
the fact that they were tests. Outsiders need that to check that no worker audited itself.
It reveals nothing about jobs created after `until`. Share copies with a delay.
Because each rig remembers the inputs it ran, the export is recorded in the live ledger
(`ledger_copy_made`), and from then on the coordinator refuses to reuse the input of a
hidden test from that period, or a near copy of it, as a new hidden test (`test-reuse-v2`,
app/ledger.py). So always export from the live ledger: since `ledger-role-v1` (app/ledger_role.py) a
backup, or a ledger a restore moved aside, is refused, because the record would land there.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from . import copy_guard, ledger_clock, ledger_role
from .ledger import Ledger
from .screen import MAX_SCREEN_LINE_CHARS, say, say_lines

COPY_FORMAT = "bnh-ledger-copy/1"
OPEN_STATES = ("queued", "leased")


class CopyError(ValueError):
    pass


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _redact(db: sqlite3.Connection, until: int, now: int, salt: bytes) -> dict[str, int]:
    """Trim and scrub a fresh copy in place. Raises CopyError if the period isn't settled."""
    db.execute("CREATE TEMP TABLE keep(job_id TEXT PRIMARY KEY)")
    # Jobs created before `until`, plus every re-check (audit/tie-breaker) of those jobs,
    # even one created later: the settlement of an in-period result depends on it.
    db.execute(
        "WITH RECURSIVE k(job_id) AS (SELECT job_id FROM jobs WHERE created_at < ? "
        "UNION SELECT j.job_id FROM jobs j JOIN k ON j.audit_of = k.job_id) "
        "INSERT INTO keep SELECT job_id FROM k", (until,))
    marks = ",".join("?" * len(OPEN_STATES))
    still_open = db.execute(
        f"SELECT COUNT(*) FROM jobs WHERE job_id IN (SELECT job_id FROM keep) "
        f"AND status IN ({marks})", OPEN_STATES).fetchone()[0]
    if still_open:
        raise CopyError(f"{still_open} job(s) from this period, or re-checks of them, are "
                        "still queued or running; wait until they finish")
    pending = db.execute("SELECT COUNT(*) FROM submissions WHERE audit_status='pending' AND "
                         "job_id IN (SELECT job_id FROM keep)").fetchone()[0]
    if pending:
        raise CopyError(f"{pending} result(s) are still waiting for a re-check")

    not_kept = "NOT IN (SELECT job_id FROM keep)"
    db.execute(f"DELETE FROM job_exclusions WHERE job_id {not_kept}")
    db.execute(f"DELETE FROM submissions WHERE job_id {not_kept}")
    db.execute(f"DELETE FROM jobs WHERE job_id {not_kept}")
    db.execute(f"DELETE FROM events WHERE at >= ? OR (job_id IS NOT NULL AND job_id {not_kept})",
               (until,))
    db.execute("DELETE FROM events WHERE event LIKE 'enrollment%'")
    db.execute("UPDATE events SET detail = NULL")
    db.execute("DELETE FROM enrollments")

    # Workers: keep anyone who joined before `until` or shows up in what we kept.
    db.execute(
        "DELETE FROM workers WHERE registered_at >= ? AND worker_id NOT IN ("
        "SELECT worker_id FROM jobs WHERE worker_id IS NOT NULL "
        "UNION SELECT assigned_worker FROM jobs WHERE assigned_worker IS NOT NULL "
        "UNION SELECT worker_id FROM submissions "
        "UNION SELECT worker_id FROM job_exclusions "
        "UNION SELECT worker_id FROM events WHERE worker_id IS NOT NULL)", (until,))
    db.execute("UPDATE workers SET key_hash = NULL, capabilities = '[]', last_seen_at = NULL")
    db.execute("UPDATE workers SET revoked_at = NULL WHERE revoked_at >= ?", (until,))
    owners: dict[str, str] = {}
    for row in db.execute("SELECT owner FROM workers WHERE owner IS NOT NULL "
                          "ORDER BY registered_at, worker_id").fetchall():
        owners.setdefault(row[0], f"owner-{len(owners) + 1}")
    for real, label in owners.items():
        db.execute("UPDATE workers SET owner = ? WHERE owner = ?", (label, real))

    for (job_id, payload_hash) in db.execute("SELECT job_id, payload_hash FROM jobs").fetchall():
        masked = hmac.new(salt, payload_hash.encode("utf-8"), hashlib.sha256).hexdigest()
        db.execute("UPDATE jobs SET payload = '', signature = '', expected_result = NULL, "
                   "target = NULL, payload_hash = ? WHERE job_id = ?", (masked, job_id))
    db.execute("UPDATE submissions SET result = NULL, result_hash = ''")

    db.execute("CREATE TABLE copy_info (format TEXT NOT NULL, until INTEGER NOT NULL, "
               "created_at INTEGER NOT NULL, note TEXT NOT NULL)")
    db.execute("INSERT INTO copy_info VALUES (?,?,?,?)", (
        COPY_FORMAT, until, now,
        "Redacted ledger copy for re-running reward reports. Simulated points only."))
    counts = {}
    for table in ("jobs", "submissions", "workers", "events"):
        counts[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


def refuse_unless_live(source: Path) -> None:
    """`ledger-role-v1`: the record of a copy must land in the live ledger, or the
    coordinator would hand out the period's hidden tests again (`test-reuse-v2`)."""
    role = ledger_role.of_file(source)
    if role == ledger_role.BACKUP:
        raise CopyError("that file is a backup, not the live ledger: the record that this "
                        "period's hidden tests were shared would land in the backup, and "
                        "the coordinator would keep using them. Export from the live ledger")
    try:
        ledger_role.refuse_unless_live(role, "that file")
    except ledger_role.NotLiveLedger as exc:
        raise CopyError(f"{exc}; export from the live ledger") from None


def export_copy(source: Path, dest: Path, until: int, now: int,
                salt: bytes | None = None) -> dict[str, Any]:
    """Write a redacted copy of `source` covering everything before `until` to `dest`."""
    source, dest = Path(source), Path(dest)
    if until > now:
        raise CopyError("period is still open; export only after `until` has passed")
    if not source.exists():
        raise CopyError(f"database not found: {source}")
    if dest.resolve() == source.resolve():
        raise CopyError("the copy must be a different file from the ledger")
    if dest.exists():
        raise CopyError(f"{dest} already exists; pick a new file name")
    try:
        source_info = copy_info(source)
    except sqlite3.DatabaseError as exc:
        raise CopyError(f"not a readable ledger: {exc}") from None
    if source_info is not None:
        # Re-exporting a copy would also write the export record into a shared file.
        raise CopyError("that file is already a ledger copy; export from the live ledger")
    refuse_unless_live(source)
    problems = Ledger(source).check_consistency()
    if problems:
        raise CopyError("ledger fails its own consistency check; fix it before sharing: "
                        + "; ".join(problems[:3]))

    partial = dest.with_name(dest.name + ".partial")
    for leftover in (partial, Path(f"{partial}-journal")):
        leftover.unlink(missing_ok=True)
    try:
        # The backup API gives a consistent snapshot even while the coordinator is running.
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(partial)) as out:
            src.backup(out)
        with closing(sqlite3.connect(partial, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal
            db.execute("PRAGMA secure_delete=ON")     # overwrite deleted rows with zeros
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                counts = _redact(db, until, now, salt or os.urandom(32))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("VACUUM")  # drop free pages so nothing deleted can be recovered
        # Redaction must not break any invariant the report relies on.
        # Vetted like a stranger's copy would be, so every honest export opens cleanly.
        try:
            after = Ledger(partial, untrusted=True).check_consistency()
        except copy_guard.UnsafeCopyError as exc:
            raise CopyError(f"redacted copy fails the copy check: {exc}") from None
        if after:
            raise CopyError("redacted copy fails the consistency check: " + "; ".join(after[:3]))
        partial.replace(dest)
        file_sha256 = _file_sha256(dest)
        # `test-reuse-v2`: from now on the hidden tests in this period are known to the rigs
        # that ran them. If this can't be written down, don't leave the copy around either.
        try:
            Ledger(source).record_copy(now, until, file_sha256)
        except Exception as exc:  # noqa: BLE001 - any failure means "not recorded"
            dest.unlink(missing_ok=True)
            raise CopyError("could not record the copy in the ledger, so it was deleted "
                            f"({type(exc).__name__}); nothing was shared") from None
    finally:
        for leftover in (partial, Path(f"{partial}-wal"), Path(f"{partial}-shm"),
                         Path(f"{partial}-journal")):
            leftover.unlink(missing_ok=True)
    return {"format": COPY_FORMAT, "until": until, "created_at": now, **counts,
            "file_sha256": file_sha256}


def copy_info(path: Path) -> dict[str, Any] | None:
    """The copy's header, or None if `path` is a normal (unredacted) ledger.

    A copy may come from anyone, so it is vetted before it is believed (app/copy_guard.py,
    `copy-open-v1`): plain ledger tables only, intact, one `copy_info` row. Raises
    copy_guard.UnsafeCopyError (a sqlite3.DatabaseError) otherwise. Opening is read-only
    and immutable, so it changes no bytes; a live WAL ledger still answers None."""
    path = Path(path)
    if not copy_guard.has_copy_info(path):
        return None
    return copy_guard.vet(path)


def _compare_cli(older: Path, newer: Path, out: Path | None) -> int:
    from .ledger_compare import CompareError, compare_copies  # it imports this module

    if out is not None and out.exists():
        print(f"{out} already exists; pick a new file name", file=sys.stderr)
        return 2
    try:
        result = compare_copies(older, newer)
    except CompareError as exc:
        print(say(str(exc), MAX_SCREEN_LINE_CHARS), file=sys.stderr)
        return 2
    # Both copies may come from someone else, and every name in a problem line (rigs, jobs,
    # events) is read from them, so all of it is printed safely (`operator-screen-v1`). The
    # --out JSON keeps the exact text.
    o, n, c = result["older"], result["newer"], result["compared"]
    lines = [f"older: {o['file']} until={o['until']} sha256={o['sha256']}",
             f"newer: {n['file']} until={n['until']} sha256={n['sha256']}",
             f"compared {c['jobs']} jobs, {c['results']} results, {c['rigs']} rigs, "
             f"{c['events']} events from the older copy"]
    if result["ok"]:
        lines.append("OK: the newer copy keeps the older copy's history unchanged "
                     "(nothing edited, deleted or back-dated)")
    else:
        lines.append(f"PROBLEM: {result['problem_count']} difference(s) in history that was "
                     "already shared:")
        lines += [f"  - {line}" for line in result["problems"]]
        hidden = result["problem_count"] - len(result["problems"])
        if hidden:
            lines.append(f"  ... and {hidden} more")
    lines.append(result["note"])
    sys.stdout.write(say_lines(lines))
    if out is not None:
        tmp = out.with_name(out.name + ".partial")
        tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        tmp.replace(out)
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ledger_copy",
                                     description="Redacted ledger copies for re-running reports.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    exp = sub.add_parser("export", help="write a redacted copy up to the end of a closed period")
    exp.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    exp.add_argument("--until", type=int, required=True,
                     help="unix seconds; must be in the past (same `until` as the report)")
    exp.add_argument("--out", type=Path, required=True)
    info = sub.add_parser("info", help="show what a copy covers")
    info.add_argument("copy", type=Path)
    cmp_ = sub.add_parser("compare", help="check that a newer copy kept an older copy's "
                          "history unchanged (nothing edited, deleted or back-dated)")
    cmp_.add_argument("older", type=Path)
    cmp_.add_argument("newer", type=Path)
    cmp_.add_argument("--out", type=Path, help="also write the result as JSON (never overwrites)")
    args = parser.parse_args(argv)

    if args.cmd == "compare":
        return _compare_cli(args.older, args.newer, args.out)

    if args.cmd == "info":
        if not args.copy.exists():
            print(f"file not found: {args.copy}", file=sys.stderr)
            return 2
        try:
            meta = copy_info(args.copy)
        except sqlite3.DatabaseError as exc:  # includes copy_guard.UnsafeCopyError
            print(say(f"refusing to read {args.copy.name}: {exc}", MAX_SCREEN_LINE_CHARS),
                  file=sys.stderr)
            return 2
        if meta is None:
            print("not a ledger copy (this looks like a full, unredacted ledger)")
            return 1
        # The header was written by whoever made the copy (`operator-screen-v1`).
        sys.stdout.write(say_lines([
            f"format={meta['format']} until={meta['until']} created_at={meta['created_at']}",
            f"sha256={_file_sha256(args.copy)}", meta["note"]]))
        return 0

    try:
        # `ledger-role-v1`: say so before anything else (the clock rule below would
        # otherwise read the backup's times).
        refuse_unless_live(args.db)
        # `ledger-clock-v1`: the export is recorded in the live ledger, never with a time
        # earlier than one already there (and `until` must not be after it).
        now = ledger_clock.tool_now(Ledger(args.db, read_only=True).latest_time()) \
            if args.db.exists() else int(time.time())
        out = export_copy(args.db, args.out, args.until, now)
    except (CopyError, ledger_clock.ClockBehindError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote {args.out} until={out['until']} jobs={out['jobs']} "
          f"results={out['submissions']} workers={out['workers']} sha256={out['file_sha256']}")
    print("Check a signed report against it with: python -m app.rewards verify report.json "
          f"--db {args.out} --public-key <coordinator key>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
