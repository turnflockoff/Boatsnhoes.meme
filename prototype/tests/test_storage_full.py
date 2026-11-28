"""`storage-full-v1` (app/storage.py): the coordinator on a disk that is filling up.

Before (shown on a real 8 MB ext4 disk, see docs/BUILD-LOG.md 0.5.66): once the disk was
full every route failed, `/health` and reads included, because each request's connection
had to re-create SQLite's -wal/-shm side files; the log showed "cannot rollback - no
transaction is active" instead of the real error; results got `500` and a dropped
connection and the demo worker threw them away; the operator health page said "ok" until
the very end."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import httpx
import pytest

import demo_worker
from app import storage
from app.config import Settings, get_settings
from app.ledger import Ledger
from app.main import create_app
from app.signing import ManifestSigner
from tests.auth_helpers import OP, FirstPickRng, WorkerClient, enroll, receipt
from tests.test_audits import T0, Clock, claim, sha
from tests.test_worker_guard import signed_job, signer, worker_main  # noqa: F401 (fixture)

MB = 1024 * 1024
PROTOTYPE = Path(__file__).resolve().parents[1]


class Disk:
    """A pretend disk whose free space a test sets (in MB, or None = can't be read)."""

    def __init__(self, free_mb: int | None = 50_000) -> None:
        self.free_mb = free_mb
        self.asked: list[Path] = []

    def __call__(self, folder: Path) -> int | None:
        self.asked.append(folder)
        return None if self.free_mb is None else self.free_mb * MB


def make(tmp_path: Path, disk: Disk | None = None, **overrides):
    disk = disk or Disk()
    fields = dict(database_path=tmp_path / "db.sqlite3", audit_rate=0.0, audit_rate_new=0.0,
                  audit_rate_suspect=0.0, operator_token=OP["X-Operator-Token"],
                  public_settle_delay_seconds=0)
    app = create_app(Settings(**(fields | overrides)), clock=Clock(), rng=FirstPickRng(1),
                     disk_free=disk)
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b"):
        enroll(client, w)
    return client, disk


def op_health(client) -> dict:
    r = client.get("/v1/operator/health", headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


def job_count(client) -> int:
    return sum(client.app.state.ledger.status_counts().values())


def submit(client, job_id: str, worker: str, result: str):
    return client.post(f"/v1/jobs/{job_id}/submit", json={
        "worker_id": worker, "result": result, "result_hash": sha(result),
        "started_at": T0, "completed_at": T0, **receipt(client, job_id)})


# -- what counts as "the disk is full or failing" ----------------------------------------------

def full_error(code: int | None, text: str) -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError(text)
    if code is not None:
        exc.sqlite_errorcode = code  # type: ignore[attr-defined]
    return exc


@pytest.mark.parametrize("code,text", [
    (13, "database or disk is full"), (10, "disk I/O error"),
    (10 | (13 << 8), "disk I/O error"),  # an extended I/O error code (SQLITE_IOERR_FSYNC)
    (None, "database or disk is full"), (None, "Disk I/O error"),
])
def test_full_and_failing_disks_are_storage_errors(code, text) -> None:
    assert storage.is_storage_error(full_error(code, text))


@pytest.mark.parametrize("exc", [
    full_error(5, "database is locked"), full_error(1, "no such table: jobs"),
    full_error(None, "near \"SELEC\": syntax error"), sqlite3.IntegrityError("UNIQUE failed"),
    OSError(28, "No space left on device"), ValueError("database or disk is full"),
])
def test_other_errors_are_not_storage_errors(exc) -> None:
    """A bug, a lock or a broken query must still surface as a real error."""
    assert not storage.is_storage_error(exc)


def test_a_real_sqlite_full_error_is_recognised(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "small.sqlite3")
    db.execute("CREATE TABLE t(x)")
    db.execute("PRAGMA max_page_count = 3")
    with pytest.raises(sqlite3.OperationalError) as caught:
        for _ in range(100):
            db.execute("INSERT INTO t VALUES(?)", ("x" * 4000,))
    assert storage.is_storage_error(caught.value)
    db.close()


# -- the free-space guard ----------------------------------------------------------------------

def test_the_guard_refuses_only_under_the_reserve(tmp_path: Path) -> None:
    disk = Disk(100)
    guard = storage.StorageGuard(tmp_path / "db.sqlite3", 100, 1000, disk)
    guard.check_new_work()  # exactly the reserve is still fine
    disk.free_mb = 99
    with pytest.raises(storage.StorageLow) as low:
        guard.check_new_work()
    assert low.value.free_mb == 99 and guard.refused_new_work == 1
    assert disk.asked[-1] == tmp_path  # the ledger's own folder, not the current one


def test_a_zero_reserve_never_refuses_and_says_so(tmp_path: Path) -> None:
    guard = storage.StorageGuard(tmp_path / "db.sqlite3", 0, 0, Disk(0))
    guard.check_new_work()
    snap = guard.snapshot()
    assert snap["taking_new_work"] is True and snap["free_mb"] == 0
    assert any("DISK_RESERVE_MB is 0" in w for w in guard.warnings(snap))


def test_unreadable_free_space_never_blocks_work_but_warns(tmp_path: Path) -> None:
    guard = storage.StorageGuard(tmp_path / "db.sqlite3", 100, 1000, Disk(None))
    guard.check_new_work()
    snap = guard.snapshot()
    assert snap["free_mb"] is None and snap["taking_new_work"] is True
    assert guard.warnings(snap) == ["couldn't read the free space on the ledger's disk; "
                                    "check it by hand (`df -h`)"]


@pytest.mark.parametrize("reserve,warn", [(-1, 10), (10, -1), (100, 50)])
def test_nonsense_settings_are_refused(tmp_path: Path, reserve: int, warn: int) -> None:
    with pytest.raises(ValueError):
        storage.StorageGuard(tmp_path / "db.sqlite3", reserve, warn, Disk())


def test_the_real_free_space_reader_matches_statvfs(tmp_path: Path) -> None:
    st = os.statvfs(tmp_path)
    got = storage.free_bytes(tmp_path)
    assert got is not None and abs(got - st.f_bavail * st.f_frsize) < 64 * MB
    assert storage.free_bytes(tmp_path / "missing" / "folder") is None


def test_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    defaults = get_settings()
    assert (defaults.disk_reserve_mb, defaults.disk_warn_mb) == (100, 1000)
    monkeypatch.setenv("DISK_RESERVE_MB", "500")
    monkeypatch.setenv("DISK_WARN_MB", "5000")
    s = get_settings()
    assert (s.disk_reserve_mb, s.disk_warn_mb) == (500, 5000)
    monkeypatch.setenv("DISK_RESERVE_MB", "-1")
    with pytest.raises(ValueError):
        get_settings()


# -- the coordinator ---------------------------------------------------------------------------

def test_under_the_reserve_new_work_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    client, disk = make(tmp_path)
    assert client.post("/v1/jobs", json={"payload": "before"}).status_code == 200
    before = job_count(client)
    disk.free_mb = 99
    replies = [
        client.post("/v1/jobs", json={"payload": "new"}),
        client.post("/v1/challenges", json={"payload": "test", "expected_result": sha("test")},
                    headers=OP),
        client.post("/v1/enrollments", json={"owner": "someone"}, headers=OP),
    ]
    for r in replies:
        assert r.status_code == 503, r.text
        assert r.headers["Retry-After"] == str(storage.LOW_STORAGE_RETRY_AFTER)
        assert r.json() == {"detail": storage.LOW_STORAGE_DETAIL}
        assert "99" not in r.text and "MB" not in r.text  # how full stays operator-only
    assert job_count(client) == before
    health = op_health(client)
    assert health["storage"]["taking_new_work"] is False
    assert health["storage"]["new_work_refused_since_start"] == 3
    assert any(w.startswith("ledger disk nearly full: 99 MB free") for w in health["warnings"])


def test_under_the_reserve_rigs_still_finish_and_hand_in_work(tmp_path: Path) -> None:
    """The room that's left is for results, so the queue drains instead of freezing."""
    client, disk = make(tmp_path)
    for p in ("one", "two"):
        assert client.post("/v1/jobs", json={"payload": p}).status_code == 200
    disk.free_mb = 10
    for _ in range(2):
        job = claim(client, "wk-a")
        assert job.status_code == 200, job.text
        r = submit(client, job.json()["job_id"], "wk-a", sha(job.json()["payload"]))
        assert r.status_code == 200, r.text
    assert claim(client, "wk-a").status_code == 404
    # Existing rigs can still update what they run, and the operator can still revoke.
    assert client.post("/v1/workers/wk-a/revoke", headers=OP).status_code == 200


def test_once_space_is_freed_new_work_carries_on_by_itself(tmp_path: Path) -> None:
    client, disk = make(tmp_path)
    disk.free_mb = 0
    assert client.post("/v1/jobs", json={"payload": "x"}).status_code == 503
    disk.free_mb = 100
    assert client.post("/v1/jobs", json={"payload": "x"}).status_code == 200
    assert op_health(client)["storage"]["taking_new_work"] is True


def test_public_health_says_nothing_about_the_disk(tmp_path: Path) -> None:
    client, disk = make(tmp_path)
    disk.free_mb = 5
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "storage" not in json.dumps(body) and "free" not in json.dumps(body)


def test_operator_health_warns_before_the_reserve(tmp_path: Path) -> None:
    client, disk = make(tmp_path)
    plenty = op_health(client)
    assert not any("disk" in w for w in plenty["warnings"])
    assert plenty["storage"] | {"ledger_mb": 0} == {
        "version": "storage-full-v1", "free_mb": 50_000, "reserve_mb": 100, "warn_mb": 1000,
        "ledger_mb": 0, "taking_new_work": True, "failed_writes_since_start": 0,
        "new_work_refused_since_start": 0}
    assert plenty["storage"]["ledger_mb"] >= 0
    disk.free_mb = 999
    warned = op_health(client)
    assert warned["status"] == "attention"
    assert [w for w in warned["warnings"] if "disk" in w] == [
        "ledger disk getting full: 999 MB free (DISK_WARN_MB is 1000). At 100 MB the "
        "coordinator stops taking new work"]
    disk.free_mb = 1000
    assert not any("disk" in w for w in op_health(client)["warnings"])


def test_a_write_that_fails_for_lack_of_space_is_a_polite_503(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = make(tmp_path)
    assert client.post("/v1/jobs", json={"payload": "p"}).status_code == 200
    job = claim(client, "wk-a").json()
    ledger = client.app.state.ledger
    real_submit = ledger.submit

    def full(*args, **kwargs):
        raise full_error(13, "database or disk is full")
    monkeypatch.setattr(ledger, "submit", full)
    r = submit(client, job["job_id"], "wk-a", sha("p"))
    assert r.status_code == 503 and r.headers["Retry-After"] == "60"
    assert r.json() == {"detail": storage.WRITE_FAILED_DETAIL}
    health = op_health(client)
    assert health["storage"]["failed_writes_since_start"] == 1
    assert any("1 write(s) failed since start" in w for w in health["warnings"])
    # Once there's room again the very same result goes through.
    monkeypatch.setattr(ledger, "submit", real_submit)
    assert submit(client, job["job_id"], "wk-a", sha("p")).status_code == 200


def test_other_database_errors_are_still_real_errors(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = make(tmp_path)

    def broken(*args, **kwargs):
        raise full_error(1, "no such table: jobs")
    monkeypatch.setattr(client.app.state.ledger, "claim_next", broken)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        claim(client, "wk-a")
    assert op_health(client)["storage"]["failed_writes_since_start"] == 0


def test_the_storage_log_line_has_no_file_paths(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    client, _ = make(tmp_path)

    def full(*args, **kwargs):
        raise full_error(10, f"disk I/O error at {tmp_path}/db.sqlite3-wal")
    monkeypatch.setattr(client.app.state.ledger, "claim_next", full)
    with caplog.at_level("ERROR", logger="bnh.coordinator"):
        assert claim(client, "wk-a").status_code == 503
    [line] = [r for r in caplog.records if r.getMessage() == "storage write failed"]
    assert line.sqlite_code == 10 and line.error_type == "OperationalError"
    assert str(tmp_path) not in json.dumps({k: str(v) for k, v in vars(line).items()})


# -- the ledger --------------------------------------------------------------------------------

def side_files(path: Path) -> set[str]:
    return {s for s in ("-wal", "-shm") if path.with_name(path.name + s).exists()}


def test_the_side_files_stay_while_the_ledger_is_open(tmp_path: Path) -> None:
    """Before, they vanished after every request and had to be made again by the next one."""
    path = tmp_path / "db.sqlite3"
    ledger = Ledger(path)
    ledger.ping()
    ledger.status_counts()
    assert side_files(path) == {"-wal", "-shm"}
    ledger.close()
    ledger.close()  # twice is fine
    assert side_files(path) == set()
    ledger.ping()  # still usable, just without the protection


def test_read_only_ledgers_hold_nothing_open(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    Ledger(path).close()
    before = path.read_bytes()
    ro = Ledger(path, read_only=True)
    ro.ping()
    assert getattr(ro, "_keeper", None) is None
    assert path.read_bytes() == before  # (a read-only SQLite reader may still make side files)


def test_the_idle_connection_never_blocks_writers_or_checkpoints(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    ledger = Ledger(path)
    keeper = ledger._keeper
    assert keeper is not None and not keeper.in_transaction
    with sqlite3.connect(path, timeout=0.1, isolation_level=None) as other:
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO events(job_id, worker_id, event, detail, at) "
                      "VALUES(NULL, NULL, 'x', NULL, 1)")
        other.execute("COMMIT")
        busy, log_frames, done = other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert busy == 0 and log_frames == done
    ledger.close()


def test_a_failed_write_raises_its_own_error_not_the_rollbacks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Before: "cannot rollback - no transaction is active" hid "database or disk is full"."""
    ledger = Ledger(tmp_path / "db.sqlite3")
    real_connect = ledger._connect

    def small_disk() -> sqlite3.Connection:
        db = real_connect()
        db.execute(f"PRAGMA max_page_count = "
                   f"{db.execute('PRAGMA page_count').fetchone()[0]}")
        return db
    monkeypatch.setattr(ledger, "_connect", small_disk)
    with pytest.raises(sqlite3.OperationalError) as caught:
        for i in range(200):
            ledger.note(T0, "filler", detail="x" * 3000)
    assert storage.is_storage_error(caught.value), caught.value
    assert "rollback" not in str(caught.value)


def test_a_locked_ledger_raises_locked_not_the_rollbacks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "db.sqlite3"
    ledger = Ledger(path)
    monkeypatch.setattr(ledger, "_connect", lambda: sqlite3.connect(
        path, timeout=0.05, isolation_level=None))
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            ledger.note(T0, "blocked")
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_write_that_failed_part_way_leaves_nothing_behind(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = Ledger(tmp_path / "db.sqlite3")
    before = len(ledger.events())

    class Boom(Exception):
        pass
    with pytest.raises(Boom):
        with ledger._tx() as db:
            ledger._event(db, T0, "half-done")
            raise Boom
    assert len(ledger.events()) == before


# -- a disk that really is full (a separate process with a file-size limit) --------------------

FULL_DISK_RUN = textwrap.dedent("""
    import json, resource, signal, sys
    sys.path.insert(0, sys.argv[2])
    from pathlib import Path
    from app.config import Settings
    from app.main import create_app
    from tests.auth_helpers import OP, WorkerClient, enroll, receipt
    from tests.test_audits import sha
    keep = sys.argv[3] == "keep"
    path = Path(sys.argv[1]) / "ledger" / "db.sqlite3"
    app = create_app(Settings(database_path=path, operator_token=OP["X-Operator-Token"],
                              audit_rate=0.0, audit_rate_new=0.0, audit_rate_suspect=0.0,
                              public_settle_delay_seconds=0, disk_reserve_mb=0))
    c = WorkerClient(app)
    enroll(c, "wk-a")
    for i in range(3):
        c.post("/v1/jobs", json={"payload": f"p{i}"})
    job = c.post("/v1/jobs/next", params={"worker_id": "wk-a"}).json()
    rec = receipt(c, job["job_id"])
    if not keep:
        app.state.ledger.close()  # what every request did before storage-full-v1
    # From here nothing can be written past the first 16 KB of any file, less than SQLite
    # needs for a fresh -shm file (32 KB) or for a result: like a disk with nothing left.
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (16384, 16384))
    out = {}
    for name, call in [
        ("health", lambda: c.get("/health")),
        ("ledger", lambda: c.get("/v1/ledger")),
        ("op_health", lambda: c.get("/v1/operator/health", headers=OP)),
        ("claim", lambda: c.post("/v1/jobs/next", params={"worker_id": "wk-a"})),
        ("submit", lambda: c.post(f"/v1/jobs/{job['job_id']}/submit", json={
            "worker_id": "wk-a", "result": sha(job["payload"]),
            "result_hash": sha(sha(job["payload"])), "started_at": 1, "completed_at": 1,
            **rec})),
        ("new_job", lambda: c.post("/v1/jobs", json={"payload": "x" * 4000})),
    ]:
        try:
            out[name] = call().status_code
        except Exception as exc:
            out[name] = type(exc).__name__ + ": " + str(exc)
    print(json.dumps(out))
""")


def run_on_a_full_disk(tmp_path: Path, mode: str) -> dict:
    script = tmp_path / "full_disk_run.py"
    script.write_text(FULL_DISK_RUN)
    done = subprocess.run([sys.executable, str(script), str(tmp_path), str(PROTOTYPE), mode],
                          capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not hasattr(os, "statvfs") or sys.platform == "win32",
                    reason="needs a POSIX file-size limit")
def test_on_a_really_full_disk_reads_keep_working_and_writes_say_try_later(
        tmp_path: Path) -> None:
    out = run_on_a_full_disk(tmp_path, "keep")
    assert out["health"] == 200 and out["ledger"] == 200 and out["op_health"] == 200
    # Writes can't happen, but they are a polite "come back", not a crash.
    assert out["submit"] == 503 and out["claim"] == 503 and out["new_job"] == 503


@pytest.mark.skipif(not hasattr(os, "statvfs") or sys.platform == "win32",
                    reason="needs a POSIX file-size limit")
def test_without_the_idle_connection_even_health_fails_on_a_full_disk(tmp_path: Path) -> None:
    """The "before" picture, kept as a test so the reason for the idle connection stays
    visible: with nothing holding the side files open, nothing works on a full disk."""
    out = run_on_a_full_disk(tmp_path, "close")
    assert out["health"] == 503  # the health check itself can't read the ledger


# -- the demo worker ---------------------------------------------------------------------------

def test_the_worker_retries_a_result_the_coordinator_couldnt_save(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        signer: ManifestSigner) -> None:
    monkeypatch.setattr(demo_worker.time, "time", lambda: T0)
    waits: list[float] = []
    monkeypatch.setattr(demo_worker, "pause", waits.append)
    job = signed_job(signer)
    answers = iter([
        httpx.Response(503, json={"detail": storage.WRITE_FAILED_DETAIL},
                       headers={"Retry-After": "60"}),
        httpx.Response(503, json={"detail": storage.WRITE_FAILED_DETAIL},
                       headers={"Retry-After": "60"}),
        httpx.Response(200, json={"status": "accepted"}),
    ])
    submitted: list[str] = []

    def coordinator(path: str) -> httpx.Response:
        if path.endswith("/submit"):
            submitted.append(path)
            return next(answers)
        return httpx.Response(200, json=job)
    out = worker_main(tmp_path, monkeypatch, signer, coordinator, capsys, ["--once"])
    assert len(submitted) == 3 and waits == [60, 60]
    assert "try again later" in out and "200 " in out


def test_the_worker_stops_asking_for_work_on_a_503_and_says_why(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        signer: ManifestSigner) -> None:
    calls: list[str] = []

    def coordinator(path: str) -> httpx.Response:
        calls.append(path)
        return httpx.Response(503, json={"detail": storage.WRITE_FAILED_DETAIL},
                              headers={"Retry-After": "60"})
    out = worker_main(tmp_path, monkeypatch, signer, coordinator, capsys, [])
    assert calls == ["/v1/jobs/next"]  # a claim isn't retried in a loop
    assert "isn't handing out work right now" in out and "full or failing" in out


def test_claims_are_still_only_retried_on_429() -> None:
    import inspect
    assert inspect.signature(demo_worker.post_with_backoff).parameters[
        "retry_on"].default == (429,)


def test_concurrent_requests_with_the_idle_connection(tmp_path: Path) -> None:
    """The idle connection is shared by no one: many threads keep working side by side."""
    client, _ = make(tmp_path)
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(5):
                assert client.post("/v1/jobs", json={"payload": f"{n}-{i}"}).status_code == 200
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and job_count(client) == 30


def test_a_rollback_that_fails_too_never_hides_the_first_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when undoing fails as well (a disk that is failing, not just full), the error
    that reaches the log is the one that started it."""
    ledger = Ledger(tmp_path / "db.sqlite3")
    real_connect = ledger._connect

    class FailingDisk:
        def __init__(self) -> None:
            self.db = real_connect()
            self.in_transaction = True

        def execute(self, sql: str, *args):
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("disk I/O error")
            if sql.startswith("INSERT"):
                raise full_error(13, "database or disk is full")
            return self.db.execute(sql, *args)

        def close(self) -> None:
            self.db.close()
    monkeypatch.setattr(ledger, "_connect", FailingDisk)
    with pytest.raises(sqlite3.OperationalError, match="database or disk is full"):
        ledger.note(T0, "anything")
