"""Shareable ledger copies (threat model #10, second step).

A signed reward report proves *who* issued the numbers, not that they are right. To check
the numbers, someone needs the ledger they came from. This module makes a redacted copy of
the ledger, up to the end of a closed period, that anyone can re-run a report against:

    python -m app.ledger_copy export --db ./data/prototype.sqlite3 --until 1800000000 \\
        --out ledger-copy.sqlite3
    python -m app.rewards verify report.json --db ledger-copy.sqlite3 --public-key <hex>

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
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .ledger import Ledger

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
        after = Ledger(partial, read_only=True).check_consistency()
        if after:
            raise CopyError("redacted copy fails the consistency check: " + "; ".join(after[:3]))
        partial.replace(dest)
    finally:
        for leftover in (partial, Path(f"{partial}-wal"), Path(f"{partial}-shm"),
                         Path(f"{partial}-journal")):
            leftover.unlink(missing_ok=True)
    return {"format": COPY_FORMAT, "until": until, "created_at": now, **counts,
            "file_sha256": _file_sha256(dest)}


def copy_info(path: Path) -> dict[str, Any] | None:
    """The copy's header, or None if `path` is a normal (unredacted) ledger."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    # A plain read: it changes no bytes, and unlike mode=ro it also works on a live WAL ledger.
    with closing(sqlite3.connect(path)) as db:
        has = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='copy_info'"
                         ).fetchone()
        if not has:
            return None
        row = db.execute("SELECT format, until, created_at, note FROM copy_info").fetchone()
    return {"format": row[0], "until": row[1], "created_at": row[2], "note": row[3]}


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
    args = parser.parse_args(argv)

    if args.cmd == "info":
        if not args.copy.exists():
            print(f"file not found: {args.copy}", file=sys.stderr)
            return 2
        meta = copy_info(args.copy)
        if meta is None:
            print("not a ledger copy (this looks like a full, unredacted ledger)")
            return 1
        print(f"format={meta['format']} until={meta['until']} created_at={meta['created_at']}")
        print(f"sha256={_file_sha256(args.copy)}")
        print(meta["note"])
        return 0

    try:
        out = export_copy(args.db, args.out, args.until, int(time.time()))
    except CopyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote {args.out} until={out['until']} jobs={out['jobs']} "
          f"results={out['submissions']} workers={out['workers']} sha256={out['file_sha256']}")
    print("Check a signed report against it with: python -m app.rewards verify report.json "
          f"--db {args.out} --public-key <coordinator key>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
