"""Durable SQLite store for jobs, leases, workers, accepted results and an audit trail.

Job state machine (every transition is written to the `events` table):

    queued --claim--> leased --correct result--> accepted
       ^                 |
       |                 +--wrong result / lease timeout--> queued   (attempts left)
       |                 +--wrong result, no attempts left--> rejected
       |                 +--lease timeout, no attempts left--> failed
       +--(any non-final state past the job deadline)--------> expired

All state changes run inside `BEGIN IMMEDIATE` transactions, so two workers (or two
coordinator processes sharing the database file) can never lease the same job.

Phase 4 verification. Every job has a `job_kind`:
    work       normal job
    audit      a hidden re-run of an accepted "schema-only" work result, which can never be
               leased to the worker that produced the original (`job_exclusions`)
    challenge  a hidden known-answer job created by the operator (`expected_result`)
    tiebreak   a hidden third run of a disputed job, excluded from both the original worker
               and the auditor; `audit_of` points at the original work job
Workers see the same envelope for all four.

Auditor assignment (threat model #4). An audit or tie-breaker is not first-come: the
coordinator hands it, as soon as it is queued, to one eligible worker chosen at random from
the workers seen in the last `active_seconds` (`assigned_worker`). Only that worker can claim it, so polling faster
does not win more audits. If the chosen worker doesn't claim it within `assign_seconds`, is
revoked, or gives it back (lease timeout, rejected result), it is re-assigned at random,
avoiding the previous pick when anyone else is eligible. Each accepted submission carries an
`audit_status`:
    not-needed        the coordinator checked the answer exactly
    unaudited         unproven and not re-run (not sampled, or the re-run never happened)
    pending           a re-run is queued
    confirmed         an independent re-run agreed (confidence becomes "replicated")
    disputed          an independent re-run disagreed; both results are held while a
                      tie-breaker runs (or until a person looks, if it never runs)
    outvoted          the tie-breaker agreed with the other side; this result stays held
    unresolved        the tie-breaker agreed with neither side (or with both); all held
    passed-challenge / failed-challenge   outcome of a hidden known-answer job

Emergency pause (`pause-v1`, see app/incident.py). The operator can pause the coordinator
during an incident. While paused: no job is leased, no job or hidden test is created, no
enrollment code is issued or used, and no audit/tie-breaker is assigned (so nobody's
"not picked up" count grows because of the pause). Results for jobs leased before the pause
are still accepted, and revocation still works. The state is the latest
`coordinator_paused` / `coordinator_resumed` event, so it survives a restart and is part of
the audit trail. On resume, audit assignments the pause interrupted get a fresh window.
"""
from __future__ import annotations

import json
import random
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .audit_policy import AUDITOR_PICK_RULES, PASSED_STATUSES, STRIKE_STATUSES, auditor_pool

FINAL_STATES = ("accepted", "rejected", "expired", "failed")

# Emergency pause (`pause-v1`): the latest of these two events is the coordinator's state.
PAUSE_VERSION = "pause-v1"
PAUSE_EVENTS = ("coordinator_paused", "coordinator_resumed")
MAX_PAUSE_REASON = 200


class CoordinatorPaused(Exception):
    """Refused because the operator paused the coordinator (see app/incident.py)."""

    def __init__(self, since: int):
        super().__init__("coordinator is paused")
        self.since = since

# Public settlement buckets. Collapsing the detailed audit status keeps the public ledger
# from revealing which jobs were hidden challenges.
SETTLEMENT = {
    "not-needed": "confirmed", "confirmed": "confirmed", "passed-challenge": "confirmed",
    "pending": "pending", "unaudited": "unverified",
    "disputed": "held", "failed-challenge": "held", "outvoted": "held", "unresolved": "held",
}

Agree = Callable[[str, str], bool]

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL,
    registered_at INTEGER NOT NULL,
    key_hash TEXT,
    last_seen_at INTEGER,
    owner TEXT,
    revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS enrollments (
    code_hash TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER,
    worker_id TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    reward_points INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    signature TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('queued','leased','accepted','rejected','expired','failed')),
    worker_id TEXT,
    lease_expires_at INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    job_kind TEXT NOT NULL DEFAULT 'work',
    audit_of TEXT,
    expected_result TEXT,
    assigned_worker TEXT,
    assigned_until INTEGER,
    schema_version TEXT NOT NULL DEFAULT 'bnh-job/0',
    model_sha256 TEXT,
    target TEXT
);
CREATE TABLE IF NOT EXISTS job_exclusions (
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    worker_id TEXT NOT NULL,
    PRIMARY KEY (job_id, worker_id)
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS submissions (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    worker_id TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    reward_points INTEGER NOT NULL,
    accepted_at INTEGER NOT NULL,
    verifier_method TEXT NOT NULL DEFAULT 'deterministic-recompute',
    confidence TEXT NOT NULL DEFAULT 'exact',
    result TEXT,
    audit_status TEXT NOT NULL DEFAULT 'not-needed'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    worker_id TEXT,
    event TEXT NOT NULL,
    detail TEXT,
    at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_event ON events(event, id);
"""


MIGRATIONS = {
    "workers": (
        ("key_hash", "TEXT"),
        ("owner", "TEXT"),
        ("revoked_at", "INTEGER"),
        ("last_seen_at", "INTEGER"),
    ),
    "submissions": (
        ("verifier_method", "TEXT NOT NULL DEFAULT 'deterministic-recompute'"),
        ("confidence", "TEXT NOT NULL DEFAULT 'exact'"),
        ("result", "TEXT"),
        ("audit_status", "TEXT NOT NULL DEFAULT 'not-needed'"),
    ),
    "jobs": (
        ("job_kind", "TEXT NOT NULL DEFAULT 'work'"),
        ("audit_of", "TEXT"),
        ("expected_result", "TEXT"),
        ("assigned_worker", "TEXT"),
        ("assigned_until", "INTEGER"),
        # Jobs from before versioning were signed without these; 'bnh-job/0' marks them
        # so workers refuse them instead of failing on a confusing signature error.
        ("schema_version", "TEXT NOT NULL DEFAULT 'bnh-job/0'"),
        ("model_sha256", "TEXT"),
        # Aimed hidden tests: 'worker:<id>' or 'tier:<new|suspect|established>'.
        ("target", "TEXT"),
    ),
}


# Job kinds that go to a randomly chosen worker instead of whoever asks first.
ASSIGNED_KINDS = ("audit", "tiebreak")

# Aimed hidden tests (threat model #1 and #8): a challenge with a `target` goes only to the
# worker it names, or to a random active worker in the named trust tier, instead of to
# whoever asks first. Their private events have their own names, so they never count
# towards a rig's audit follow-through.
TARGET_TIERS = ("new", "suspect", "established")
_EVENTS = {
    "audit": ("auditor_assigned", "auditor_unassigned", "audit_assignment_expired"),
    "challenge": ("test_assigned", "test_unassigned", "test_assignment_expired"),
}


def parse_target(target: str | None) -> tuple[str, str] | None:
    """'worker:wk-a' -> ('worker', 'wk-a'); 'tier:new' -> ('tier', 'new'); None -> None."""
    if target is None:
        return None
    kind, _, value = target.partition(":")
    if kind == "worker" and value:
        return kind, value
    if kind == "tier" and value in TARGET_TIERS:
        return kind, value
    raise ValueError(f"bad target {target!r}")


class Ledger:
    def __init__(self, path: Path, rng: random.Random | None = None,
                 active_seconds: int = 300, assign_seconds: int = 120,
                 read_only: bool = False,
                 tier_of: Callable[[dict[str, int]], str] | None = None,
                 auditor_pick: str = "uniform"):
        """`rng` picks auditors; the default uses the OS's secure randomness so workers
        can't predict who gets an audit. A worker counts as active for `active_seconds`
        after it last asked for a job, sent a result, or enrolled. An assigned worker has
        `assign_seconds` to claim its audit before someone else gets it.
        `read_only` opens an existing file without changing a single byte (no WAL switch,
        no migrations), for checking a shared ledger copy whose sha256 was published.
        `tier_of` maps a worker's audit_history() counts to its trust tier (see
        app/audit_policy.py); without it, hidden tests aimed at a tier find nobody.
        `auditor_pick` is "uniform" (any eligible worker) or "trusted-first" (audits and
        tie-breakers go to the most trusted tier that has an eligible worker; needs
        `tier_of`, see audit_policy.auditor_pool)."""
        if auditor_pick not in AUDITOR_PICK_RULES:
            raise ValueError(f"unknown auditor_pick {auditor_pick!r}")
        if auditor_pick == "trusted-first" and tier_of is None:
            raise ValueError("auditor_pick='trusted-first' needs tier_of")
        self.path = Path(path)
        self.rng = rng or random.SystemRandom()
        self.active_seconds = active_seconds
        self.assign_seconds = assign_seconds
        self.read_only = read_only
        self.tier_of = tier_of
        self.auditor_pick = auditor_pick
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            # Migrate older (Phase 1-3) databases in place.
            for table, columns in MIGRATIONS.items():
                have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
                for name, ddl in columns:
                    if name not in have:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            # After the migration, so older databases have the column first.
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS workers_key_hash ON workers(key_hash)")

    # -- plumbing -----------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True,
                                 timeout=10, isolation_level=None)
        else:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction that takes the database write lock up front."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db: sqlite3.Connection, now: int, event: str, job_id: str | None = None,
               worker_id: str | None = None, detail: str | None = None) -> None:
        db.execute("INSERT INTO events(job_id, worker_id, event, detail, at) VALUES(?,?,?,?,?)",
                   (job_id, worker_id, event, detail, now))

    # -- workers and enrollment ------------------------------------------------------
    # Only sha256 hashes of enrollment codes and worker keys are stored. Both are 256-bit
    # random tokens, so a fast hash is enough: there is nothing to brute-force.
    def create_enrollment(self, code_hash: str, owner: str, now: int, expires_at: int) -> None:
        with self._tx() as db:
            self._refuse_if_paused(db)
            db.execute("INSERT INTO enrollments(code_hash, owner, created_at, expires_at) "
                       "VALUES(?,?,?,?)", (code_hash, owner, now, expires_at))
            self._event(db, now, "enrollment_created", detail=f"owner={owner}")

    def enroll_worker(self, worker_id: str, capabilities: str, code_hash: str, key_hash: str,
                      now: int) -> tuple[str, str | None]:
        """Use a one-time enrollment code to register `worker_id` with a new key.
        Returns (outcome, owner): outcome is enrolled, bad_code, taken or revoked.
        A failed attempt never consumes the code. Raises CoordinatorPaused while paused
        (the code stays unused)."""
        with self._tx() as db:
            self._refuse_if_paused(db)
            code = db.execute("SELECT * FROM enrollments WHERE code_hash=?",
                              (code_hash,)).fetchone()
            if code is None or code["used_at"] is not None or code["expires_at"] <= now:
                return "bad_code", None
            worker = db.execute("SELECT * FROM workers WHERE worker_id=?",
                                (worker_id,)).fetchone()
            if worker is not None and worker["revoked_at"] is not None:
                return "revoked", None
            if worker is not None and worker["key_hash"] is not None:
                return "taken", None  # an enrollment code can't take over a keyed worker
            owner = code["owner"]
            if worker is None:
                db.execute("INSERT INTO workers(worker_id, capabilities, registered_at, "
                           "key_hash, owner, last_seen_at) VALUES(?,?,?,?,?,?)",
                           (worker_id, capabilities, now, key_hash, owner, now))
            else:  # a keyless worker from an older (pre-auth) database
                db.execute("UPDATE workers SET capabilities=?, key_hash=?, owner=?, "
                           "last_seen_at=? WHERE worker_id=?",
                           (capabilities, key_hash, owner, now, worker_id))
            db.execute("UPDATE enrollments SET used_at=?, worker_id=? WHERE code_hash=?",
                       (now, worker_id, code_hash))
            self._event(db, now, "worker_enrolled", worker_id=worker_id, detail=f"owner={owner}")
            return "enrolled", owner

    def update_capabilities(self, worker_id: str, capabilities: str, now: int) -> None:
        with self._tx() as db:
            db.execute("UPDATE workers SET capabilities=? WHERE worker_id=?",
                       (capabilities, worker_id))
            self._event(db, now, "worker_updated", worker_id=worker_id)

    def worker_for_key(self, key_hash: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM workers WHERE key_hash=?", (key_hash,)).fetchone()
            return dict(row) if row else None

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
            return dict(row) if row else None

    def list_workers(self) -> list[dict[str, Any]]:
        """Operator view of every worker. Never includes key hashes."""
        with closing(self._connect()) as db:
            rows = db.execute("SELECT worker_id, owner, registered_at, revoked_at, "
                              "key_hash IS NOT NULL AS has_key FROM workers "
                              "ORDER BY worker_id").fetchall()
            return [dict(r) | {"has_key": bool(r["has_key"])} for r in rows]

    def has_worker(self, worker_id: str) -> bool:
        return self.get_worker(worker_id) is not None

    def revoke_worker(self, worker_id: str, now: int) -> bool:
        """Block a worker's key and take back any job it holds right now.
        Its already-accepted results stay as they are, for the operator to review."""
        with self._tx() as db:
            cur = db.execute("UPDATE workers SET revoked_at=? WHERE worker_id=? "
                             "AND revoked_at IS NULL", (now, worker_id))
            if not cur.rowcount:
                return False
            self._event(db, now, "worker_revoked", worker_id=worker_id)
            for row in db.execute("SELECT job_id, attempts, max_attempts FROM jobs "
                                  "WHERE status='leased' AND worker_id=?",
                                  (worker_id,)).fetchall():
                final = row["attempts"] >= row["max_attempts"]
                db.execute("UPDATE jobs SET status=?, worker_id=NULL, lease_expires_at=NULL, "
                           "updated_at=?, assigned_until=? WHERE job_id=?",
                           ("failed" if final else "queued", now, now, row["job_id"]))
                self._event(db, now, "lease_revoked_failed" if final else
                            "lease_revoked_requeued", row["job_id"], worker_id)
                if final:
                    self._audit_not_run(db, row["job_id"], now)
            return True

    # -- jobs -----------------------------------------------------------------------
    def create_job(self, fields: dict[str, Any], signature: str, max_attempts: int,
                   now: int, job_kind: str = "work", expected_result: str | None = None,
                   target: str | None = None) -> None:
        """`target` (hidden tests only, see parse_target) aims the job at one worker or at
        a trust tier; it is assigned in the same transaction, like an audit."""
        if target is not None and (job_kind != "challenge" or parse_target(target) is None):
            raise ValueError("only hidden test jobs can have a target")
        with self._tx() as db:
            self._refuse_if_paused(db)
            self._insert_job(db, fields, signature, max_attempts, now, job_kind,
                             expected_result=expected_result, target=target)
            if target is not None:
                self._assign_auditors(db, now)

    def _insert_job(self, db: sqlite3.Connection, fields: dict[str, Any], signature: str,
                    max_attempts: int, now: int, job_kind: str, audit_of: str | None = None,
                    expected_result: str | None = None,
                    exclude_workers: tuple[str, ...] = (),
                    target: str | None = None) -> None:
        db.execute(
            "INSERT INTO jobs(job_id, task_type, payload, payload_hash, reward_points, "
            "expires_at, signature, status, max_attempts, created_at, updated_at, job_kind, "
            "audit_of, expected_result, schema_version, model_sha256, target) "
            "VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?)",
            (fields["job_id"], fields["task_type"], fields["payload"], fields["payload_hash"],
             fields["reward_points"], fields["expires_at"], signature, max_attempts, now, now,
             job_kind, audit_of, expected_result, fields["schema_version"],
             fields["model_sha256"], target),
        )
        for worker_id in exclude_workers:
            db.execute("INSERT INTO job_exclusions(job_id, worker_id) VALUES(?,?)",
                       (fields["job_id"], worker_id))
        # Same public event name for every kind, so the audit trail does not reveal it.
        self._event(db, now, "job_created", job_id=fields["job_id"])

    def replace_signature(self, job_id: str, old_signature: str, new_signature: str,
                          now: int, detail: str) -> bool:
        """Swap a job's stored signature after a planned key rotation. Only changes the row
        if it still holds `old_signature` (so two requests can't race). Logged as
        `job_resigned` with the old and new key fingerprints."""
        with self._tx() as db:
            cur = db.execute("UPDATE jobs SET signature=? WHERE job_id=? AND signature=?",
                             (new_signature, job_id, old_signature))
            if cur.rowcount:
                self._event(db, now, "job_resigned", job_id=job_id, detail=detail)
            return bool(cur.rowcount)

    # -- emergency pause (`pause-v1`, app/incident.py) ---------------------------------
    @staticmethod
    def _pause_row(db: sqlite3.Connection) -> sqlite3.Row | None:
        """The `coordinator_paused` event in force, or None if running normally."""
        row = db.execute(
            f"SELECT id, event, detail, at FROM events WHERE event IN "
            f"({','.join('?' * len(PAUSE_EVENTS))}) ORDER BY id DESC LIMIT 1",
            PAUSE_EVENTS).fetchone()
        return row if row is not None and row["event"] == "coordinator_paused" else None

    def _refuse_if_paused(self, db: sqlite3.Connection) -> None:
        row = self._pause_row(db)
        if row is not None:
            raise CoordinatorPaused(int(row["at"]))

    def pause_state(self) -> dict[str, Any] | None:
        """None while running; otherwise when, why and how it was paused (operator-only:
        the reason can describe the incident)."""
        with closing(self._connect()) as db:
            row = self._pause_row(db)
        if row is None:
            return None
        detail = json.loads(row["detail"] or "{}")
        return {"since": int(row["at"]), "reason": detail.get("reason", ""),
                "by": detail.get("by", "unknown")}

    def pause(self, now: int, reason: str, by: str) -> tuple[bool, int]:
        """Pause the coordinator. Returns (newly_paused, since). Pausing twice keeps the
        first time and reason, so a second alarm can't hide when the incident started."""
        reason = " ".join(reason.split())
        if not reason or len(reason) > MAX_PAUSE_REASON or not reason.isprintable():
            raise ValueError(f"reason must be 1-{MAX_PAUSE_REASON} printable characters")
        with self._tx() as db:
            row = self._pause_row(db)
            if row is not None:
                return False, int(row["at"])
            self._event(db, now, "coordinator_paused",
                        detail=json.dumps({"reason": reason, "by": by}, sort_keys=True))
            return True, now

    def resume(self, now: int, note: str, by: str) -> dict[str, Any] | None:
        """Resume. Returns None if it wasn't paused. Audit/tie-breaker/aimed-test assignments
        that were still live when the pause began, and whose pick hasn't taken the job since,
        get a fresh `assign_seconds` window (never a shorter one), so the pause isn't counted
        against those rigs as "not picked up"."""
        note = " ".join(note.split())
        if len(note) > MAX_PAUSE_REASON or not note.isprintable():
            raise ValueError(f"note must be at most {MAX_PAUSE_REASON} printable characters")
        with self._tx() as db:
            row = self._pause_row(db)
            if row is None:
                return None
            paused_at = int(row["at"])
            extended = 0
            for job in db.execute(
                    "SELECT job_id, job_kind, target, assigned_worker FROM jobs "
                    "WHERE status='queued' AND assigned_worker IS NOT NULL "
                    "AND assigned_until IS NOT NULL AND assigned_until>?",
                    (paused_at,)).fetchall():
                kind = "challenge" if job["job_kind"] == "challenge" and job["target"] else "audit"
                if self._leased_since_assignment(db, job["job_id"], job["assigned_worker"],
                                                 _EVENTS[kind][0]):
                    continue  # its pick already had the job; its turn is over
                cur = db.execute("UPDATE jobs SET assigned_until=MAX(assigned_until, ?) "
                                 "WHERE job_id=? AND assigned_until<?",
                                 (now + self.assign_seconds, job["job_id"],
                                  now + self.assign_seconds))
                extended += cur.rowcount
            self._event(db, now, "coordinator_resumed", detail=json.dumps(
                {"note": note, "by": by, "paused_seconds": now - paused_at,
                 "assignments_extended": extended}, sort_keys=True))
            # Hand out anything queued during the pause that still has no pick.
            self._assign_auditors(db, now)
            return {"paused_since": paused_at, "paused_seconds": now - paused_at,
                    "assignments_extended": extended}

    def pause_history(self) -> list[dict[str, Any]]:
        """Every pause and resume, oldest first (operator-only: details can name the incident)."""
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT event, detail, at FROM events WHERE event IN "
                f"({','.join('?' * len(PAUSE_EVENTS))}) ORDER BY id", PAUSE_EVENTS).fetchall()
        return [{"event": r["event"], "at": int(r["at"])} | json.loads(r["detail"] or "{}")
                for r in rows]

    def note(self, now: int, event: str, job_id: str | None = None,
             worker_id: str | None = None, detail: str | None = None) -> None:
        """Append one audit-trail event outside any other change (e.g. a refused receipt)."""
        with self._tx() as db:
            self._event(db, now, event, job_id, worker_id, detail)

    def _audit_not_run(self, db: sqlite3.Connection, job_id: str, now: int) -> None:
        """An audit or tie-breaker job ended without a usable result.
        Audit: the original stays unproven. Tie-breaker: the dispute stays held."""
        row = db.execute("SELECT job_kind, audit_of FROM jobs WHERE job_id=?",
                         (job_id,)).fetchone()
        if row is None or row["audit_of"] is None:
            return
        if row["job_kind"] == "tiebreak":
            self._event(db, now, "tiebreak_not_run", row["audit_of"], detail=job_id)
            return
        if row["job_kind"] != "audit":
            return
        cur = db.execute("UPDATE submissions SET audit_status='unaudited' "
                         "WHERE job_id=? AND audit_status='pending'", (row["audit_of"],))
        if cur.rowcount:
            self._event(db, now, "audit_not_run", row["audit_of"], detail=job_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def _sweep(self, db: sqlite3.Connection, now: int) -> None:
        """Expire overdue jobs and recover leases whose holder went silent."""
        for row in db.execute(
            "SELECT job_id, worker_id FROM jobs WHERE status IN ('queued','leased') "
            "AND expires_at<=?", (now,)
        ).fetchall():
            db.execute("UPDATE jobs SET status='expired', lease_expires_at=NULL, updated_at=? "
                       "WHERE job_id=?", (now, row["job_id"]))
            self._event(db, now, "job_expired", row["job_id"], row["worker_id"])
            self._audit_not_run(db, row["job_id"], now)
        for row in db.execute(
            "SELECT job_id, worker_id, attempts, max_attempts FROM jobs "
            "WHERE status='leased' AND lease_expires_at<=?", (now,)
        ).fetchall():
            final = row["attempts"] >= row["max_attempts"]
            db.execute(
                "UPDATE jobs SET status=?, worker_id=NULL, lease_expires_at=NULL, updated_at=?, "
                "assigned_until=? WHERE job_id=?",
                ("failed" if final else "queued", now, now, row["job_id"]))
            self._event(db, now, "lease_timeout_failed" if final else "lease_timeout_requeued",
                        row["job_id"], row["worker_id"])
            if final:
                self._audit_not_run(db, row["job_id"], now)

    def sweep(self, now: int) -> None:
        with self._tx() as db:
            self._sweep(db, now)

    def _eligible_auditors(self, db: sqlite3.Connection, job_id: str, now: int) -> list[str]:
        """Active, unrevoked workers that are not excluded from `job_id`, directly or by
        sharing an owner with an excluded worker. Sorted, so a seeded rng is repeatable."""
        rows = db.execute(
            "SELECT w.worker_id FROM workers w WHERE w.revoked_at IS NULL "
            "AND w.last_seen_at IS NOT NULL AND w.last_seen_at>? AND NOT EXISTS ("
            "SELECT 1 FROM job_exclusions x LEFT JOIN workers xw ON xw.worker_id=x.worker_id "
            "WHERE x.job_id=? AND (x.worker_id=w.worker_id OR "
            "(xw.owner IS NOT NULL AND xw.owner=w.owner))) ORDER BY w.worker_id",
            (now - self.active_seconds, job_id)).fetchall()
        return [r["worker_id"] for r in rows]

    def _audit_history_rows(self, db: sqlite3.Connection,
                            worker_id: str | None = None) -> dict[str, dict[str, int]]:
        """Per worker: results that passed an independent check, and strikes."""
        passed = ",".join("?" * len(PASSED_STATUSES))
        strikes = ",".join("?" * len(STRIKE_STATUSES))
        sql = (f"SELECT w.worker_id, "
               f"COALESCE(SUM(s.audit_status IN ({passed})), 0) AS checks_passed, "
               f"COALESCE(SUM(s.audit_status IN ({strikes})), 0) AS strikes "
               f"FROM workers w LEFT JOIN submissions s ON s.worker_id=w.worker_id "
               f"{'WHERE w.worker_id=? ' if worker_id else ''}"
               f"GROUP BY w.worker_id ORDER BY w.worker_id")
        args = (*PASSED_STATUSES, *STRIKE_STATUSES, *((worker_id,) if worker_id else ()))
        return {r["worker_id"]: {"checks_passed": int(r["checks_passed"]),
                                 "strikes": int(r["strikes"])}
                for r in db.execute(sql, args).fetchall()}

    def _target_candidates(self, db: sqlite3.Connection, target: tuple[str, str],
                           now: int) -> list[str]:
        """Who an aimed hidden test may go to. A named worker gets it as long as it isn't
        revoked (active or not: the operator chose it). A tier target goes to active,
        unrevoked workers whose tier, worked out now from their record, matches."""
        kind, value = target
        if kind == "worker":
            row = db.execute("SELECT revoked_at FROM workers WHERE worker_id=?",
                             (value,)).fetchone()
            return [value] if row is not None and row["revoked_at"] is None else []
        if self.tier_of is None:
            return []
        active = [r["worker_id"] for r in db.execute(
            "SELECT worker_id FROM workers WHERE revoked_at IS NULL AND last_seen_at IS NOT "
            "NULL AND last_seen_at>? ORDER BY worker_id", (now - self.active_seconds,))]
        history = self._audit_history_rows(db)
        return [w for w in active if self.tier_of(history[w]) == value]

    def _assign_auditors(self, db: sqlite3.Connection, now: int) -> None:
        """Give every queued audit/tie-breaker (and every aimed hidden test) that has no live
        assignment to one eligible worker picked at random. A live assignment is one that
        hasn't run out and whose worker isn't revoked. The previous pick is avoided when
        anyone else is eligible. A test aimed at one named worker stays with it until the
        job's own deadline. Does nothing while the coordinator is paused: a pick couldn't
        claim its job, and would be counted as not picking it up."""
        if self._pause_row(db) is not None:
            return
        kinds = ",".join("?" * len(ASSIGNED_KINDS))
        for row in db.execute(
            f"SELECT j.job_id, j.job_kind, j.target, j.expires_at, j.assigned_worker, "
            f"j.assigned_until, w.revoked_at "
            f"FROM jobs j LEFT JOIN workers w "
            f"ON w.worker_id=j.assigned_worker WHERE j.status='queued' "
            f"AND (j.job_kind IN ({kinds}) OR j.target IS NOT NULL) "
            f"AND (j.assigned_worker IS NULL "
            f"OR j.assigned_until IS NULL OR j.assigned_until<=? OR w.revoked_at IS NOT NULL) "
            f"ORDER BY j.created_at, j.job_id", (*ASSIGNED_KINDS, now)).fetchall():
            target = parse_target(row["target"]) if row["job_kind"] == "challenge" else None
            assigned_ev, unassigned_ev, expired_ev = _EVENTS["challenge" if target else "audit"]
            if target:
                candidates = self._target_candidates(db, target, now)
            else:
                candidates = self._eligible_auditors(db, row["job_id"], now)
            previous = row["assigned_worker"]
            if previous is not None and row["revoked_at"] is None \
                    and not self._leased_since_assignment(db, row["job_id"], previous,
                                                          assigned_ev):
                # The pick's time ran out and it never picked the job up. (A pick that took
                # the job and then let the lease run out, or had its result refused, already
                # has its own event; a revoked pick is the operator's doing, not a drop.)
                # Private: it names who was handed an audit or a test.
                self._event(db, now, expired_ev, row["job_id"], previous,
                            detail="not picked up in time")
            if previous in candidates and len(candidates) > 1:
                candidates.remove(previous)
            pool_note = ""
            if not target and self.auditor_pick == "trusted-first" and candidates:
                # `auditor-pick-v1`: only the most trusted tier that has anyone eligible.
                # Worked out now, from each rig's record at this moment.
                history = self._audit_history_rows(db)
                assert self.tier_of is not None
                candidates, tier = auditor_pool(candidates,
                                                lambda w: self.tier_of(history[w]))
                pool_note = f" pool={tier}"
            pick = self.rng.choice(candidates) if candidates else None
            if pick is None and previous is None:
                continue  # nobody eligible yet; try again on the next request
            until = None
            if pick:
                until = (int(row["expires_at"]) if target and target[0] == "worker"
                         else now + self.assign_seconds)
            db.execute("UPDATE jobs SET assigned_worker=?, assigned_until=? WHERE job_id=?",
                       (pick, until, row["job_id"]))
            # Private event: it names which worker was handed an audit or a test.
            self._event(db, now, assigned_ev if pick else unassigned_ev,
                        row["job_id"], pick,
                        detail=f"candidates={len(candidates)}{pool_note}")

    @staticmethod
    def _leased_since_assignment(db: sqlite3.Connection, job_id: str, worker_id: str,
                                 assigned_event: str = "auditor_assigned") -> bool:
        """Did `worker_id` lease `job_id` after it was last assigned to it? Looked up in the
        audit trail, so a worker that is picked again for the same job is judged on its
        latest assignment only."""
        assigned_id = db.execute(
            "SELECT MAX(id) FROM events WHERE job_id=? AND worker_id=? "
            "AND event=?", (job_id, worker_id, assigned_event)).fetchone()[0]
        if assigned_id is None:
            return False
        return db.execute(
            "SELECT 1 FROM events WHERE job_id=? AND worker_id=? AND event='job_leased' "
            "AND id>? LIMIT 1", (job_id, worker_id, assigned_id)).fetchone() is not None

    def claim_next(self, worker_id: str, lease_seconds: int, now: int) -> dict[str, Any] | None:
        """Atomically lease the oldest queued job `worker_id` may take, or return None.
        Work and challenge jobs are first-come; audits, tie-breakers and aimed hidden tests
        only go to the worker they were assigned to (see _assign_auditors).
        Raises CoordinatorPaused while paused; the worker still counts as seen, so rigs
        that keep asking during a pause stay active for audit picks after it."""
        with self._tx() as db:
            pause = self._pause_row(db)
            db.execute("UPDATE workers SET last_seen_at=? WHERE worker_id=?", (now, worker_id))
            if pause is None:
                return self._claim(db, worker_id, lease_seconds, now)
        raise CoordinatorPaused(int(pause["at"]))

    def _claim(self, db: sqlite3.Connection, worker_id: str, lease_seconds: int,
               now: int) -> dict[str, Any] | None:
        self._sweep(db, now)
        self._assign_auditors(db, now)
        # An excluded worker's owner is excluded too, so one person's rigs never
        # audit or break ties on each other's results.
        owner = db.execute("SELECT owner FROM workers WHERE worker_id=?",
                           (worker_id,)).fetchone()
        row = db.execute(
            "SELECT job_id FROM jobs j WHERE status='queued' AND NOT EXISTS ("
            "SELECT 1 FROM job_exclusions x LEFT JOIN workers w ON w.worker_id=x.worker_id "
            "WHERE x.job_id=j.job_id AND (x.worker_id=? OR "
            "(w.owner IS NOT NULL AND w.owner=?))) "
            f"AND ((j.job_kind NOT IN ({','.join('?' * len(ASSIGNED_KINDS))}) "
            "AND j.target IS NULL) OR j.assigned_worker=?) "
            "ORDER BY created_at, job_id LIMIT 1",
            (worker_id, owner["owner"] if owner else None, *ASSIGNED_KINDS, worker_id)
        ).fetchone()
        if row is None:
            return None
        lease_until = min(now + lease_seconds, self._job_deadline(db, row["job_id"]))
        db.execute(
            "UPDATE jobs SET status='leased', worker_id=?, lease_expires_at=?, "
            "attempts=attempts+1, updated_at=? WHERE job_id=? AND status='queued'",
            (worker_id, lease_until, now, row["job_id"]),
        )
        self._event(db, now, "job_leased", row["job_id"], worker_id)
        job = db.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        return dict(job)

    @staticmethod
    def _job_deadline(db: sqlite3.Connection, job_id: str) -> int:
        return int(db.execute("SELECT expires_at FROM jobs WHERE job_id=?",
                              (job_id,)).fetchone()[0])

    def submit(self, job_id: str, worker_id: str, result_hash: str, correct: bool,
               now: int, verifier_method: str = "deterministic-recompute",
               confidence: str = "exact", result: str | None = None, needs_audit: bool = False,
               audit_job: tuple[dict[str, Any], str] | None = None,
               agree: Agree | None = None,
               tiebreak_job: tuple[dict[str, Any], str] | None = None) -> tuple[str, dict[str, Any] | None]:
        """Record a result. Returns (outcome, job) where outcome is one of:
        accepted, duplicate (same accepted result again), conflict, unknown, not_leased,
        wrong_worker, expired, rejected_requeued, rejected_final.

        Phase 4 (all inside the same transaction as the acceptance):
          - `audit_job` (fields, signature): schedule a hidden re-run of an accepted work
            result that `needs_audit`, excluded from this worker.
          - accepting an audit job compares it with the original using `agree`. If they
            disagree and `tiebreak_job` (fields, signature) is given, a hidden tie-breaker
            is queued for a worker that is neither side of the dispute.
          - accepting a tie-breaker settles the dispute by majority (see _resolve_tiebreak).
          - accepting a challenge job compares it with the operator's expected answer.
        """
        agree = agree or (lambda a, b: a == b)
        with self._tx() as db:
            self._sweep(db, now)
            db.execute("UPDATE workers SET last_seen_at=? WHERE worker_id=?", (now, worker_id))
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                return "unknown", None
            job = dict(job)
            if job["status"] == "accepted":
                prior = db.execute("SELECT worker_id, result_hash FROM submissions WHERE job_id=?",
                                   (job_id,)).fetchone()
                if prior and prior["worker_id"] == worker_id and prior["result_hash"] == result_hash:
                    return "duplicate", job
                self._event(db, now, "submission_conflict", job_id, worker_id)
                return "conflict", job
            if job["status"] == "expired":
                return "expired", job
            if job["status"] != "leased":
                return "not_leased", job
            if job["worker_id"] != worker_id:
                self._event(db, now, "submission_wrong_worker", job_id, worker_id)
                return "wrong_worker", job
            if not correct:
                final = job["attempts"] >= job["max_attempts"]
                db.execute(
                    "UPDATE jobs SET status=?, worker_id=NULL, lease_expires_at=NULL, updated_at=?, "
                    "assigned_until=? WHERE job_id=?",
                    ("rejected" if final else "queued", now, now, job_id))
                self._event(db, now, "result_rejected_final" if final else "result_rejected_requeued",
                            job_id, worker_id)
                if final:
                    self._audit_not_run(db, job_id, now)
                job["status"] = "rejected" if final else "queued"
                return ("rejected_final" if final else "rejected_requeued"), job
            kind = job["job_kind"]
            audit_status = "not-needed"
            if kind == "work" and needs_audit:
                audit_status = "pending" if audit_job else "unaudited"
            elif kind == "challenge":
                passed = result is not None and agree(job["expected_result"] or "", result)
                audit_status = "passed-challenge" if passed else "failed-challenge"
            db.execute(
                "INSERT INTO submissions(job_id, worker_id, result_hash, reward_points, accepted_at, "
                "verifier_method, confidence, result, audit_status) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, worker_id, result_hash, job["reward_points"], now, verifier_method,
                 confidence, result, audit_status))
            db.execute("UPDATE jobs SET status='accepted', lease_expires_at=NULL, updated_at=? "
                       "WHERE job_id=?", (now, job_id))
            self._event(db, now, "result_accepted", job_id, worker_id,
                        f"{verifier_method}/{confidence}")
            if audit_status == "pending" and audit_job:
                fields, signature = audit_job
                self._insert_job(db, fields, signature, job["max_attempts"], now, "audit",
                                 audit_of=job_id, exclude_workers=(worker_id,))
                self._event(db, now, "audit_scheduled", job_id, detail=fields["job_id"])
            elif kind == "challenge":
                self._event(db, now, audit_status.replace("-challenge", "_challenge"),
                            job_id, worker_id)
            elif kind == "audit":
                self._resolve_audit(db, job, result, agree, now, tiebreak_job)
            elif kind == "tiebreak":
                self._resolve_tiebreak(db, job, result, agree, now)
            # Hand any audit or tie-breaker just queued to a random worker right away, so
            # the pick never depends on who happens to ask next.
            self._assign_auditors(db, now)
            job["status"] = "accepted"
            return "accepted", job

    def _resolve_audit(self, db: sqlite3.Connection, audit: dict[str, Any], result: str | None,
                       agree: Agree, now: int,
                       tiebreak_job: tuple[dict[str, Any], str] | None = None) -> None:
        original = db.execute("SELECT * FROM submissions WHERE job_id=?",
                              (audit["audit_of"],)).fetchone()
        if original is None or original["audit_status"] != "pending":
            return
        match = (result is not None and original["result"] is not None
                 and agree(original["result"], result))
        status = "confirmed" if match else "disputed"
        for job_id in (original["job_id"], audit["job_id"]):
            db.execute("UPDATE submissions SET audit_status=?, "
                       "confidence=CASE WHEN ? THEN 'replicated' ELSE confidence END "
                       "WHERE job_id=?", (status, match, job_id))
            self._event(db, now, "audit_match" if match else "audit_mismatch", job_id,
                        detail=audit["job_id"] if job_id == original["job_id"]
                        else original["job_id"])
        if not match and tiebreak_job:
            fields, signature = tiebreak_job
            self._insert_job(db, fields, signature, audit["max_attempts"], now, "tiebreak",
                             audit_of=original["job_id"],
                             exclude_workers=(original["worker_id"], audit["worker_id"]))
            self._event(db, now, "tiebreak_scheduled", original["job_id"],
                        detail=fields["job_id"])

    def _resolve_tiebreak(self, db: sqlite3.Connection, tb: dict[str, Any], result: str | None,
                          agree: Agree, now: int) -> None:
        """Majority of three. The tie-breaker sides with exactly one of the two disputed
        results: that result and the tie-breaker are confirmed, the other is outvoted.
        Siding with neither (or, under a loose tolerance, with both) settles nothing."""
        original = db.execute("SELECT * FROM submissions WHERE job_id=?",
                              (tb["audit_of"],)).fetchone()
        audit = db.execute(
            "SELECT s.* FROM submissions s JOIN jobs j USING(job_id) "
            "WHERE j.audit_of=? AND j.job_kind='audit'", (tb["audit_of"],)).fetchone()
        if original is None or audit is None or original["audit_status"] != "disputed":
            return
        sides = [s for s in (original, audit)
                 if result is not None and s["result"] is not None and agree(s["result"], result)]
        if len(sides) == 1:
            winner = sides[0]
            loser = audit if winner is original else original
            updates = {winner["job_id"]: "confirmed", tb["job_id"]: "confirmed",
                       loser["job_id"]: "outvoted"}
            event = "tiebreak_majority"
        else:
            updates = dict.fromkeys((original["job_id"], audit["job_id"], tb["job_id"]),
                                    "unresolved")
            event = "tiebreak_unresolved"
        for job_id, status in updates.items():
            db.execute("UPDATE submissions SET audit_status=?, "
                       "confidence=CASE WHEN ?='confirmed' THEN 'replicated' ELSE confidence END "
                       "WHERE job_id=?", (status, status, job_id))
            self._event(db, now, event, job_id, detail=f"{status} (tie-breaker {tb['job_id']})")

    # -- reporting --------------------------------------------------------------------
    def entries(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT job_id, worker_id, result_hash, reward_points, accepted_at, "
                "verifier_method, confidence FROM submissions ORDER BY accepted_at, job_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def submission(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM submissions WHERE job_id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def points_by_status(self) -> dict[str, int]:
        """Simulated points per public settlement bucket (see SETTLEMENT)."""
        totals = dict.fromkeys(("confirmed", "pending", "unverified", "held"), 0)
        with closing(self._connect()) as db:
            for row in db.execute("SELECT audit_status, SUM(reward_points) AS n "
                                  "FROM submissions GROUP BY audit_status").fetchall():
                totals[SETTLEMENT.get(row["audit_status"], "held")] += int(row["n"] or 0)
        return totals

    def audits(self) -> list[dict[str, Any]]:
        """Operator view: every audit and challenge job with its outcome."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT j.job_id, j.job_kind, j.audit_of, j.task_type, j.status, j.created_at, "
                "j.assigned_worker, j.target, "
                "s.worker_id, s.audit_status FROM jobs j LEFT JOIN submissions s USING(job_id) "
                "WHERE j.job_kind IN ('audit','challenge','tiebreak') ORDER BY j.created_at, j.job_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def review_groups(self) -> list[dict[str, Any]]:
        """Raw rows for the operator review queue (app/review.py, threat model #7). Read-only.

        One group per original job that has points held: a work job with its audit and
        tie-breaker (whatever state they're in), or a failed hidden test job on its own.
        Contains answers and expected answers, so it must stay operator-only."""
        held = tuple(s for s, bucket in SETTLEMENT.items() if bucket == "held")
        marks = ",".join("?" * len(held))
        with closing(self._connect()) as db:
            roots = [r[0] for r in db.execute(
                f"SELECT DISTINCT CASE WHEN j.job_kind IN ('audit','tiebreak') THEN j.audit_of "
                f"ELSE j.job_id END FROM submissions s JOIN jobs j USING(job_id) "
                f"WHERE s.audit_status IN ({marks}) ORDER BY 1", held).fetchall()]
            groups = []
            for root in roots:
                jobs = db.execute(
                    "SELECT j.job_id, j.job_kind, j.task_type, j.payload, j.status, "
                    "j.expected_result, j.created_at, s.worker_id, s.result, s.audit_status, "
                    "s.reward_points, s.accepted_at, w.owner FROM jobs j "
                    "LEFT JOIN submissions s USING(job_id) "
                    "LEFT JOIN workers w ON w.worker_id=s.worker_id "
                    "WHERE j.job_id=? OR j.audit_of=? ORDER BY j.created_at, j.job_id",
                    (root, root)).fetchall()
                groups.append({"root": root, "jobs": [dict(r) for r in jobs]})
        return groups

    def worker_trust(self, worker_id: str) -> dict[str, Any]:
        """Operator view: how a worker's results held up under audits and challenges."""
        with closing(self._connect()) as db:
            counts = {r["audit_status"]: int(r["n"]) for r in db.execute(
                "SELECT audit_status, COUNT(*) AS n FROM submissions WHERE worker_id=? "
                "GROUP BY audit_status", (worker_id,)).fetchall()}
            rejected = db.execute(
                "SELECT COUNT(*) FROM events WHERE worker_id=? AND event LIKE 'result_rejected%'",
                (worker_id,)).fetchone()[0]
            worker = db.execute("SELECT owner, revoked_at FROM workers WHERE worker_id=?",
                                (worker_id,)).fetchone()
        return {
            "worker_id": worker_id,
            "owner": worker["owner"] if worker else None,
            "revoked": bool(worker and worker["revoked_at"] is not None),
            "accepted": sum(counts.values()),
            "rejected": int(rejected),
            "audits_confirmed": counts.get("confirmed", 0),
            "audits_disputed": counts.get("disputed", 0),
            "audits_pending": counts.get("pending", 0),
            "audits_outvoted": counts.get("outvoted", 0),
            "audits_unresolved": counts.get("unresolved", 0),
            "challenges_passed": counts.get("passed-challenge", 0),
            "challenges_failed": counts.get("failed-challenge", 0),
            "audit_follow_through": self.audit_follow_through(worker_id),
            "note": "Agreement between workers is evidence, not proof: colluding workers "
                    "can agree on a wrong answer.",
        }

    def audit_history(self, worker_id: str | None = None) -> dict[str, Any]:
        """Threat model #1: per worker, how many of its accepted results passed an
        independent check and how many are strikes (see app/audit_policy.py). With a
        `worker_id`, that worker's counts; without, a dict of every worker's counts."""
        with closing(self._connect()) as db:
            rows = self._audit_history_rows(db, worker_id)
        if worker_id:
            return rows.get(worker_id, {"checks_passed": 0, "strikes": 0})
        return rows

    def audit_follow_through(self, worker_id: str | None = None) -> dict[str, Any]:
        """Threat model #4 follow-up: per worker, what happened to the audits and
        tie-breakers it was randomly handed. Lifetime counts, from the audit trail:

            assigned      times it was picked for one (a re-pick of the same job counts again)
            completed     audit/tie-breaker results it got accepted
            not_claimed   picks it never took up before `assign_seconds` ran out
            lease_expired ones it took, then went silent until its lease ran out
            refused       ones whose result the coordinator rejected (e.g. empty output)
            dropped       not_claimed + lease_expired + refused

        A rig that went offline and a ring member quietly skipping checks it doesn't like
        look the same here: this is a reason to look, not proof. Revocation by the operator
        is not counted. With a `worker_id`, that worker's counts; without, every worker's."""
        kinds = ",".join("?" * len(ASSIGNED_KINDS))
        sql = (
            "SELECT w.worker_id, "
            "(SELECT COUNT(*) FROM events e WHERE e.worker_id=w.worker_id "
            " AND e.event='auditor_assigned') AS assigned, "
            f"(SELECT COUNT(*) FROM submissions s JOIN jobs j USING(job_id) "
            f" WHERE s.worker_id=w.worker_id AND j.job_kind IN ({kinds})) AS completed, "
            "(SELECT COUNT(*) FROM events e WHERE e.worker_id=w.worker_id "
            " AND e.event='audit_assignment_expired') AS not_claimed, "
            f"(SELECT COUNT(*) FROM events e JOIN jobs j USING(job_id) "
            f" WHERE e.worker_id=w.worker_id AND j.job_kind IN ({kinds}) "
            f" AND e.event IN ('lease_timeout_requeued','lease_timeout_failed')) "
            f" AS lease_expired, "
            f"(SELECT COUNT(*) FROM events e JOIN jobs j USING(job_id) "
            f" WHERE e.worker_id=w.worker_id AND j.job_kind IN ({kinds}) "
            f" AND e.event IN ('result_rejected_requeued','result_rejected_final')) AS refused "
            f"FROM workers w {'WHERE w.worker_id=? ' if worker_id else ''}"
            "ORDER BY w.worker_id")
        args = (*ASSIGNED_KINDS, *ASSIGNED_KINDS, *ASSIGNED_KINDS,
                *((worker_id,) if worker_id else ()))
        with closing(self._connect()) as db:
            rows = {}
            for r in db.execute(sql, args).fetchall():
                counts = {k: int(r[k]) for k in ("assigned", "completed", "not_claimed",
                                                  "lease_expired", "refused")}
                counts["dropped"] = (counts["not_claimed"] + counts["lease_expired"]
                                     + counts["refused"])
                rows[r["worker_id"]] = counts
        if worker_id:
            return rows.get(worker_id, dict.fromkeys(
                ("assigned", "completed", "not_claimed", "lease_expired", "refused",
                 "dropped"), 0))
        return rows

    def reward_rows(self, since: int, until: int) -> list[dict[str, Any]]:
        """Phase 5: accepted results with `since <= accepted_at < until`, in a fixed order.
        Only the fields a reward report needs: never results, payloads or job kinds."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT s.job_id, s.worker_id, j.task_type, s.reward_points, s.accepted_at, "
                "s.verifier_method, s.confidence, s.audit_status FROM submissions s "
                "JOIN jobs j USING(job_id) WHERE s.accepted_at>=? AND s.accepted_at<? "
                "ORDER BY s.accepted_at, s.job_id", (since, until)).fetchall()
            return [dict(r) for r in rows]

    def activity_counts(self, since: int, until: int) -> dict[str, dict[str, int]]:
        """Phase 5: per-worker counts of public lifecycle events with `since <= at < until`."""
        names = {"job_leased": "leases", "result_rejected_requeued": "rejected",
                 "result_rejected_final": "rejected", "lease_timeout_requeued": "lease_timeouts",
                 "lease_timeout_failed": "lease_timeouts",
                 "submission_wrong_worker": "bad_submissions",
                 "submission_conflict": "bad_submissions",
                 "receipt_mismatch": "bad_submissions"}
        out: dict[str, dict[str, int]] = {}
        with closing(self._connect()) as db:
            marks = ",".join("?" * len(names))
            for r in db.execute(
                f"SELECT worker_id, event, COUNT(*) AS n FROM events WHERE worker_id IS NOT NULL "
                f"AND event IN ({marks}) AND at>=? AND at<? GROUP BY worker_id, event",
                (*names, since, until)).fetchall():
                counts = out.setdefault(r["worker_id"], {})
                key = names[r["event"]]
                counts[key] = counts.get(key, 0) + int(r["n"])
        return out

    def total_points(self) -> int:
        with closing(self._connect()) as db:
            row = db.execute("SELECT COALESCE(SUM(reward_points),0) AS total FROM submissions"
                             ).fetchone()
            return int(row["total"])

    def status_counts(self) -> dict[str, int]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
            return {row["status"]: int(row["n"]) for row in rows}

    def ping(self) -> None:
        """Cheapest possible proof the database file can be opened and read."""
        with closing(self._connect()) as db:
            db.execute("SELECT 1 FROM jobs LIMIT 1").fetchall()

    def health_snapshot(self, now: int, window_seconds: int = 3600) -> dict[str, Any]:
        """Operator view of how the coordinator is doing right now. Read-only: it does not
        sweep, so leases past due show up as `overdue_leases` until the next request."""
        with closing(self._connect()) as db:
            one = lambda sql, *args: db.execute(sql, args).fetchone()[0]  # noqa: E731
            kinds = ",".join("?" * len(ASSIGNED_KINDS))
            oldest = one("SELECT MIN(created_at) FROM jobs WHERE status='queued'")
            snapshot = {
                "workers": {
                    "registered": one("SELECT COUNT(*) FROM workers"),
                    "active": one("SELECT COUNT(*) FROM workers WHERE revoked_at IS NULL AND "
                                  "last_seen_at IS NOT NULL AND last_seen_at>?",
                                  now - self.active_seconds),
                    "revoked": one("SELECT COUNT(*) FROM workers WHERE revoked_at IS NOT NULL"),
                },
                "queue": {
                    "queued": one("SELECT COUNT(*) FROM jobs WHERE status='queued'"),
                    "leased": one("SELECT COUNT(*) FROM jobs WHERE status='leased'"),
                    "oldest_queued_age_seconds": None if oldest is None else now - int(oldest),
                    "overdue_leases": one("SELECT COUNT(*) FROM jobs WHERE status='leased' "
                                          "AND lease_expires_at<=?", now),
                    "past_deadline": one("SELECT COUNT(*) FROM jobs WHERE status IN "
                                         "('queued','leased') AND expires_at<=?", now),
                },
                "audits": {
                    "open": one(f"SELECT COUNT(*) FROM jobs WHERE status IN ('queued','leased') "
                                f"AND job_kind IN ({kinds})", *ASSIGNED_KINDS),
                    # Queued with no live pick: nobody eligible, or the pick ran out of time.
                    "waiting_for_auditor": one(
                        f"SELECT COUNT(*) FROM jobs j LEFT JOIN workers w "
                        f"ON w.worker_id=j.assigned_worker WHERE j.status='queued' "
                        f"AND j.job_kind IN ({kinds}) AND (j.assigned_worker IS NULL "
                        f"OR j.assigned_until IS NULL OR j.assigned_until<=? "
                        f"OR w.revoked_at IS NOT NULL)", *ASSIGNED_KINDS, now),
                },
                # Hidden tests aimed at one rig or a trust tier (operator-only numbers).
                "aimed_tests": {
                    "open": one("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','leased') "
                                "AND job_kind='challenge' AND target IS NOT NULL"),
                    "waiting_for_rig": one(
                        "SELECT COUNT(*) FROM jobs j LEFT JOIN workers w "
                        "ON w.worker_id=j.assigned_worker WHERE j.status='queued' "
                        "AND j.job_kind='challenge' AND j.target IS NOT NULL "
                        "AND (j.assigned_worker IS NULL OR j.assigned_until IS NULL "
                        "OR j.assigned_until<=? OR w.revoked_at IS NOT NULL)", now),
                },
                "recent_events": {r["event"]: int(r["n"]) for r in db.execute(
                    "SELECT event, COUNT(*) AS n FROM events WHERE at>? AND at<=? "
                    "GROUP BY event ORDER BY event", (now - window_seconds, now)).fetchall()},
                "recent_window_seconds": window_seconds,
                "last_event_at": one("SELECT MAX(at) FROM events"),
            }
        return snapshot

    def events(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            if job_id:
                rows = db.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,))
            else:
                rows = db.execute("SELECT * FROM events ORDER BY id")
            return [dict(r) for r in rows.fetchall()]

    def copy_until(self) -> int | None:
        """End of the period a redacted ledger copy covers (app/ledger_copy.py), or None
        for a full ledger. Reports past this point would be built from missing data."""
        with closing(self._connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                              "AND name='copy_info'").fetchone():
                return None
            row = db.execute("SELECT until FROM copy_info").fetchone()
            return int(row["until"]) if row else None

    def check_consistency(self) -> list[str]:
        """Invariants an auditor can run at any time. Returns a list of problems."""
        problems: list[str] = []
        with closing(self._connect()) as db:
            q = lambda sql: [dict(r) for r in db.execute(sql).fetchall()]  # noqa: E731
            for r in q("SELECT s.job_id FROM submissions s JOIN jobs j USING(job_id) "
                       "WHERE j.status!='accepted'"):
                problems.append(f"submission for non-accepted job {r['job_id']}")
            for r in q("SELECT j.job_id FROM jobs j LEFT JOIN submissions s USING(job_id) "
                       "WHERE j.status='accepted' AND s.job_id IS NULL"):
                problems.append(f"accepted job without submission {r['job_id']}")
            for r in q("SELECT s.job_id FROM submissions s JOIN jobs j USING(job_id) "
                       "WHERE s.reward_points!=j.reward_points"):
                problems.append(f"reward mismatch {r['job_id']}")
            for r in q("SELECT job_id FROM jobs WHERE status='leased' AND "
                       "(worker_id IS NULL OR lease_expires_at IS NULL)"):
                problems.append(f"lease without holder {r['job_id']}")
            for r in q("SELECT job_id FROM jobs WHERE attempts>max_attempts"):
                problems.append(f"attempts over limit {r['job_id']}")
            for r in q("SELECT s.job_id FROM submissions s WHERE s.audit_status='pending' AND "
                       "NOT EXISTS (SELECT 1 FROM jobs a WHERE a.audit_of=s.job_id AND "
                       "a.job_kind='audit' AND a.status IN ('queued','leased'))"):
                problems.append(f"pending audit without open audit job {r['job_id']}")
            for r in q("SELECT j.job_id FROM jobs j JOIN job_exclusions x USING(job_id) "
                       "WHERE j.worker_id=x.worker_id"):
                problems.append(f"job leased to an excluded worker {r['job_id']}")
            for r in q("SELECT DISTINCT j.job_id FROM jobs j JOIN job_exclusions x USING(job_id) "
                       "JOIN workers ex ON ex.worker_id=x.worker_id "
                       "JOIN workers w ON w.worker_id=j.worker_id "
                       "WHERE ex.owner IS NOT NULL AND ex.owner=w.owner AND w.worker_id!=ex.worker_id"):
                problems.append(f"job given to a worker with the same owner as an excluded "
                                f"worker {r['job_id']}")
            for r in q("SELECT s.job_id FROM submissions s JOIN jobs j USING(job_id) "
                       "JOIN jobs o ON o.job_id=j.audit_of JOIN submissions os ON os.job_id=o.job_id "
                       "WHERE j.job_kind='audit' AND s.worker_id=os.worker_id"):
                problems.append(f"worker audited its own result {r['job_id']}")
            for r in q("SELECT t.job_id FROM jobs t JOIN submissions ts USING(job_id) "
                       "JOIN submissions os ON os.job_id=t.audit_of "
                       "JOIN jobs a ON a.audit_of=t.audit_of AND a.job_kind='audit' "
                       "JOIN submissions s_a ON s_a.job_id=a.job_id "
                       "WHERE t.job_kind='tiebreak' AND ts.worker_id IN (os.worker_id, s_a.worker_id)"):
                problems.append(f"worker broke a tie on its own dispute {r['job_id']}")
            for r in q("SELECT s.job_id FROM submissions s JOIN jobs j USING(job_id) "
                       "WHERE s.audit_status IN ('outvoted','unresolved') AND NOT EXISTS ("
                       "SELECT 1 FROM jobs t JOIN submissions ts ON ts.job_id=t.job_id "
                       "WHERE t.job_kind='tiebreak' AND t.audit_of="
                       "CASE WHEN j.job_kind='work' THEN j.job_id ELSE j.audit_of END)"):
                problems.append(f"tie-breaker outcome without a tie-breaker {r['job_id']}")
            for r in q("SELECT j.job_id FROM jobs j WHERE j.job_kind IN ('audit','tiebreak') "
                       "AND j.worker_id IS NOT NULL AND j.status IN ('leased','accepted') "
                       "AND j.assigned_worker IS NOT NULL AND j.worker_id!=j.assigned_worker"):
                problems.append(f"audit taken by a worker it wasn't assigned to {r['job_id']}")
            for r in q("SELECT j.job_id FROM jobs j WHERE j.job_kind='challenge' "
                       "AND j.worker_id IS NOT NULL AND j.status IN ('leased','accepted') "
                       "AND j.assigned_worker IS NOT NULL AND j.worker_id!=j.assigned_worker"):
                problems.append(f"aimed hidden test taken by a worker it wasn't assigned to "
                                f"{r['job_id']}")
            # Ledger copies made before 0.5.21 are opened read-only and have no `target`.
            has_target = any(c["name"] == "target"
                             for c in db.execute("PRAGMA table_info(jobs)").fetchall())
            for r in q("SELECT job_id FROM jobs WHERE target LIKE 'worker:%' AND worker_id "
                       "IS NOT NULL AND worker_id!=substr(target, 8)") if has_target else ():
                problems.append(f"hidden test aimed at one worker taken by another "
                                f"{r['job_id']}")
            for r in q("SELECT j.job_id FROM jobs j JOIN jobs o ON o.job_id=j.audit_of "
                       "WHERE j.job_kind IN ('audit','tiebreak') AND "
                       "(j.schema_version!=o.schema_version OR j.payload_hash!=o.payload_hash "
                       "OR COALESCE(j.model_sha256,'')!=COALESCE(o.model_sha256,''))"):
                problems.append(f"re-run doesn't match the original job's version, input or "
                                f"model {r['job_id']}")
            for r in q("SELECT job_id FROM jobs WHERE schema_version!='bnh-job/0' AND "
                       "task_type='llm:generate' AND model_sha256 IS NULL"):
                problems.append(f"model job without a pinned model hash {r['job_id']}")
        return problems
