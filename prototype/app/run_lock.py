"""Who is using the ledger right now (`backup-restore-v1`, code audit sixteenth pass).

Restoring a backup swaps the ledger file. If the coordinator (or an operator tool) still
has the old file open, it keeps writing into the file that was moved aside, and its
SQLite side files (`-wal`, `-shm`) can be replayed into the restored one, which damages it
("database disk image is malformed"; shown in docs/BUILD-LOG.md 0.5.67). So:

  * every program that opens the ledger for writing (the coordinator, `app.workers`,
    `app.incident`, report signing, ...) holds a *shared* lock on `<ledger>.lock` for as
    long as it has the ledger open. Any number of them can hold it at once;
  * `python -m app.backup restore` needs the *exclusive* lock. It gets it only when nobody
    else has the ledger open, and while it holds it nothing else can open the ledger for
    writing (they stop with `LedgerBusy` and a plain message).

The lock file is empty and owner-only; the operating system drops a lock by itself when
the program holding it exits or crashes, so a crash never leaves the ledger locked.
Honest limits: these are advisory `flock` locks. They work between programs on the same
machine (Linux, macOS); not on Windows (no lock is taken there), and not reliably for a
ledger on a network drive. Read-only views don't take the lock.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no advisory locks
    fcntl = None  # type: ignore[assignment]

RULE = "backup-restore-v1"

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class LedgerBusy(RuntimeError):
    """The ledger is being restored (for a shared lock) or is in use (for the exclusive
    one). The message says what to do."""


def lock_path(ledger: Path) -> Path:
    ledger = Path(ledger)
    return ledger.with_name(ledger.name + ".lock")


class RunLock:
    """An flock on `<ledger>.lock`, shared or exclusive. Never waits."""

    def __init__(self, ledger: Path, exclusive: bool) -> None:
        self.path = lock_path(ledger)
        self.exclusive = exclusive
        self._fd: int | None = None
        if fcntl is None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            if exclusive:
                raise LedgerBusy(
                    "the ledger is in use (the coordinator or an operator tool has it open); "
                    "stop the coordinator and wait for any tool to finish, then run the "
                    "restore again") from None
            raise LedgerBusy(
                "a restore from a backup is running on this ledger "
                "(`python -m app.backup restore`); start again when it has finished") from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # closing the file drops the lock

    def __enter__(self) -> "RunLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:  # noqa: BLE001 - never raise from a finalizer
            pass


def shared(ledger: Path) -> RunLock:
    return RunLock(ledger, exclusive=False)


def exclusive(ledger: Path) -> RunLock:
    return RunLock(ledger, exclusive=True)
