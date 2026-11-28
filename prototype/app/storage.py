"""Running low on, or out of, disk space (`storage-full-v1`).

Fifteenth code-audit pass: what happens to the coordinator when the disk under its ledger
fills up? Shown on a real 8 MB ext4 disk on this sandbox (see docs/BUILD-LOG.md, 0.5.66):

1. Everything stopped, even reading. Each request opens its own database connection, and
   when the last one closes SQLite deletes its two side files (`-wal` and `-shm`). On a
   full disk the next request can't create them again, so every route, `/health`
   included, failed with "disk I/O error": rigs couldn't even be told to wait.
2. The real reason was hidden. After a failed write, the ledger tried to undo a
   transaction SQLite had already undone, and that second error ("cannot rollback - no
   transaction is active") is what reached the log.
3. Rigs lost finished work. Results answered with `500` and a dropped connection; the
   demo worker only retried `429`, so the result was thrown away and the job re-run later.
4. Nothing warned first. The operator health page said "ok" right up to the moment the
   disk was full.

The fixes live in three places:
- app/ledger.py keeps one idle connection open for the life of the coordinator, so the
  side files stay and reads (health, ledger, trust pages) keep working on a full disk,
  and a failed write now raises its own error, not the rollback's.
- this module: a free-space check with a reserve. Below `DISK_RESERVE_MB` the coordinator
  stops taking NEW work (jobs, hidden tests, enrollment codes) so the room left goes to
  results from rigs that already did the work; claims and results carry on, so the queue
  drains. Below `DISK_WARN_MB` the operator health page warns. A write that still fails
  for lack of space gets a polite `503` with `Retry-After`, never a `500`.
- demo_worker.py retries a result after a `503` (results are idempotent), so a full-disk
  moment costs a rig a short wait, not its work.

Nothing here deletes or moves anything: freeing space is the operator's call.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

STORAGE_VERSION = "storage-full-v1"
MB = 1024 * 1024
# How long a rig is asked to wait: low storage needs a person, a failed write may pass.
LOW_STORAGE_RETRY_AFTER = 300
WRITE_FAILED_RETRY_AFTER = 60
LOW_STORAGE_DETAIL = ("coordinator is low on storage and isn't taking new work right now; "
                      "results for jobs already handed out are still accepted")
WRITE_FAILED_DETAIL = ("coordinator could not save to its storage (full or failing); "
                       "nothing was changed, try again later")

# SQLite's primary result codes (the low byte of an extended code).
SQLITE_IOERR = 10
SQLITE_FULL = 13
_STORAGE_MESSAGES = ("database or disk is full", "disk i/o error")


class StorageLow(Exception):
    """Free space is under the reserve: refuse new work, keep the room for results."""

    def __init__(self, free_mb: int) -> None:
        super().__init__(f"{free_mb} MB free")
        self.free_mb = free_mb


def is_storage_error(exc: BaseException) -> bool:
    """True for SQLite errors that mean the disk is full or failing (not a bug or a lock)."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF in (SQLITE_IOERR, SQLITE_FULL):
        return True
    return str(exc).strip().lower() in _STORAGE_MESSAGES


def free_bytes(folder: Path) -> int | None:
    """Space an ordinary program can still use on the disk holding `folder` (what `df`
    shows as "Avail"), or None if it can't be read."""
    try:
        st = os.statvfs(folder)
    except (OSError, AttributeError):  # AttributeError: no statvfs on this OS
        return None
    return st.f_bavail * st.f_frsize


def ledger_bytes(path: Path) -> int:
    """The ledger plus its side files, as they are on disk right now."""
    total = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            total += path.with_name(path.name + suffix).stat().st_size
        except OSError:
            continue
    return total


class StorageGuard:
    """Free-space checks for the ledger's disk. `free` is injectable for tests."""

    def __init__(self, ledger_path: Path, reserve_mb: int, warn_mb: int,
                 free: Callable[[Path], int | None] = free_bytes) -> None:
        if reserve_mb < 0 or warn_mb < 0:
            raise ValueError("DISK_RESERVE_MB and DISK_WARN_MB can't be negative")
        if reserve_mb and warn_mb and warn_mb < reserve_mb:
            raise ValueError("DISK_WARN_MB must be at least DISK_RESERVE_MB")
        self.path = Path(ledger_path)
        self.reserve_mb = reserve_mb
        self.warn_mb = warn_mb
        self._free = free
        self._lock = threading.Lock()
        self.failed_writes = 0
        self.refused_new_work = 0

    def free_mb(self) -> int | None:
        value = self._free(self.path.parent)
        return None if value is None else value // MB

    def check_new_work(self) -> None:
        """Raise StorageLow if free space is under the reserve. An unreadable free-space
        figure never blocks work (the warning says so instead)."""
        if not self.reserve_mb:
            return
        free = self.free_mb()
        if free is not None and free < self.reserve_mb:
            with self._lock:
                self.refused_new_work += 1
            raise StorageLow(free)

    def record_failed_write(self) -> None:
        with self._lock:
            self.failed_writes += 1

    def snapshot(self) -> dict[str, Any]:
        free = self.free_mb()
        low = bool(self.reserve_mb) and free is not None and free < self.reserve_mb
        with self._lock:
            failed, refused = self.failed_writes, self.refused_new_work
        return {"version": STORAGE_VERSION, "free_mb": free, "reserve_mb": self.reserve_mb,
                "warn_mb": self.warn_mb, "ledger_mb": round(ledger_bytes(self.path) / MB, 1),
                "taking_new_work": not low, "failed_writes_since_start": failed,
                "new_work_refused_since_start": refused}

    def warnings(self, snap: dict[str, Any] | None = None) -> list[str]:
        snap = snap or self.snapshot()
        free = snap["free_mb"]
        out: list[str] = []
        if free is None:
            out.append("couldn't read the free space on the ledger's disk; check it by hand "
                       "(`df -h`)")
        elif not snap["taking_new_work"]:
            out.append(f"ledger disk nearly full: {free} MB free, under DISK_RESERVE_MB "
                       f"({self.reserve_mb} MB). New jobs, hidden tests and enrollment codes "
                       "are refused so the rest is kept for results; free some space (old "
                       "logs, backups, model files) and it carries on by itself")
        elif self.warn_mb and free < self.warn_mb:
            out.append(f"ledger disk getting full: {free} MB free (DISK_WARN_MB is "
                       f"{self.warn_mb}). At {self.reserve_mb} MB the coordinator stops "
                       "taking new work")
        if snap["failed_writes_since_start"]:
            out.append(f"{snap['failed_writes_since_start']} write(s) failed since start "
                       "because the disk was full or failing; the rigs were asked to retry. "
                       "If the disk isn't full, check it for hardware errors (`dmesg`)")
        if not self.reserve_mb:
            out.append("DISK_RESERVE_MB is 0, so nothing keeps room for results when the "
                       "disk fills up")
        return out
