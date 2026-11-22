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
Workers see the same envelope for all four. Each accepted submission carries an
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
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

FINAL_STATES = ("accepted", "rejected", "expired", "failed")

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
    updated_at INTEGER NOT NULL,
    job_kind TEXT NOT NULL DEFAULT 'work',
    audit_of TEXT,
    expected_result TEXT
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
"""


MIGRATIONS = {
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
    ),
}


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
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
                   now: int, job_kind: str = "work", expected_result: str | None = None) -> None:
        with self._tx() as db:
            self._insert_job(db, fields, signature, max_attempts, now, job_kind,
                             expected_result=expected_result)

    def _insert_job(self, db: sqlite3.Connection, fields: dict[str, Any], signature: str,
                    max_attempts: int, now: int, job_kind: str, audit_of: str | None = None,
                    expected_result: str | None = None,
                    exclude_workers: tuple[str, ...] = ()) -> None:
        db.execute(
            "INSERT INTO jobs(job_id, task_type, payload, payload_hash, reward_points, "
            "expires_at, signature, status, max_attempts, created_at, updated_at, job_kind, "
            "audit_of, expected_result) VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?,?,?)",
            (fields["job_id"], fields["task_type"], fields["payload"], fields["payload_hash"],
             fields["reward_points"], fields["expires_at"], signature, max_attempts, now, now,
             job_kind, audit_of, expected_result),
        )
        for worker_id in exclude_workers:
            db.execute("INSERT INTO job_exclusions(job_id, worker_id) VALUES(?,?)",
                       (fields["job_id"], worker_id))
        # Same public event name for every kind, so the audit trail does not reveal it.
        self._event(db, now, "job_created", job_id=fields["job_id"])

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
                "UPDATE jobs SET status=?, worker_id=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE job_id=?", ("failed" if final else "queued", now, row["job_id"]))
            self._event(db, now, "lease_timeout_failed" if final else "lease_timeout_requeued",
                        row["job_id"], row["worker_id"])
            if final:
                self._audit_not_run(db, row["job_id"], now)

    def sweep(self, now: int) -> None:
        with self._tx() as db:
            self._sweep(db, now)

    def claim_next(self, worker_id: str, lease_seconds: int, now: int) -> dict[str, Any] | None:
        """Atomically lease the oldest queued job to `worker_id`, or return None."""
        with self._tx() as db:
            self._sweep(db, now)
            row = db.execute(
                "SELECT job_id FROM jobs j WHERE status='queued' AND NOT EXISTS ("
                "SELECT 1 FROM job_exclusions x WHERE x.job_id=j.job_id AND x.worker_id=?) "
                "ORDER BY created_at, job_id LIMIT 1", (worker_id,)
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
                "s.worker_id, s.audit_status FROM jobs j LEFT JOIN submissions s USING(job_id) "
                "WHERE j.job_kind IN ('audit','challenge','tiebreak') ORDER BY j.created_at, j.job_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def worker_trust(self, worker_id: str) -> dict[str, Any]:
        """Operator view: how a worker's results held up under audits and challenges."""
        with closing(self._connect()) as db:
            counts = {r["audit_status"]: int(r["n"]) for r in db.execute(
                "SELECT audit_status, COUNT(*) AS n FROM submissions WHERE worker_id=? "
                "GROUP BY audit_status", (worker_id,)).fetchall()}
            rejected = db.execute(
                "SELECT COUNT(*) FROM events WHERE worker_id=? AND event LIKE 'result_rejected%'",
                (worker_id,)).fetchone()[0]
        return {
            "worker_id": worker_id,
            "accepted": sum(counts.values()),
            "rejected": int(rejected),
            "audits_confirmed": counts.get("confirmed", 0),
            "audits_disputed": counts.get("disputed", 0),
            "audits_pending": counts.get("pending", 0),
            "audits_outvoted": counts.get("outvoted", 0),
            "audits_unresolved": counts.get("unresolved", 0),
            "challenges_passed": counts.get("passed-challenge", 0),
            "challenges_failed": counts.get("failed-challenge", 0),
            "note": "Agreement between workers is evidence, not proof: colluding workers "
                    "can agree on a wrong answer.",
        }

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
                 "submission_conflict": "bad_submissions"}
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
            for r in q("SELECT s.job_id FROM submissions s WHERE s.audit_status='pending' AND "
                       "NOT EXISTS (SELECT 1 FROM jobs a WHERE a.audit_of=s.job_id AND "
                       "a.job_kind='audit' AND a.status IN ('queued','leased'))"):
                problems.append(f"pending audit without open audit job {r['job_id']}")
            for r in q("SELECT j.job_id FROM jobs j JOIN job_exclusions x USING(job_id) "
                       "WHERE j.worker_id=x.worker_id"):
                problems.append(f"job leased to an excluded worker {r['job_id']}")
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
        return problems
