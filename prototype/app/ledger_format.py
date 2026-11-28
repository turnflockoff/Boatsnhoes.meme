"""Which coordinator version may open the ledger (`ledger-format-v1`, code audit, 17th pass).

The question this pass asked: what happens when the ledger and the code don't come from the
same version? That happens on every upgrade, and on every *rollback* ("the new version has
a bug, put the old one back"), which is the first thing anyone does after a bad deploy.

Shown on this sandbox (docs/BUILD-LOG.md, 0.5.68), with a ledger written by today's code:

  * **Older code ignored newer rules, silently.** The 0.5.20 coordinator opened it without a
    word and handed a hidden test aimed at rig A to rig B, and both 0.5.20 and 0.5.25
    accepted new work during an incident pause. The 0.5.65 coordinator doesn't take the
    "in use" lock (`backup-restore-v1`), so a restore ran while it was still running. Each
    old version just doesn't know the newer column, event or lock exists.
  * **Today's code did the same to a newer ledger.** A ledger with an extra rule from a
    future version (a column today's code has never heard of) opened as if nothing were
    there. Nothing in the file said which version wrote it.
  * **Two programs upgrading at once crashed.** Opening an old (Phase 1) ledger with two
    processes at the same moment (the coordinator and an operator tool after an upgrade):
    7 of 80 opens stopped with "duplicate column name". Each looked for missing columns, then
    both added the same one. The upgrade wasn't one step either: stopped halfway, it left a
    ledger with some new columns and not others.

The rule, from this version on:

  1. **The ledger says which format it is.** `FORMAT` is stored in the file's header
     (SQLite's `PRAGMA user_version`; 0 means "from before this rule"). It goes up whenever
     the tables, columns or indexes change, or a stored value starts to mean something new.
  2. **Code refuses a ledger newer than itself.** Coordinator, operator tools, backups and
     copy checkers all stop with `LedgerTooNew` and a plain message instead of running with
     rules missing. To really go back, restore a backup made before the upgrade.
  3. **The upgrade is one step.** Adding tables, columns and indexes and writing the new
     format number happen in one transaction that holds the write lock: a second program
     waits, then finds nothing left to do; a crash leaves the old ledger as it was.
  4. **The upgrade is written down.** A `ledger_format_upgraded` event (from, to, which rule)
     goes into the audit trail. Its time is the newest time already in the ledger, so an
     upgrade never moves the ledger's clock (`ledger-clock-v1`); the server's clock reading
     is in the detail for people.
  5. **The layout is frozen per format.** `FINGERPRINTS` holds a hash of every table's
     columns and every index for each format, and a test compares a fresh ledger with it.
     Changing the layout without raising `FORMAT` fails that test, so the number can't be
     forgotten.

What it can't do: versions from before this rule (0.5.67 and older) don't read the number,
so they would still open a newer ledger. No coordinator has run for real yet, so no real
ledger was ever made by one of them: don't start an older version on a ledger.
`python -m app.ledger_format check --db <ledger>` tells you a ledger's format, read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

RULE = "ledger-format-v1"
FORMAT = 1
UPGRADE_EVENT = "ledger_format_upgraded"

# sha256 of `layout()` for each format. A fresh ledger and an upgraded old one must both
# match the current entry (tests/test_ledger_format.py).
FINGERPRINTS = {
    1: "5db5468174fcbf47ca91e2af2f862634017bb3e2cfd0086ac9094187616122c1",
}


class LedgerTooNew(RuntimeError):
    """The ledger was written by a newer coordinator than this code."""

    def __init__(self, found: int, what: str = "the ledger") -> None:
        self.found = found
        super().__init__(
            f"{what} is ledger format {found}, written by a newer coordinator; this code only "
            f"understands format {FORMAT} or older. Running it would quietly skip the newer "
            "version's rules (a revoked rig, an aimed hidden test, a pause it doesn't know "
            "about). Run the newer version, or to really go back, restore a backup made "
            "before the upgrade (python -m app.backup restore); work since then is lost.")


def read(db: sqlite3.Connection) -> int:
    """The format number in the ledger's header (0 = from before `ledger-format-v1`)."""
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def refuse_if_newer(found: int, what: str = "the ledger") -> None:
    if found > FORMAT:
        raise LedgerTooNew(found, what)


def layout(db: sqlite3.Connection) -> list[Any]:
    """Every table's columns (name, type, not-null, default, key) and every named index's
    table, columns and uniqueness, sorted so that a ledger upgraded column by column and
    a fresh one compare equal. SQLite's own tables are left out."""
    out: list[Any] = []
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name").fetchall()]
    for t in tables:
        cols = sorted([r[1], r[2].upper(), int(r[3]), r[4], int(r[5])]
                      for r in db.execute(f"PRAGMA table_info({t})").fetchall())
        out.append(["table", t, cols])
    for name, table in db.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
            "ORDER BY name").fetchall():
        unique = any(r[1] == name and r[2] for r in
                     db.execute(f"PRAGMA index_list({table})").fetchall())
        cols = [r[2] for r in db.execute(f"PRAGMA index_info({name})").fetchall()]
        out.append(["index", name, table, cols, bool(unique)])
    return out


def fingerprint(db: sqlite3.Connection) -> str:
    return hashlib.sha256(json.dumps(layout(db), separators=(",", ":")).encode()).hexdigest()


def status(path: Path) -> dict[str, Any]:
    """Read-only: the ledger's format, whether this code may open it, and its upgrades."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                                 timeout=10)) as db:
        found = read(db)
        has_events = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                                "name='events'").fetchone() is not None
        upgrades = [json.loads(r[0] or "{}") for r in db.execute(
            "SELECT detail FROM events WHERE event=? ORDER BY id", (UPGRADE_EVENT,))
            .fetchall()] if has_events else []
        fp = fingerprint(db)
    return {"rule": RULE, "ledger_format": found, "this_code_understands": FORMAT,
            "can_open": found <= FORMAT,
            "will_upgrade": found < FORMAT,
            "layout_matches": found == FORMAT and fp == FINGERPRINTS.get(FORMAT),
            "upgrades": upgrades}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ledger_format",
        description="Which ledger format a file is, and whether this code may open it "
                    "(read-only; changes nothing).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ck = sub.add_parser("check", help="show the ledger's format (read-only)")
    ck.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    args = parser.parse_args(argv)
    try:
        info = status(args.db)
    except FileNotFoundError:
        print(f"error: no ledger at {args.db}", file=sys.stderr)
        return 2
    except sqlite3.DatabaseError as exc:
        print(f"error: not a readable ledger: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(info, indent=2, sort_keys=True))
    if not info["can_open"]:
        print(str(LedgerTooNew(info["ledger_format"])), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
