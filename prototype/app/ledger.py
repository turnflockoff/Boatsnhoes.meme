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
                      (with `tiebreak_decides="established-only"`, a majority whose rigs
                      aren't both `established` settles nothing: all three stay `disputed`
                      for a person, see audit_policy.tiebreak_settles)
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
import threading
import time
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from . import copy_guard, ledger_format, run_lock, secret_file, tasks
from .audit_policy import (
    AUDITOR_PICK_RULES,
    PASSED_STATUSES,
    STRIKE_STATUSES,
    TIEBREAK_DECIDES_RULES,
    auditor_pool,
    tiebreak_settles,
)

FINAL_STATES = ("accepted", "rejected", "expired", "failed")

# Emergency pause (`pause-v1`): the latest of these two events is the coordinator's state.
PAUSE_VERSION = "pause-v1"
PAUSE_EVENTS = ("coordinator_paused", "coordinator_resumed")
MAX_PAUSE_REASON = 200


# Retired hidden tests (`test-reuse-v1`, threat model #11/#14). A ledger copy (app/
# ledger_copy.py) shows which jobs before its `until` were hidden tests, and every rig
# remembers the inputs of the jobs it ran. So once a copy is made, the input of every
# hidden test from that period is "burned": using it again as a hidden test would let the
# rigs that ran it recognise the test. Each export is recorded as a `ledger_copy_made`
# event, and a new hidden test with the exact input of one created before the latest
# recorded `until` is refused.
# `test-reuse-v2` (0.5.39): also refused when it is a *near copy* of one: same task type and
# the same recognisable text once case, spacing, punctuation and invisible characters are
# ignored, and for `llm:generate` whatever the seed, max_tokens, timeout or model (see
# `tasks.input_fingerprint`). A rig that remembers "Name a ship." as a test recognises
# "name a ship" with seed 8 just as well. Rewording is still not caught.
TEST_REUSE_RULE = "test-reuse-v2"
COPY_MADE_EVENT = "ledger_copy_made"
# `ledger-clock-v1` (app/ledger_clock.py): a reward report was signed; its time is at or
# after the report's `until`, so nothing is written into a signed period afterwards.
REPORT_SIGNED_EVENT = "reward_report_signed"


class HiddenTestRevealed(ValueError):
    """Refused: this hidden test's exact input was a hidden test in a period that a ledger
    copy already covers, so the rigs that ran it can tell it is a test."""

    def __init__(self, revealed_until: int, match: str = "exact"):
        super().__init__("hidden test input already revealed by a ledger copy")
        self.revealed_until = revealed_until
        self.match = match  # "exact" or "near-copy" (test-reuse-v2)


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


# `ledger-pages-v1` (code audit, twelfth pass): indexes so the routes anyone can call stay
# cheap as the ledger grows. Without them a public `GET /v1/jobs/{id}` read every event ever
# written, and the public ledger had to sort every result to show any of them. Created
# after the migrations, so a ledger from before a column existed gets the column first.
SCALE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS submissions_by_time ON submissions(accepted_at, job_id)",
    "CREATE INDEX IF NOT EXISTS events_by_job ON events(job_id, id)",
    "CREATE INDEX IF NOT EXISTS jobs_by_audit_of ON jobs(audit_of)",
)

# `ledger-pages-v1`: the most results one ledger page can hold.
MAX_PAGE = 1000

# `refusal-log-v1` (code audit, thirteenth pass): what one enrolled rig can make the
# coordinator write. A refused submission (a result for a job someone else already
# finished, a job leased to another rig, or a receipt that doesn't match the job) used to
# add a row to the audit trail *every* time. Measured on a real server: 180 bytes per
# request, so one rig inside the 240-a-minute request limit could add 345,600 rows (about
# 62 MB) a day, forever, and pile them onto another rig's job: a day of that made the
# public `GET /v1/jobs/{id}` for that job 54 MB and 1.5 s. Now the same refusal (same job,
# same rig, same reason) is written once; repeats are only counted in memory for the
# operator health page.
REFUSAL_EVENTS = ("submission_conflict", "submission_wrong_worker", "receipt_mismatch")

# `refusal-log-v1`: the most events `GET /v1/jobs/{id}` shows (oldest first). With repeats
# no longer written, a job has a handful; the cap is a second fence, not the main one.
MAX_JOB_EVENTS = 200

# `public-events-v1` (code audit, nineteenth pass): the only event fields the public job
# page shows. Not `id`: it counts up across the whole ledger, which tied a re-check to the
# job it re-checks for anyone looking (see public_job_events).
PUBLIC_EVENT_COLUMNS = ("job_id", "worker_id", "event", "detail", "at")
PUBLIC_EVENTS_RULE = "public-events-v1"

# `refusal-log-v1`: every accepted result asks for the sending rig's audit record (to pick
# its audit rate), and that read every result ever accepted (about 10 ms at 100,000, and
# growing). This index lets it read only that rig's results.
WORKER_INDEXES = (
    "CREATE INDEX IF NOT EXISTS submissions_by_worker ON submissions(worker_id, audit_status)",
)

# `ledger-clock-v1`: the newest time written anywhere in the ledger.
LATEST_TIME_SQL = (
    "SELECT MAX(m) FROM (SELECT MAX(at) AS m FROM events "
    "UNION ALL SELECT MAX(updated_at) FROM jobs "
    "UNION ALL SELECT MAX(created_at) FROM jobs "
    "UNION ALL SELECT MAX(accepted_at) FROM submissions "
    "UNION ALL SELECT MAX(registered_at) FROM workers "
    "UNION ALL SELECT MAX(last_seen_at) FROM workers "
    "UNION ALL SELECT MAX(revoked_at) FROM workers "
    "UNION ALL SELECT MAX(created_at) FROM enrollments "
    "UNION ALL SELECT MAX(used_at) FROM enrollments)")

# Job kinds that go to a randomly chosen worker instead of whoever asks first.
ASSIGNED_KINDS = ("audit", "tiebreak")

# `follow-through-v2` (threat model #4 follow-up): an audit/tie-breaker pick whose window ran
# out (event `e`) while the pick was taking other jobs. Busy means it leased some other job
# after it was handed this one (its latest `auditor_assigned` for the job before `e`) and
# before the window was closed. Every lease runs the expiry check first, so a lease written
# after the window ran out always comes after `e` and can't make a silent rig look busy.
_BUSY_IN_WINDOW = (
    "EXISTS (SELECT 1 FROM events l WHERE l.worker_id=e.worker_id "
    "AND l.event='job_leased' AND l.id<e.id AND l.id>(SELECT MAX(a.id) FROM events a "
    "WHERE a.job_id=e.job_id AND a.worker_id=e.worker_id AND a.event='auditor_assigned' "
    "AND a.id<e.id))")

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
                 auditor_pick: str = "uniform", tiebreak_decides: str = "always",
                 untrusted: bool = False):
        """`rng` picks auditors; the default uses the OS's secure randomness so workers
        can't predict who gets an audit. A worker counts as active for `active_seconds`
        after it last asked for a job, sent a result, or enrolled. An assigned worker has
        `assign_seconds` to claim its audit before someone else gets it.
        `read_only` opens an existing file without changing a single byte (no WAL switch,
        no migrations), for checking a shared ledger copy whose sha256 was published.
        `untrusted` (implies `read_only`) is for a copy someone else made: the file is
        vetted once (app/copy_guard.py, `copy-open-v1`: plain tables only, intact, size
        limits) and every connection is immutable and hardened. Raises
        copy_guard.UnsafeCopyError if the file isn't a plain ledger copy.
        `tier_of` maps a worker's audit_history() counts to its trust tier (see
        app/audit_policy.py); without it, hidden tests aimed at a tier find nobody.
        `auditor_pick` is "uniform" (any eligible worker) or "trusted-first" (audits and
        tie-breakers go to the most trusted tier that has an eligible worker; needs
        `tier_of`, see audit_policy.auditor_pool).
        `tiebreak_decides` is "always" (a tie-breaker's majority settles a dispute) or
        "established-only" (only when both rigs on the winning side are established;
        needs `tier_of`, see audit_policy.tiebreak_settles)."""
        if auditor_pick not in AUDITOR_PICK_RULES:
            raise ValueError(f"unknown auditor_pick {auditor_pick!r}")
        if auditor_pick == "trusted-first" and tier_of is None:
            raise ValueError("auditor_pick='trusted-first' needs tier_of")
        if tiebreak_decides not in TIEBREAK_DECIDES_RULES:
            raise ValueError(f"unknown tiebreak_decides {tiebreak_decides!r}")
        if tiebreak_decides == "established-only" and tier_of is None:
            raise ValueError("tiebreak_decides='established-only' needs tier_of")
        self.path = Path(path)
        self.rng = rng or random.SystemRandom()
        self.active_seconds = active_seconds
        self.assign_seconds = assign_seconds
        self.read_only = read_only
        self.tier_of = tier_of
        self.auditor_pick = auditor_pick
        self.tiebreak_decides = tiebreak_decides
        self.untrusted = untrusted
        # `refusal-log-v1`: repeats of a refusal already in the audit trail, per rig, since
        # this process started (memory only, like the request limits).
        self._repeats: Counter[str] = Counter()
        self._repeats_lock = threading.Lock()
        if untrusted:
            self.read_only = True
            copy_guard.vet(self.path)
            with closing(self._connect()) as db:
                ledger_format.refuse_if_newer(ledger_format.read(db), "this ledger copy")
            return
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            # `ledger-format-v1`: checking a newer ledger with older code would read it
            # without the rules that version added.
            with closing(self._connect()) as db:
                ledger_format.refuse_if_newer(ledger_format.read(db))
            return
        self._prepare_private_files()
        # `backup-restore-v1` (app/run_lock.py): held while this ledger is open for writing,
        # so a restore can't swap the file under it (and it can't open one mid-restore).
        self._run_lock: run_lock.RunLock | None = run_lock.shared(self.path)
        try:
            with closing(self._connect()) as db:
                # `ledger-format-v1`: never run on a ledger a newer version wrote.
                ledger_format.refuse_if_newer(ledger_format.read(db))
                self._use_wal(db)
                self._upgrade(db)
        except BaseException:
            self.close()
            raise
        # `storage-full-v1` (app/storage.py): one idle connection held open for the life of
        # the ledger. SQLite deletes the -wal and -shm side files when its last connection
        # closes, and every request here opens and closes its own; on a full disk they then
        # couldn't be created again and even reads (health included) failed with "disk I/O
        # error". Never used for queries and never holds a transaction, so it blocks nothing.
        self._keeper: sqlite3.Connection | None = sqlite3.connect(
            self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self._keeper.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()

    # `ledger-format-v1`: the statements that make a fresh ledger, one at a time, so they
    # can run inside a transaction (executescript would commit first).
    _SCHEMA_STATEMENTS = tuple(s.strip() for s in SCHEMA.split(";") if s.strip())

    @staticmethod
    def _use_wal(db: sqlite3.Connection, wait_seconds: float = 10.0) -> None:
        """Switch the ledger to WAL mode. A ledger in the older journal mode (every
        restored one: `backup restore` leaves it so) needs a moment alone for the switch,
        and SQLite answers "database is locked" at once instead of waiting when another
        program is switching it too (seen: 1 in 60 opens with three programs starting
        together after a restore). So wait and try again, like any other lock."""
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                db.execute("PRAGMA journal_mode=WAL").fetchall()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def _upgrade(self, db: sqlite3.Connection) -> None:
        """Bring the ledger up to `ledger_format.FORMAT` in one transaction (see
        app/ledger_format.py). The write lock is taken first and everything is looked at
        again under it, so two programs opening an old ledger at once don't both add the
        same column; a crash part-way leaves the ledger as it was. Nothing is written when
        the ledger is already current."""
        db.execute("BEGIN IMMEDIATE")
        try:
            found = ledger_format.read(db)  # again: someone may have upgraded meanwhile
            ledger_format.refuse_if_newer(found)
            existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                                 "name='jobs'").fetchone() is not None
            for statement in self._SCHEMA_STATEMENTS:
                db.execute(statement)
            # Migrate older (Phase 1-3) databases in place.
            for table, columns in MIGRATIONS.items():
                have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
                for name, ddl in columns:
                    if name not in have:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            # After the migration, so older databases have the column first.
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS workers_key_hash ON workers(key_hash)")
            for ddl in SCALE_INDEXES + WORKER_INDEXES:
                db.execute(ddl)
            if found < ledger_format.FORMAT:
                if existed:
                    # At the newest time already written, so the ledger's clock never moves
                    # (`ledger-clock-v1`); the server clock's reading is for people.
                    at = db.execute(LATEST_TIME_SQL).fetchone()[0] or 0
                    self._event(db, int(at), ledger_format.UPGRADE_EVENT, detail=json.dumps({
                        "rule": ledger_format.RULE, "from": found,
                        "to": ledger_format.FORMAT, "server_clock": int(time.time())},
                        sort_keys=True))
                db.execute(f"PRAGMA user_version = {int(ledger_format.FORMAT)}")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

    def close(self) -> None:
        """Let go of the idle connection (the side files go once nothing else has the
        ledger open) and of the run lock (`backup-restore-v1`). The ledger still works
        afterwards, just without those protections."""
        keeper, self._keeper = getattr(self, "_keeper", None), None
        if keeper is not None:
            keeper.close()
        lock, self._run_lock = getattr(self, "_run_lock", None), None
        if lock is not None:
            lock.release()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - never raise from a finalizer
            pass

    # -- plumbing -----------------------------------------------------------------
    # SQLite side files that hold the same data as the ledger itself.
    SIDE_FILES = ("-wal", "-shm", "-journal")

    def _prepare_private_files(self) -> None:
        """`secret-file-v1`: the ledger holds every hidden test's expected answer, job
        inputs and key hashes. SQLite on its own creates it readable by every user on the
        machine (0644 under the usual umask). Create the folder (0700) and an empty file
        (0600) first, so SQLite and its -wal/-shm files inherit owner-only permissions,
        and refuse to start on an existing ledger, side file or folder that other users
        can read or change (the message says which `chmod` fixes it)."""
        secret_file.make_private_folder(self.path.parent, "the ledger")
        secret_file.create_empty(self.path, "the ledger")
        for name in ("",) + self.SIDE_FILES:
            side = self.path.with_name(self.path.name + name)
            try:
                secret_file.check_path(side, "the ledger" if not name else
                                       f"the ledger's {name[1:]} file", secret=True,
                                       folder=False)
            except FileNotFoundError:
                continue

    def _connect(self) -> sqlite3.Connection:
        if self.untrusted:
            db = copy_guard.connect(self.path)
        elif self.read_only:
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
            # `storage-full-v1`: after a failed COMMIT (disk full) or BEGIN (locked, I/O
            # error) SQLite has already undone the transaction, and a second ROLLBACK raised
            # "cannot rollback - no transaction is active", hiding the real error. Undo only
            # what is still open, and always re-raise the original error.
            if db.in_transaction:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db: sqlite3.Connection, now: int, event: str, job_id: str | None = None,
               worker_id: str | None = None, detail: str | None = None) -> None:
        db.execute("INSERT INTO events(job_id, worker_id, event, detail, at) VALUES(?,?,?,?,?)",
                   (job_id, worker_id, event, detail, now))

    def _refusal(self, db: sqlite3.Connection, now: int, event: str, job_id: str,
                 worker_id: str, detail: str | None = None) -> bool:
        """`refusal-log-v1`: write a refused submission to the audit trail only the first
        time this rig is refused on this job for this reason. A repeat says nothing new and
        is only counted in memory (`repeated_refusals`). Returns True if a row was written.
        The lookup goes through `events_by_job`, so it reads only this job's few events."""
        if event not in REFUSAL_EVENTS:
            raise ValueError(f"not a refusal event: {event!r}")
        seen = db.execute("SELECT 1 FROM events WHERE job_id=? AND worker_id=? AND event=? "
                          "AND detail IS ? LIMIT 1", (job_id, worker_id, event, detail)
                          ).fetchone()
        if seen is not None:
            with self._repeats_lock:
                self._repeats[worker_id] += 1
            return False
        self._event(db, now, event, job_id, worker_id, detail)
        return True

    def repeated_refusals(self) -> dict[str, int]:
        """Per rig: refused submissions not written because the same refusal (same job,
        same reason) is already in the audit trail. Since this process started."""
        with self._repeats_lock:
            return dict(sorted(self._repeats.items()))

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
            if job_kind == "challenge":
                self._refuse_if_revealed(db, fields["task_type"], fields["payload"],
                                         fields["payload_hash"])
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

    # -- retired hidden tests (`test-reuse-v1`) -------------------------------------
    @staticmethod
    def _revealed_until(db: sqlite3.Connection) -> int | None:
        latest: int | None = None
        for (detail,) in db.execute("SELECT detail FROM events WHERE event=?",
                                    (COPY_MADE_EVENT,)).fetchall():
            try:
                until = int(json.loads(detail or "{}")["until"])
            except (ValueError, KeyError, TypeError):
                continue  # detail is NULL inside a ledger copy; never on a live ledger
            latest = until if latest is None else max(latest, until)
        return latest

    def _refuse_if_revealed(self, db: sqlite3.Connection, task_type: str, payload: str,
                            payload_hash: str) -> None:
        until = self._revealed_until(db)
        if until is None:
            return
        if db.execute("SELECT 1 FROM jobs WHERE job_kind='challenge' AND payload_hash=? "
                      "AND created_at<? LIMIT 1", (payload_hash, until)).fetchone():
            raise HiddenTestRevealed(until, "exact")
        # `test-reuse-v2`: near copies. Hidden tests are few, so a scan is fine here; the
        # fingerprint (which includes the task type) is worked out from the stored payload,
        # so tests created before this rule are covered too.
        mine = tasks.input_fingerprint(task_type, payload)
        for (old_type, old_payload) in db.execute(
                "SELECT task_type, payload FROM jobs WHERE job_kind='challenge' "
                "AND created_at<?", (until,)):
            if tasks.input_fingerprint(old_type, old_payload) == mine:
                raise HiddenTestRevealed(until, "near-copy")


    def record_copy(self, now: int, until: int, file_sha256: str) -> None:
        """Note that a ledger copy covering everything before `until` was made. Not
        blocked by a pause: it changes no job, and forgetting it would be the unsafe side."""
        with self._tx() as db:
            self._event(db, now, COPY_MADE_EVENT,
                        detail=json.dumps({"until": int(until), "sha256": file_sha256}))

    def record_signed_report(self, now: int, since: int, until: int, report_sha256: str) -> None:
        """Note that a reward report for [since, until) was signed (`ledger-clock-v1`).
        Its time (at or after `until`) is then a recorded time, so the coordinator never
        writes anything into the signed period afterwards, even if the clock is set back."""
        with self._tx() as db:
            self._event(db, now, REPORT_SIGNED_EVENT, detail=json.dumps(
                {"since": int(since), "until": int(until), "report_sha256": report_sha256}))

    def latest_time(self) -> int | None:
        """The newest time written anywhere in the ledger (`ledger-clock-v1`): events, job
        updates, accepted results, rigs seen, enrollment codes. None for an empty ledger."""
        with closing(self._connect()) as db:
            row = db.execute(LATEST_TIME_SQL).fetchone()
        return None if row[0] is None else int(row[0])

    def revealed_until(self) -> int | None:
        """End of the latest period covered by a ledger copy made from this ledger."""
        with closing(self._connect()) as db:
            return self._revealed_until(db)

    def test_reuse(self) -> dict[str, Any]:
        """Operator-only numbers for `test-reuse-v2`. Inputs are compared by fingerprint
        (`tasks.input_fingerprint`), so near copies count as the same input."""
        with closing(self._connect()) as db:
            until = self._revealed_until(db)
            rows = db.execute("SELECT task_type, payload, status, created_at FROM jobs "
                              "WHERE job_kind='challenge'").fetchall()
        prints = [(tasks.input_fingerprint(r["task_type"], r["payload"]), r) for r in rows]
        uses = Counter(fp for fp, _ in prints)
        open_sharing = sum(1 for fp, r in prints
                           if r["status"] in ("queued", "leased") and uses[fp] > 1)
        revealed = 0 if until is None else len(
            {fp for fp, r in prints if int(r["created_at"]) < until})
        return {"rule": TEST_REUSE_RULE, "fingerprint": tasks.FINGERPRINT_RULE,
                "revealed_until": until, "retired_inputs": revealed,
                "open_sharing_input": open_sharing}

    def note(self, now: int, event: str, job_id: str | None = None,
             worker_id: str | None = None, detail: str | None = None) -> None:
        """Append one audit-trail event outside any other change (e.g. a refused receipt).
        Refusals (REFUSAL_EVENTS) are written once per job, rig and reason
        (`refusal-log-v1`)."""
        with self._tx() as db:
            if event in REFUSAL_EVENTS and job_id and worker_id:
                self._refusal(db, now, event, job_id, worker_id, detail)
            else:
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

    def claim_next(self, worker_id: str, lease_seconds: int, now: int,
                   lease_for: Callable[[str, str], int] | None = None) -> dict[str, Any] | None:
        """Atomically lease the oldest queued job `worker_id` may take, or return None.
        Work and challenge jobs are first-come; audits, tie-breakers and aimed hidden tests
        only go to the worker they were assigned to (see _assign_auditors).
        `lease_for(task_type, payload)` (`lease-fit-v1`, app/tasks.lease_needed) gives the
        seconds this job's lease must last; the lease is the longer of that and
        `lease_seconds`, and never past the job's own deadline.
        Raises CoordinatorPaused while paused; the worker still counts as seen, so rigs
        that keep asking during a pause stay active for audit picks after it."""
        with self._tx() as db:
            pause = self._pause_row(db)
            db.execute("UPDATE workers SET last_seen_at=? WHERE worker_id=?", (now, worker_id))
            if pause is None:
                return self._claim(db, worker_id, lease_seconds, now, lease_for)
        raise CoordinatorPaused(int(pause["at"]))

    def _claim(self, db: sqlite3.Connection, worker_id: str, lease_seconds: int,
               now: int, lease_for: Callable[[str, str], int] | None = None
               ) -> dict[str, Any] | None:
        self._sweep(db, now)
        self._assign_auditors(db, now)
        # An excluded worker's owner is excluded too, so one person's rigs never
        # audit or break ties on each other's results.
        owner = db.execute("SELECT owner FROM workers WHERE worker_id=?",
                           (worker_id,)).fetchone()
        row = db.execute(
            "SELECT job_id, task_type, payload FROM jobs j WHERE status='queued' AND NOT EXISTS ("
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
        if lease_for is not None:
            lease_seconds = max(lease_seconds, int(lease_for(row["task_type"], row["payload"])))
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
                self._refusal(db, now, "submission_conflict", job_id, worker_id)
                return "conflict", job
            if job["status"] == "expired":
                return "expired", job
            if job["status"] != "leased":
                return "not_leased", job
            if job["worker_id"] != worker_id:
                self._refusal(db, now, "submission_wrong_worker", job_id, worker_id)
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
        Siding with neither (or, under a loose tolerance, with both) settles nothing.
        `tiebreak-trust-v1`: under "established-only" a majority settles only when the
        tie-breaker and the rig it sided with are both established (records from before
        this vote); otherwise all three stay `disputed` for the review queue, no strike."""
        original = db.execute("SELECT * FROM submissions WHERE job_id=?",
                              (tb["audit_of"],)).fetchone()
        audit = db.execute(
            "SELECT s.* FROM submissions s JOIN jobs j USING(job_id) "
            "WHERE j.audit_of=? AND j.job_kind='audit'", (tb["audit_of"],)).fetchone()
        if original is None or audit is None or original["audit_status"] != "disputed":
            return
        sides = [s for s in (original, audit)
                 if result is not None and s["result"] is not None and agree(s["result"], result)]
        held_detail = None
        if len(sides) == 1 and self.tiebreak_decides != "always":
            # Worked out before any status below changes, so this vote can't lift a rig
            # into `established` on its own.
            tb_worker = db.execute("SELECT worker_id FROM submissions WHERE job_id=?",
                                   (tb["job_id"],)).fetchone()["worker_id"]
            history = self._audit_history_rows(db)
            assert self.tier_of is not None
            blank = {"checks_passed": 0, "strikes": 0}
            winner_tier = self.tier_of(history.get(sides[0]["worker_id"], blank))
            tb_tier = self.tier_of(history.get(tb_worker, blank))
            if not tiebreak_settles(self.tiebreak_decides, [winner_tier, tb_tier]):
                side = "original" if sides[0] is original else "audit"
                # Private: it names trust tiers. Ledger copies drop event details.
                held_detail = (f"held for review (tie-breaker {tb['job_id']}; sided with "
                               f"{side}; winner {winner_tier}, tie-breaker {tb_tier}; "
                               f"{self.tiebreak_decides})")
        if held_detail is not None:
            updates = dict.fromkeys((original["job_id"], audit["job_id"], tb["job_id"]),
                                    "disputed")
            for job_id in updates:
                db.execute("UPDATE submissions SET audit_status='disputed' WHERE job_id=?",
                           (job_id,))
                self._event(db, now, "tiebreak_held_for_review", job_id, detail=held_detail)
            return
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
    def entries_page(self, shown_up_to: int | None = None, with_hashes: bool = True,
                     limit: int = 100, after: tuple[int, str] | None = None,
                     newest_first: bool = False
                     ) -> tuple[list[dict[str, Any]], tuple[int, str] | None]:
        """`ledger-pages-v1`: at most `limit` accepted results in (accepted_at, job_id)
        order (or the reverse), starting after the row `after` names, and the cursor for
        the next page (None on the last one). Reads at most `limit + 1` rows through the
        `submissions_by_time` index, however big the ledger is. Same rows and same
        withholding as `entries`."""
        if not 1 <= limit <= MAX_PAGE:
            raise ValueError(f"limit must be 1-{MAX_PAGE}")
        op, direction = ("<", "DESC") if newest_first else (">", "ASC")
        where, args = "", []
        if after is not None:
            where = f"WHERE (accepted_at, job_id) {op} (?, ?) "
            args = [int(after[0]), str(after[1])]
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT job_id, worker_id, result_hash, reward_points, accepted_at, "
                f"verifier_method, confidence FROM submissions {where}"
                f"ORDER BY accepted_at {direction}, job_id {direction} LIMIT ?",
                (*args, limit + 1)).fetchall()
        more = len(rows) > limit
        out = [self._public_entry(dict(row), shown_up_to, with_hashes) for row in rows[:limit]]
        cursor = (out[-1]["accepted_at"], out[-1]["job_id"]) if more else None
        return out, cursor

    def entries_count(self) -> int:
        with closing(self._connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0])

    @staticmethod
    def _public_entry(entry: dict[str, Any], shown_up_to: int | None,
                      with_hashes: bool) -> dict[str, Any]:
        visible = shown_up_to is None or entry["accepted_at"] <= shown_up_to
        if not visible:
            entry["confidence"] = None
        if not with_hashes:
            del entry["result_hash"]
        return entry | {"settlement_visible": visible}

    def entries(self, shown_up_to: int | None = None,
                with_hashes: bool = True) -> list[dict[str, Any]]:
        """Accepted results. With `shown_up_to` (`public-delay-v1`, threat model #14),
        results accepted after it have their `confidence` withheld (None): a
        confirmed re-run flips it to "replicated", which would tell an auditor what its
        job was. `settlement_visible` says which rows are shown in full.

        `with_hashes=False` (`public-no-hashes-v1`) leaves out `result_hash`, the sha256
        of the answer: a re-check that agrees has the same hash as the job it re-checked,
        so showing it pairs the two for anyone, at any time."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT job_id, worker_id, result_hash, reward_points, accepted_at, "
                "verifier_method, confidence FROM submissions ORDER BY accepted_at, job_id"
            ).fetchall()
        return [self._public_entry(dict(row), shown_up_to, with_hashes) for row in rows]

    def submission(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM submissions WHERE job_id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def points_by_status(self, shown_up_to: int | None = None) -> dict[str, int]:
        """Simulated points per public settlement bucket (see SETTLEMENT). With
        `shown_up_to` (`public-delay-v1`), results accepted after it count as
        "pending" whatever their real status, so a hidden test that lands straight in
        confirmed or held can't be told from normal work right after it is submitted."""
        totals = dict.fromkeys(("confirmed", "pending", "unverified", "held"), 0)
        with closing(self._connect()) as db:
            for row in db.execute(
                    "SELECT audit_status, (? IS NOT NULL AND accepted_at > ?) AS recent, "
                    "SUM(reward_points) AS n FROM submissions GROUP BY audit_status, recent",
                    (shown_up_to, shown_up_to)).fetchall():
                bucket = "pending" if row["recent"] else SETTLEMENT.get(row["audit_status"],
                                                                        "held")
                totals[bucket] += int(row["n"] or 0)
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

    def agreement_groups(self) -> list[dict[str, Any]]:
        """Raw rows for the vouch check (app/vouches.py, `vouch-check-v1`, threat model #3).
        Read-only. One group per work job, listing the results in it (the original, its
        audit, its tie-breaker) that ended up `confirmed`, i.e. agreed with each other.
        Holds no payloads or answers, but names who audited whom: operator-only."""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT CASE WHEN j.job_kind IN ('audit','tiebreak') THEN j.audit_of "
                "ELSE j.job_id END AS root, j.job_id, j.job_kind, j.task_type, s.worker_id, "
                "s.reward_points, s.accepted_at FROM submissions s JOIN jobs j USING(job_id) "
                "WHERE s.audit_status='confirmed' AND j.job_kind IN ('work','audit','tiebreak') "
                "ORDER BY root, s.accepted_at, j.job_id").fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            group = groups.setdefault(row["root"], {"root": row["root"],
                                                    "task_type": row["task_type"],
                                                    "agreed": []})
            group["agreed"].append({k: row[k] for k in ("job_id", "job_kind", "worker_id",
                                                        "reward_points", "accepted_at")})
        return list(groups.values())

    def open_aimed_tests(self, now: int) -> dict[str, int]:
        """Per worker: hidden tests aimed at it by name (`target_worker`) that it can still
        take (queued or leased, before their deadline). Used by the vouch check so the
        operator doesn't aim a second test at a rig that already has one waiting.
        Tier-aimed tests aren't counted: the rig they land on is picked at hand-out."""
        with closing(self._connect()) as db:
            rows = db.execute(
                # Group by the target itself: `jobs` has its own worker_id column (who
                # leased it), and an alias with that name would group by that instead.
                "SELECT target, COUNT(*) AS n FROM jobs "
                "WHERE job_kind='challenge' AND target LIKE 'worker:%' "
                "AND status IN ('queued','leased') AND expires_at>? "
                "GROUP BY target", (now,)).fetchall()
        return {r["target"].removeprefix("worker:"): int(r["n"]) for r in rows}

    def check_record(self) -> dict[str, dict[str, Any]]:
        """Per worker: owner label, revoked or not, strikes, when the first result that
        became a strike was submitted, and how many hidden known-answer tests it passed."""
        strikes = ",".join("?" * len(STRIKE_STATUSES))
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT w.worker_id, w.owner, w.revoked_at, "
                f"COALESCE(SUM(s.audit_status IN ({strikes})), 0) AS strikes, "
                f"MIN(CASE WHEN s.audit_status IN ({strikes}) THEN s.accepted_at END) AS first_at, "
                f"COALESCE(SUM(s.audit_status='passed-challenge'), 0) AS passed "
                f"FROM workers w LEFT JOIN submissions s ON s.worker_id=w.worker_id "
                f"GROUP BY w.worker_id ORDER BY w.worker_id",
                (*STRIKE_STATUSES, *STRIKE_STATUSES)).fetchall()
        return {r["worker_id"]: {"owner": r["owner"], "revoked": r["revoked_at"] is not None,
                                 "strikes": int(r["strikes"]),
                                 "first_struck_result_at": r["first_at"],
                                 "hidden_tests_passed": int(r["passed"])} for r in rows}

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
            not_claimed   picks it never took up before `assign_seconds` ran out, and it
                          took no other job in that window either (silent)
            not_reached   picks it never took up because it was busy: it took other jobs
                          in that window. Jobs go out oldest first, so work queued before
                          the audit comes first; the rig never saw the audit and had no
                          say in it (`follow-through-v2`). Not a drop.
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
            " AND e.event='audit_assignment_expired' AND NOT " + _BUSY_IN_WINDOW + ") "
            " AS not_claimed, "
            "(SELECT COUNT(*) FROM events e WHERE e.worker_id=w.worker_id "
            " AND e.event='audit_assignment_expired' AND " + _BUSY_IN_WINDOW + ") "
            " AS not_reached, "
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
                                                  "not_reached", "lease_expired", "refused")}
                counts["dropped"] = (counts["not_claimed"] + counts["lease_expired"]
                                     + counts["refused"])
                rows[r["worker_id"]] = counts
        if worker_id:
            return rows.get(worker_id, dict.fromkeys(
                ("assigned", "completed", "not_claimed", "not_reached", "lease_expired",
                 "refused", "dropped"), 0))
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
                # `ledger-format-v1`: which format this ledger is (operator-only).
                "ledger_format": ledger_format.read(db),
            }
        return snapshot

    def public_job_events(self, job_id: str, hidden: Iterable[str],
                          limit: int = MAX_JOB_EVENTS) -> tuple[list[dict[str, Any]], int]:
        """`refusal-log-v1`: one job's events for the public `GET /v1/jobs/{id}`, without
        the `hidden` ones, oldest first, at most `limit`, plus how many there are in all.
        Filtered and capped inside SQLite through `events_by_job`, so the reply's cost
        doesn't depend on how much anyone wrote about this job.

        `public-events-v1` (code audit, nineteenth pass): only PUBLIC_EVENT_COLUMNS, never
        the event's `id`. Ids count up across the whole ledger, so they let anyone line up
        events of different jobs exactly: a re-check's `job_created` came one id after
        the original's `result_accepted` (same transaction), and a gap in one job's ids
        showed where a private event had been written. Order is still the real order."""
        hidden = tuple(hidden)
        marks = ",".join("?" * len(hidden)) or "NULL"
        where = f"job_id=? AND event NOT IN ({marks})"
        cols = ", ".join(PUBLIC_EVENT_COLUMNS)
        with closing(self._connect()) as db:
            rows = db.execute(f"SELECT {cols} FROM events WHERE {where} ORDER BY id LIMIT ?",
                              (job_id, *hidden, limit)).fetchall()
            total = db.execute(f"SELECT COUNT(*) FROM events WHERE {where}",
                               (job_id, *hidden)).fetchone()[0]
        return [dict(r) for r in rows], int(total)

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
