"""Which ledger file is the live one (`ledger-role-v1`, code audit twenty-second pass).

The question for this pass: **what happens when a tool is pointed at a backup instead of
the live ledger?** Several tools write a record into the ledger they are given, and the
coordinator's safety rules read those records back later:

  * `ledger_copy export` writes "a copy of this period was shared" (`ledger_copy_made`);
    from then on the coordinator refuses to reuse that period's hidden tests, because
    every rig remembers the inputs it ran (`test-reuse-v2`, threat model #11);
  * `rewards report --sign` writes "this period was signed" (`reward_report_signed`), so
    nothing is ever filed into a signed period afterwards (`ledger-clock-v1`);
  * `workers revoke`, `incident pause` change who may work and whether work is handed out.

Shown on the old code (docs/BUILD-LOG.md 0.5.73; tests/test_ledger_role.py replays each):
exporting a copy from a backup wrote the "shared" record into the *backup*, so the live
coordinator still accepted the same hidden test again (`200`, not `409`); signing from a
backup left the live ledger free to file results into the signed period; and a backup
copied into place by hand (`cp backup.sqlite3 data/prototype.sqlite3`) started without a
word, letting a rig revoked after the backup log in again: everything `backup restore`
carries forward was skipped. The module docstring of `ledger_copy` already said "always
export from the live ledger, not from a backup", but nothing checked it.

The rule: a file's role sits in SQLite's own header (`PRAGMA application_id`, 4 bytes at
offset 68, which no table, query or layout check sees):

    0        the live ledger (every ledger made before this rule is 0 too)
    BACKUP   made by `python -m app.backup make`
    REPLACED the ledger a restore moved aside (`<ledger>.replaced-<time>`)

Opening a ledger for writing (the coordinator and every tool that records something)
refuses any role but live, before a single byte changes, and says what to do instead.
Reading is still allowed (`backup check`, a report without `--sign`, `review`), and
`backup restore` is the one way to turn a backup back into the live ledger: it clears
the mark on the restored file and marks the file it moved aside.

Honest limits: a file is only marked by these tools. A backup made with `cp` or another
program (after stopping the coordinator) carries the live mark, and so does every backup
made before this version; a hand-made copy of the live ledger looks live. And the mark
says what a file *is*, not that it is the *only* live ledger: two live ledgers on two
machines can't be told apart from inside.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

RULE = "ledger-role-v1"

LIVE = 0
BACKUP = int.from_bytes(b"BNHB", "big")    # 0x424E4842
REPLACED = int.from_bytes(b"BNHR", "big")  # 0x424E4852
NAMES = {LIVE: "live", BACKUP: "backup", REPLACED: "replaced"}

HEADER_OFFSET = 68  # SQLite file format: "Application ID", 4-byte big-endian integer
SQLITE_MAGIC = b"SQLite format 3\x00"


class NotLiveLedger(RuntimeError):
    """Refused: the file is a backup, a replaced ledger, or not a $BNH ledger at all."""

    def __init__(self, role: int, what: str = "this file") -> None:
        self.role = role
        if role == BACKUP:
            msg = (f"{what} is a backup made by `python -m app.backup make`, not the live "
                   "ledger. Anything written here (a shared copy, a signed report, a "
                   "revocation) would never reach the coordinator. Point the tool at the "
                   "live ledger; to put a backup back, use `python -m app.backup restore` "
                   "(a plain copy over the ledger skips what the restore carries forward)")
        elif role == REPLACED:
            msg = (f"{what} is an old ledger that `python -m app.backup restore` moved aside, "
                   "not the live ledger. It is kept to look at. Point the tool at the live "
                   "ledger; to go back to this one, use `python -m app.backup restore` "
                   "with it as the backup")
        else:
            msg = (f"{what} is not a $BNH ledger: its header belongs to another program "
                   f"(application id {role})")
        super().__init__(msg)


def name(role: int) -> str:
    return NAMES.get(role, f"unknown ({role})")


def of_db(db: sqlite3.Connection) -> int:
    """The role in an open database's header."""
    return int(db.execute("PRAGMA application_id").fetchone()[0])


def of_file(path: Path) -> int | None:
    """The role in a file's header, read as plain bytes: no SQLite, no lock, no side
    files, nothing changed. None if the file is missing, empty or not a SQLite file (a
    brand-new ledger is created live). Accurate for a live ledger in WAL mode too: the
    mark is only ever set or cleared on files in rollback-journal mode (backups, the file
    a restore moved aside, a restore being built), where the header is always on disk."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEADER_OFFSET + 4)
    except (FileNotFoundError, IsADirectoryError):
        return None
    if len(head) < HEADER_OFFSET + 4 or not head.startswith(SQLITE_MAGIC):
        return None
    return int.from_bytes(head[HEADER_OFFSET:HEADER_OFFSET + 4], "big", signed=True)


def refuse_unless_live(role: int | None, what: str = "this file") -> None:
    if role not in (None, LIVE):
        raise NotLiveLedger(role, what)


def is_ours(role: int | None) -> bool:
    return role in (None, LIVE, BACKUP, REPLACED)


def mark(db: sqlite3.Connection, role: int) -> None:
    """Set the role of an open database. Callers use it only on a file in rollback-journal
    mode that nothing else has open (see `of_file`)."""
    if role not in NAMES:
        raise ValueError(f"unknown ledger role {role!r}")
    db.execute(f"PRAGMA application_id={int(role)}")
