"""Compare two ledger copies: did the history of the earlier period change? (threat model #10)

A ledger copy (`app.ledger_copy`) lets anyone re-run a signed reward report. But the same
operator makes every copy, so a quiet edit is still possible: share an honest copy for
period 1, then later rewrite period 1 (move points, drop a failed hidden test) and share a
period-2 copy that includes the rewritten history. Each copy passes its own checks.

This tool catches that. Copies are cumulative (each one holds everything up to its `until`),
so a later copy must contain the earlier copy's history unchanged:

    python -m app.ledger_copy compare older-copy.sqlite3 newer-copy.sqlite3

It checks, row by row, that:
  * every job, result, hidden-test exclusion, rig and event in the older copy is still there,
    with the same values (nothing edited or deleted);
  * the newer copy adds nothing to the older period: no job created, rig registered or
    event logged before the older copy's `until` that the older copy didn't have
    (nothing back-dated);
  * a reward report for everything up to the older `until` comes out identical from both.

Two things are redacted differently in every copy and are compared by *shape* instead:
  * owner labels (`owner-1`, ...): rigs that shared an owner must still share one, and rigs
    that didn't, still don't;
  * masked input hashes (fresh random key per copy): jobs that had equal inputs must still
    have equal inputs, and different ones still different.
One field may legitimately change: a rig revoked after the older copy's `until` shows as
not revoked in the older copy (revocations after `until` are left out of a copy).

What it proves, honestly: that the operator didn't rewrite already-shared history between
two copies. It does not prove the first copy was true, and it only helps if someone kept the
older copy. It reads both files and changes nothing.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .ledger import Ledger
from .ledger_copy import COPY_FORMAT, _file_sha256, copy_info
from .rewards import build_report

COMPARE_FORMAT = "bnh-ledger-compare/1"
MAX_LISTED = 50  # problems listed one by one; the total count is always exact

# Columns compared for exact equality. Anything not listed here is compared by shape
# (owner, payload_hash) or by the revocation rule (revoked_at).
JOB_COLUMNS = ("task_type", "payload", "reward_points", "expires_at", "signature", "status",
               "worker_id", "lease_expires_at", "attempts", "max_attempts", "created_at",
               "updated_at", "job_kind", "audit_of", "expected_result", "assigned_worker",
               "assigned_until", "schema_version", "model_sha256", "target")
SUBMISSION_COLUMNS = ("worker_id", "result_hash", "reward_points", "accepted_at",
                      "verifier_method", "confidence", "result", "audit_status")
WORKER_COLUMNS = ("capabilities", "registered_at", "key_hash", "last_seen_at")
EVENT_COLUMNS = ("job_id", "worker_id", "event", "detail", "at")


class CompareError(ValueError):
    """The inputs can't be compared at all (not copies, wrong order, unreadable)."""


def _open(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _table(db: sqlite3.Connection, sql: str, key: str) -> dict[Any, dict[str, Any]]:
    return {row[key]: dict(row) for row in db.execute(sql).fetchall()}


def _load(path: Path) -> dict[str, Any]:
    with closing(_open(path)) as db:
        return {
            "jobs": _table(db, "SELECT * FROM jobs", "job_id"),
            "submissions": _table(db, "SELECT * FROM submissions", "job_id"),
            "workers": _table(db, "SELECT * FROM workers", "worker_id"),
            "events": _table(db, "SELECT * FROM events", "id"),
            "exclusions": {(r["job_id"], r["worker_id"]) for r in
                           db.execute("SELECT job_id, worker_id FROM job_exclusions")},
        }


def _same_shape(old: dict[Any, Any], new: dict[Any, Any], what: str) -> list[str]:
    """`old` and `new` map the same ids to labels. Equal labels must stay equal and different
    labels must stay different (labels themselves may be renamed between copies)."""
    problems: list[str] = []
    forward: dict[Any, Any] = {}
    backward: dict[Any, Any] = {}
    for key in sorted(old, key=str):
        a, b = old[key], new.get(key)
        if (a is None) != (b is None):
            problems.append(f"{what} of {key} was {'removed' if b is None else 'added'}")
            continue
        if a is None:
            continue
        if forward.setdefault(a, b) != b:
            problems.append(f"{what} of {key} no longer matches the others it matched before")
        elif backward.setdefault(b, a) != a:
            problems.append(f"{what} of {key} now matches one it was different from before")
    return problems


def _diff_rows(old: dict[Any, dict], new: dict[Any, dict], columns: tuple[str, ...],
               label: str) -> list[str]:
    problems = []
    for key in sorted(old, key=str):
        if key not in new:
            problems.append(f"{label} {key} was deleted")
            continue
        changed = [c for c in columns if old[key].get(c) != new[key].get(c)]
        if changed:
            problems.append(f"{label} {key} changed: {', '.join(changed)}")
    return problems


def compare_copies(older: Path, newer: Path) -> dict[str, Any]:
    """Return a result dict; `result["ok"]` is True when the older history is intact."""
    older, newer = Path(older), Path(newer)
    for p in (older, newer):
        if not p.exists():
            raise CompareError(f"file not found: {p}")
    try:
        meta_old, meta_new = copy_info(older), copy_info(newer)
    except sqlite3.DatabaseError as exc:
        raise CompareError(f"not a readable ledger file: {exc}") from exc
    for p, meta in ((older, meta_old), (newer, meta_new)):
        if meta is None:
            raise CompareError(f"{p.name} is a full ledger, not a shared copy; "
                               "compare two copies made with `ledger_copy export`")
        if meta["format"] != COPY_FORMAT:
            raise CompareError(f"{p.name} has unknown format {meta['format']!r}")
    until = meta_old["until"]
    if meta_new["until"] < until:
        raise CompareError(f"{newer.name} ends earlier ({meta_new['until']}) than {older.name} "
                           f"({until}); give the older copy first")

    problems: list[str] = []
    # Each copy must also be sound on its own.
    for p in (older, newer):
        for issue in Ledger(p, read_only=True).check_consistency():
            problems.append(f"{p.name} fails its own consistency check: {issue}")

    a, b = _load(older), _load(newer)

    # 1. Nothing in the older copy was edited or deleted.
    problems += _diff_rows(a["jobs"], b["jobs"], JOB_COLUMNS, "job")
    problems += _diff_rows(a["submissions"], b["submissions"], SUBMISSION_COLUMNS, "result for")
    problems += _diff_rows(a["workers"], b["workers"], WORKER_COLUMNS, "rig")
    problems += _diff_rows(a["events"], b["events"], EVENT_COLUMNS, "event")
    for job_id, worker_id in sorted(a["exclusions"] - b["exclusions"]):
        problems.append(f"exclusion of {worker_id} from job {job_id} was deleted")
    for job_id, worker_id in sorted(b["exclusions"] - a["exclusions"]):
        if job_id in a["jobs"]:
            problems.append(f"exclusion of {worker_id} from job {job_id} was added later")

    # Revocations after the older `until` are blanked in the older copy; anything else must
    # match exactly.
    for worker_id, old_row in sorted(a["workers"].items()):
        new_row = b["workers"].get(worker_id)
        if new_row is None:
            continue
        was, now = old_row["revoked_at"], new_row["revoked_at"]
        if was is not None and was != now:
            problems.append(f"rig {worker_id} changed: revoked_at")
        elif was is None and now is not None and now < until:
            problems.append(f"rig {worker_id} is now shown as revoked before {until}, "
                            "but the older copy had it active")

    # Shapes: who shared an owner, which jobs had equal inputs.
    shared_workers = [w for w in a["workers"] if w in b["workers"]]
    problems += _same_shape({w: a["workers"][w]["owner"] for w in shared_workers},
                            {w: b["workers"][w]["owner"] for w in shared_workers},
                            "owner label")
    shared_jobs = [j for j in a["jobs"] if j in b["jobs"]]
    problems += _same_shape({j: a["jobs"][j]["payload_hash"] for j in shared_jobs},
                            {j: b["jobs"][j]["payload_hash"] for j in shared_jobs},
                            "input hash")

    # 2. Nothing back-dated into the older period.
    for job_id, row in sorted(b["jobs"].items()):
        if job_id not in a["jobs"] and row["created_at"] < until:
            problems.append(f"job {job_id} appears with a creation time before {until}, "
                            "but the older copy didn't have it")
    for job_id in sorted(set(b["submissions"]) - set(a["submissions"])):
        if job_id in a["jobs"] and b["submissions"][job_id]["accepted_at"] < until:
            problems.append(f"result for {job_id} appears accepted before {until}, "
                                "but the older copy didn't have it")
    for worker_id, row in sorted(b["workers"].items()):
        if worker_id not in a["workers"] and row["registered_at"] < until:
            problems.append(f"rig {worker_id} appears registered before {until}, "
                            "but the older copy didn't have it")
    for event_id, row in sorted(b["events"].items()):
        if event_id not in a["events"] and row["at"] < until:
            problems.append(f"event {event_id} ({row['event']}) appears before {until}, "
                            "but the older copy didn't have it")

    # 3. Same numbers: a report up to the older `until` must be identical from both copies.
    report_old = report_new = None
    if until > 0:
        report_old = build_report(Ledger(older, read_only=True), 0, until)["report_sha256"]
        report_new = build_report(Ledger(newer, read_only=True), 0, until)["report_sha256"]
        if report_old != report_new:
            problems.append(f"a reward report for everything before {until} differs between "
                            "the two copies")

    return {
        "format": COMPARE_FORMAT,
        "older": {"file": older.name, "sha256": _file_sha256(older), "until": until},
        "newer": {"file": newer.name, "sha256": _file_sha256(newer),
                  "until": meta_new["until"]},
        "compared": {"jobs": len(a["jobs"]), "results": len(a["submissions"]),
                     "rigs": len(a["workers"]), "events": len(a["events"])},
        "report_sha256_up_to_older_until": report_old if report_old == report_new else None,
        "problem_count": len(problems),
        "problems": problems[:MAX_LISTED],
        "ok": not problems,
        "note": ("Checks that history shared in the older copy was not changed or added to "
                 "later. It does not prove the older copy itself was true. Simulated points "
                 "only."),
    }
