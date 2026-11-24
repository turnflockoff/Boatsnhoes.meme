"""Opening someone else's ledger copy safely (`copy-open-v1`, code audit third pass).

Ledger copies are meant to be downloaded and re-checked by anyone (`rewards verify --db`,
`ledger_copy compare`, `publish_report add|check`, and the /reports/ page tells people to
do exactly that). So a copy is a file from a stranger, and SQLite files can do more than
hold rows:

    views        a table can be swapped for a view. A view is code: it can make the
                 numbers depend on today's date (`date('now')`), on the reader's SQLite
                 version or on `random()`, so the same file (same sha256) verifies today and
                 shows different points next month. Or it can loop for hours.
    triggers     never run on reads, but have no place in a copy either.
    hot journal  a `-journal` or `-wal` file left next to the copy is replayed by a normal
                 open, which changes the copy's bytes on the reader's disk.
    bad pages    a damaged or crafted b-tree can make reads fail halfway or return rows
                 an index doesn't agree with.
    huge values  one giant cell or file can eat all memory or disk time.

What this module does before any of our code reads a copy:

  1. the file must be a regular file, not empty, at most `MAX_COPY_BYTES`, and start with
     the SQLite header;
  2. it is opened read-only *and* immutable (`?mode=ro&immutable=1`): SQLite ignores any
     `-journal`/`-wal`/`-shm` beside it and never writes, so checking a copy can't change
     it (and its sha256 stays the published one);
  3. `trusted_schema=OFF`, `cell_size_check=ON`, `mmap_size=0`, `query_only=ON`, no
     ATTACH, values capped at `MAX_VALUE_BYTES`, and (Python 3.12+) defensive mode with
     views and triggers switched off at the connection; every query is stopped after
     `QUERY_SECONDS`;
  4. the schema must be exactly what `ledger_copy export` writes: the ledger's tables plus
     `copy_info` (and SQLite's own `sqlite_sequence`), the same columns and types, only the
     ledger's own indexes, no views, no triggers, no virtual tables, no extra tables;
  5. `PRAGMA integrity_check` must say `ok`, and every row of every table is read once
     (a value over the size cap only fails when it is loaded);
  6. `copy_info` must hold exactly one row.

A copy that fails any of these is refused with `UnsafeCopyError` (a `sqlite3.DatabaseError`,
so callers that already handle unreadable files handle it too). Honest copies made by
`ledger_copy export` always pass: export runs this check on its own output before saving.

What this does not do: it doesn't make a copy's numbers true (that's the signed report,
`check_consistency` and `compare`), and it doesn't protect against a bug inside SQLite
itself beyond the hardening above. Keep Python/SQLite up to date.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Any

RULE = "copy-open-v1"
SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_COPY_BYTES = 512 * 1024 * 1024  # a copy of a busy year is a few MB; this is far above
MAX_VALUE_BYTES = 1024 * 1024       # copies hold no prompts or answers; 1 MiB is plenty
QUERY_SECONDS = 300                 # any single connection, from open to close
PROGRESS_STEPS = 10_000             # SQLite VM steps between clock checks
INTEGRITY_ERRORS_SHOWN = 3

COPY_INFO_DDL = ("CREATE TABLE copy_info (format TEXT NOT NULL, until INTEGER NOT NULL, "
                 "created_at INTEGER NOT NULL, note TEXT NOT NULL)")
# SQLite creates this one itself for AUTOINCREMENT tables; its columns are fixed.
SQLITE_OWN_TABLES = {"sqlite_sequence"}


class UnsafeCopyError(sqlite3.DatabaseError):
    """The file is not a copy we are willing to read."""


def check_file(path: Path, max_bytes: float | None = None) -> int:
    """Cheap checks before SQLite sees the file. Returns its size in bytes. `max_bytes`
    defaults to MAX_COPY_BYTES (read at call time)."""
    path = Path(path)
    if max_bytes is None:
        max_bytes = MAX_COPY_BYTES
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        raise UnsafeCopyError(f"{path.name} is not a regular file")
    size = path.stat().st_size
    if size == 0:
        raise UnsafeCopyError(f"{path.name} is empty")
    if size > max_bytes:
        raise UnsafeCopyError(f"{path.name} is {size} bytes; a ledger copy may be at most "
                              f"{max_bytes} bytes")
    with path.open("rb") as fh:
        head = fh.read(len(SQLITE_MAGIC))
    if head != SQLITE_MAGIC:
        raise UnsafeCopyError(f"{path.name} is not a SQLite file")
    return size


def _harden(db: sqlite3.Connection, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, PROGRESS_STEPS)
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    db.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    if hasattr(db, "setconfig"):  # Python 3.12+
        db.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        db.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
        db.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_VIEW, False)
        db.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
    for pragma in ("trusted_schema=OFF", "cell_size_check=ON", "mmap_size=0",
                   "query_only=ON"):
        db.execute(f"PRAGMA {pragma}")


def connect(path: Path, seconds: float = QUERY_SECONDS) -> sqlite3.Connection:
    """A hardened, read-only, immutable connection. Does not vet the schema: call `vet`
    once first (or use `open_copy`)."""
    path = Path(path)
    check_file(path)
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True,
                         isolation_level=None)
    try:
        _harden(db, seconds)
    except BaseException:
        db.close()
        raise
    return db


@lru_cache(maxsize=1)
def expected_schema() -> dict[str, Any]:
    """The schema `ledger_copy export` produces, taken from a fresh ledger built by the
    same code (so it can't drift from `ledger.py`)."""
    from .ledger import Ledger  # ledger imports nothing from here; keep it lazy anyway

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reference.sqlite3"
        Ledger(path)
        with closing(sqlite3.connect(path)) as db:
            db.execute(COPY_INFO_DDL)
            return _schema_of(db)


def _schema_of(db: sqlite3.Connection) -> dict[str, Any]:
    tables: dict[str, list[tuple[str, str, int]]] = {}
    indexes: dict[str, tuple[str, tuple[str, ...], int, int]] = {}
    others: list[tuple[str, str]] = []
    virtual: list[str] = []
    for kind, name, table, sql in db.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"):
        if kind == "table":
            if (sql or "").lstrip().upper().startswith("CREATE VIRTUAL"):
                virtual.append(name)
                continue
            cols = db.execute("SELECT name, upper(type), pk FROM pragma_table_info(?)",
                              (name,)).fetchall()
            tables[name] = sorted((c[0], c[1], c[2]) for c in cols)
        elif kind == "index":
            cols = tuple(r[0] for r in db.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,)))
            flags = db.execute("SELECT \"unique\", partial FROM pragma_index_list(?) "
                               "WHERE name = ?", (table, name)).fetchone() or (0, 0)
            indexes[name] = (table, cols, int(flags[0]), int(flags[1]))
        else:
            others.append((kind, name))
    return {"tables": tables, "indexes": indexes, "others": others, "virtual": virtual}


def _schema_problems(found: dict[str, Any]) -> list[str]:
    want = expected_schema()
    problems = [f"has a {kind} named {name!r} (copies hold tables only)"
                for kind, name in found["others"]]
    problems += [f"has a virtual table {name!r}" for name in found["virtual"]]
    allowed = set(want["tables"]) | SQLITE_OWN_TABLES
    for name in sorted(set(found["tables"]) - allowed):
        problems.append(f"has an unexpected table {name!r}")
    for name in sorted(set(want["tables"]) - set(found["tables"])):
        problems.append(f"is missing the {name!r} table")
    for name in sorted(set(want["tables"]) & set(found["tables"])):
        if found["tables"][name] != want["tables"][name]:
            problems.append(f"table {name!r} has different columns from a ledger copy")
    for name, spec in sorted(found["indexes"].items()):
        table, cols, unique, partial = spec
        if name.startswith("sqlite_autoindex_"):
            if table not in want["tables"] or partial:
                problems.append(f"has an unexpected index {name!r}")
            continue
        if want["indexes"].get(name) != spec:
            problems.append(f"has an unexpected or changed index {name!r}")
    return problems


def vet(path: Path) -> dict[str, Any]:
    """Run every check in the module docstring. Returns the copy's `copy_info` row; raises
    UnsafeCopyError (or FileNotFoundError) otherwise."""
    path = Path(path)
    check_file(path)
    try:
        with closing(connect(path)) as db:
            problems = _schema_problems(_schema_of(db))
            if problems:
                raise UnsafeCopyError(f"{path.name} is not a plain ledger copy: "
                                      + "; ".join(problems[:5]))
            result = [r[0] for r in db.execute(
                f"PRAGMA integrity_check({INTEGRITY_ERRORS_SHOWN})")]
            if result != ["ok"]:
                raise UnsafeCopyError(f"{path.name} is damaged: " + "; ".join(result))
            # Read every row once: a value over MAX_VALUE_BYTES only fails when it is
            # loaded, and better here than halfway through a report. Names are ours.
            for table in sorted(expected_schema()["tables"]):
                for _ in db.execute(f'SELECT * FROM "{table}"'):
                    pass
            rows =db.execute("SELECT format, until, created_at, note FROM copy_info "
                              "LIMIT 2").fetchall()
    except UnsafeCopyError:
        raise
    except sqlite3.DatabaseError as exc:  # includes "interrupted" and "string or blob too big"
        raise UnsafeCopyError(f"{path.name} could not be read safely: {exc}") from None
    if len(rows) != 1:
        raise UnsafeCopyError(f"{path.name} must have exactly one copy_info row, "
                              f"has {'none' if not rows else 'more than one'}")
    fmt, until, created_at, note = rows[0]
    if not (isinstance(fmt, str) and isinstance(until, int) and isinstance(created_at, int)
            and isinstance(note, str)):
        raise UnsafeCopyError(f"{path.name} has a malformed copy_info row")
    return {"format": fmt, "until": until, "created_at": created_at, "note": note}


def has_copy_info(path: Path) -> bool:
    """True if `path` looks like a copy (has a copy_info table), without trusting it.
    A live ledger (even one mid-write in WAL mode) answers False. An empty file is an
    empty SQLite database, so False too."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_file() and path.stat().st_size == 0:
        return False
    # Size limits apply to copies only: a live ledger may grow past them.
    check_file(path, max_bytes=float("inf"))
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1",
                                     uri=True)) as db:
            _harden(db, 30)
            return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                              "name='copy_info'").fetchone() is not None
    except sqlite3.DatabaseError as exc:
        raise UnsafeCopyError(f"{path.name} could not be read safely: {exc}") from None
