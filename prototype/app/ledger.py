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
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

FINAL_STATES = ("accepted", "rejected", "expired", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL,
    registered_at INTEGER NOT NULL
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
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS submissions (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    worker_id TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    reward_points INTEGER NOT NULL,
    accepted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    worker_id TEXT,
    event TEXT NOT NULL,
    detail TEXT,
    at INTEGER NOT NULL
);
"""


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    # -- plumbing -----------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
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

    # -- workers --------------------------------------------------------------------
    def register_worker(self, worker_id: str, capabilities: str, now: int) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO workers(worker_id, capabilities, registered_at) VALUES(?,?,?) "
                "ON CONFLICT(worker_id) DO UPDATE SET capabilities=excluded.capabilities",
                (worker_id, capabilities, now),
            )
            self._event(db, now, "worker_registered", worker_id=worker_id)

    def has_worker(self, worker_id: str) -> bool:
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM workers WHERE worker_id=?",
                              (worker_id,)).fetchone() is not None

    # -- jobs -----------------------------------------------------------------------
    def create_job(self, fields: dict[str, Any], signature: str, max_attempts: int,
                   now: int) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO jobs(job_id, task_type, payload, payload_hash, reward_points, "
                "expires_at, signature, status, max_attempts, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,'queued',?,?,?)",
                (fields["job_id"], fields["task_type"], fields["payload"], fields["payload_hash"],
                 fields["reward_points"], fields["expires_at"], signature, max_attempts, now, now),
            )
            self._event(db, now, "job_created", job_id=fields["job_id"])

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
        for row in db.execute(
            "SELECT job_id, worker_id, attempts, max_attempts FROM jobs "
            "WHERE status='leased' AND lease_expires_at<=?", (now,)
        ).fetchall():
            final = row["attempts"] >= row["max_attempts"]
            db.execute(
                "UPDATE jobs SET status=?, worker_id=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE job_id=?", ("failed" if final else "queued", now, row["job_id"]))
            self._event(db, now, "lease_timeout_failed" if final else "lease_timeout_requeued",
                        row["job_id"], row["worker_id"])

    def sweep(self, now: int) -> None:
        with self._tx() as db:
            self._sweep(db, now)

    def claim_next(self, worker_id: str, lease_seconds: int, now: int) -> dict[str, Any] | None:
        """Atomically lease the oldest queued job to `worker_id`, or return None."""
        with self._tx() as db:
            self._sweep(db, now)
            row = db.execute(
                "SELECT job_id FROM jobs WHERE status='queued' ORDER BY created_at, job_id LIMIT 1"
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
               now: int) -> tuple[str, dict[str, Any] | None]:
        """Record a result. Returns (outcome, job) where outcome is one of:
        accepted, duplicate (same accepted result again), conflict, unknown, not_leased,
        wrong_worker, expired, rejected_requeued, rejected_final."""
        with self._tx() as db:
            self._sweep(db, now)
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
                    "UPDATE jobs SET status=?, worker_id=NULL, lease_expires_at=NULL, updated_at=? "
                    "WHERE job_id=?", ("rejected" if final else "queued", now, job_id))
                self._event(db, now, "result_rejected_final" if final else "result_rejected_requeued",
                            job_id, worker_id)
                job["status"] = "rejected" if final else "queued"
                return ("rejected_final" if final else "rejected_requeued"), job
            db.execute(
                "INSERT INTO submissions(job_id, worker_id, result_hash, reward_points, accepted_at) "
                "VALUES(?,?,?,?,?)", (job_id, worker_id, result_hash, job["reward_points"], now))
            db.execute("UPDATE jobs SET status='accepted', lease_expires_at=NULL, updated_at=? "
                       "WHERE job_id=?", (now, job_id))
            self._event(db, now, "result_accepted", job_id, worker_id)
            job["status"] = "accepted"
            return "accepted", job

    # -- reporting --------------------------------------------------------------------
    def entries(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT job_id, worker_id, result_hash, reward_points, accepted_at "
                "FROM submissions ORDER BY accepted_at, job_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def total_points(self) -> int:
        with closing(self._connect()) as db:
            row = db.execute("SELECT COALESCE(SUM(reward_points),0) AS total FROM submissions"
                             ).fetchone()
            return int(row["total"])

    def status_counts(self) -> dict[str, int]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
            return {row["status"]: int(row["n"]) for row in rows}

    def events(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            if job_id:
                rows = db.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,))
            else:
                rows = db.execute("SELECT * FROM events ORDER BY id")
            return [dict(r) for r in rows.fetchall()]

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
        return problems
