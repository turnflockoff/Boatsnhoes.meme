"""Secret and pinned files on disk (`secret-file-v1`, code audit eighth pass).

Earlier passes looked at what comes in over the network and in files from strangers. This
one looks at the files our own programs keep on the machine they run on, and who else on
that machine can read or change them:

    data/prototype.sqlite3     the coordinator's ledger. Besides points it holds every
                               hidden test's EXPECTED ANSWER and who it is aimed at, job
                               inputs, and hashes of worker keys and enrollment codes.
    coordinator-signing.key    the coordinator's Ed25519 private key.
    data/worker-<id>.key       a worker's key (what it logs in with).
    data/coordinator.pub       the coordinator public key a worker pinned, and
    data/coordinator-handover.pub  the offline handover key it pinned. Not secret, but
                               whoever can change them can make the worker trust their
                               own coordinator.

Found in this pass, shown on a real machine with a second, ordinary user account
(tests/test_secret_files.py replays each one):

  * the ledger was created readable by every user on the machine (0644 under the usual
    umask), so any other account could read the answers to every hidden test;
  * the worker read its key file without checking who else can read it (the coordinator's
    signing key already had that check), and read the pinned keys without checking who can
    change them, including a folder anyone can write to, where another user can swap
    `coordinator.pub` for their own key;
  * a named pipe in place of a key file hung the worker or the coordinator forever, and
    the signing key's "only you can read it" check looked at the path and then read it
    again by name, so the file could be swapped in between.

The rules, in one place:

  1. folders we create for these files are owner-only (0700); files we create are 0600 and
     never replace an existing file or follow a planted link;
  2. a file is opened once, without blocking, and everything is checked on that open file:
     a regular file (never more than `max_bytes` read), owned by this user (or root), and
       - secrets: nobody else can read or change it   (else: `chmod 600 <file>`)
       - pins:    nobody else can change it            (else: `chmod 600 <file>`)
  3. the folder holding it can't be changed by other users (else: `chmod 700 <folder>`),
     because whoever can write the folder can delete the file and put their own in its
     place.

Honest limits: this guards against *other accounts* on the same machine and against
mistakes (a key copied with the wrong permissions, a key put in a shared folder). Anyone
who is root, or who runs as the same user, can still read everything. Windows keeps
permissions differently; there only the regular-file and size checks apply.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from app.worker_guard import say

SECRET_FILE_VERSION = "secret-file-v1"

MAX_KEY_BYTES = 64 * 1024  # a PEM key is ~120 bytes; a worker key 48; a pin 65

_POSIX = os.name == "posix"
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class UnsafeFileError(ValueError):
    """A secret or pinned file (or its folder) that other users can read or change, or
    that isn't a plain file. The message says how to fix it."""


def _shown(path: Path) -> str:
    return say(str(path), 160)


def _owned_by_us(info: os.stat_result) -> bool:
    return info.st_uid in (os.geteuid(), 0)


def check_folder(folder: str | os.PathLike[str], what: str = "folder") -> None:
    """The folder must be ours (or root's) and not writable by anyone else."""
    if not _POSIX:
        return
    folder = Path(folder)
    try:
        info = os.stat(folder)
    except OSError as exc:
        raise UnsafeFileError(f"the folder of {what} ({_shown(folder)}) can't be checked "
                              f"({exc.strerror or 'error'})") from None
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeFileError(f"{_shown(folder)} is not a folder")
    if not _owned_by_us(info):
        raise UnsafeFileError(f"the folder of {what} ({_shown(folder)}) belongs to another "
                              f"user, who could swap the file; keep it in your own folder")
    if info.st_mode & 0o022:
        raise UnsafeFileError(f"other users can change the folder of {what} "
                              f"({_shown(folder)}) and swap the file; run: chmod 700 "
                              f"{_shown(folder)}")


def make_private_folder(folder: str | os.PathLike[str], what: str = "folder") -> None:
    """Create `folder` (and missing parents) owner-only if it doesn't exist, then check it.
    An existing folder is never changed, only checked."""
    folder = Path(folder)
    if not folder.exists():
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_folder(folder, what)


def check_open_file(fd: int, path: Path, what: str, *, secret: bool) -> os.stat_result:
    """Checks on an already open file: regular, ours, and (on POSIX) not readable
    (secrets) or changeable (pins) by others. (`read` limits the size while reading.)"""
    info = os.fstat(fd)
    label = f"{what} {_shown(path)}"
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeFileError(f"{label} is not a regular file")
    if _POSIX:
        if not _owned_by_us(info):
            raise UnsafeFileError(f"{label} belongs to another user; use your own copy")
        if secret and info.st_mode & 0o077:
            raise UnsafeFileError(f"{label} can be read by other users; run: chmod 600 "
                                  f"{_shown(path)} (and check nobody copied it)")
        if not secret and info.st_mode & 0o022:
            raise UnsafeFileError(f"{label} can be changed by other users; run: chmod 600 "
                                  f"{_shown(path)} and check it still holds the right key")
    return info


def check_path(path: str | os.PathLike[str], what: str, *, secret: bool,
               folder: bool = True) -> None:
    """Check an existing file by opening it (never blocking, never reading it)."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | _O_NONBLOCK | _O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UnsafeFileError(f"{what} {_shown(path)} can't be opened "
                              f"({exc.strerror or 'error'})") from None
    try:
        check_open_file(fd, path, what, secret=secret)
    finally:
        os.close(fd)
    if folder:
        check_folder(path.parent, what)


def read(path: str | os.PathLike[str], what: str, *, secret: bool,
         max_bytes: int = MAX_KEY_BYTES, folder: bool = True) -> bytes:
    """Read a secret or pinned file after the checks above. FileNotFoundError if missing,
    UnsafeFileError for everything else. Never reads more than `max_bytes`."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | _O_NONBLOCK | _O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UnsafeFileError(f"{what} {_shown(path)} can't be opened "
                              f"({exc.strerror or 'error'})") from None
    try:
        check_open_file(fd, path, what, secret=secret)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1 << 16, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:  # also if it grows while being read
                raise UnsafeFileError(f"{what} {_shown(path)} is too big (over {max_bytes} "
                                      "bytes)")
    finally:
        os.close(fd)
    if folder:
        check_folder(path.parent, what)
    return b"".join(chunks)


def read_text(path: str | os.PathLike[str], what: str, *, secret: bool,
              max_bytes: int = MAX_KEY_BYTES, folder: bool = True) -> str:
    """`read`, decoded as strict ASCII with surrounding whitespace removed (keys and pins
    are plain ASCII; anything else is a wrong or damaged file)."""
    data = read(path, what, secret=secret, max_bytes=max_bytes, folder=folder)
    try:
        return data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise UnsafeFileError(f"{what} {_shown(Path(path))} is not a plain-text key "
                              "file") from None


def write_new(path: str | os.PathLike[str], data: bytes, what: str, *,
              folder: bool = True) -> None:
    """Create `path` owner-only (0600) with `data`. Refuses to replace anything already
    there, including a planted link. With `folder`, a missing folder is created owner-only
    and an existing one is checked first."""
    path = Path(path)
    if folder:
        make_private_folder(path.parent, what)
    else:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        if fd != -1:
            os.close(fd)


def replace(path: str | os.PathLike[str], data: bytes, what: str) -> None:
    """Swap `path` for a new owner-only file holding `data` in one step (a crash leaves
    the old or the new file whole). The temporary file is never a planted link."""
    path = Path(path)
    make_private_folder(path.parent, what)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    write_new(tmp, data, what, folder=False)
    os.replace(tmp, path)


def create_empty(path: str | os.PathLike[str], what: str) -> bool:
    """Create an empty owner-only file if nothing is at `path` yet (used for the SQLite
    ledger before SQLite creates it world-readable). True if it was created."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                     0o600)
    except FileExistsError:
        return False
    os.close(fd)
    return True
