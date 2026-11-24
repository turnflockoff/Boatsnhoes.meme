"""Worker enrollment and revocation for the operator.

How a rig joins:
    1. The operator makes a one-time enrollment code tied to an `owner` label
       (the person or group running the rig): `POST /v1/enrollments`, or this CLI.
    2. The rig sends the code once to `POST /v1/workers/register` and gets back its own
       worker key. The coordinator stores only sha256 hashes of codes and keys.
    3. The rig sends `Authorization: Bearer <key>` on every claim and submit. A key only
       works for its own worker id, so one rig can't claim or submit as another.

Workers with the same owner never audit or break ties on each other's results. Give each
real person their own owner label. For a test where your own two RX 470s should check each
other, enroll them under two labels (for example `owner-gpu0` and `owner-gpu1`) and
remember that agreement between them proves less.

CLI (needs the database file, not the API):
    python -m app.workers enroll --owner alice [--ttl 86400] [--db ./data/prototype.sqlite3]
    python -m app.workers revoke <worker_id>
    python -m app.workers list
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys
import time
from pathlib import Path

from .ledger import CoordinatorPaused, Ledger
from .security import sha256_text
from .screen import say

OWNER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
MIN_TTL, MAX_TTL = 60, 7 * 86400


def new_enrollment_code() -> str:
    return "bnhe_" + secrets.token_urlsafe(32)


def new_worker_key() -> str:
    return "bnhw_" + secrets.token_urlsafe(32)


def issue_code(ledger: Ledger, owner: str, ttl_seconds: int, now: int) -> tuple[str, int]:
    """Create a one-time enrollment code. Returns (code, expires_at). The code itself is
    never stored, so this is the only time anyone sees it."""
    if not OWNER_PATTERN.match(owner):
        raise ValueError("owner must be 1-64 letters, digits, dot, dash or underscore")
    if not MIN_TTL <= ttl_seconds <= MAX_TTL:
        raise ValueError(f"ttl must be between {MIN_TTL} and {MAX_TTL} seconds")
    code = new_enrollment_code()
    ledger.create_enrollment(sha256_text(code), owner, now, now + ttl_seconds)
    return code, now + ttl_seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.workers",
                                     description="Enroll, revoke and list workers.")
    parser.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    enr = sub.add_parser("enroll", help="make a one-time enrollment code")
    enr.add_argument("--owner", required=True, help="who runs the rig (one label per person)")
    enr.add_argument("--ttl", type=int, default=86400, help="seconds until the code expires")
    rev = sub.add_parser("revoke", help="block a worker's key")
    rev.add_argument("worker_id")
    sub.add_parser("list", help="list workers (never shows keys)")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    ledger = Ledger(args.db)
    now = int(time.time())
    if args.cmd == "enroll":
        try:
            code, expires_at = issue_code(ledger, args.owner, args.ttl, now)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except CoordinatorPaused as exc:
            print(f"coordinator is paused (since {exc.since}); no new rigs until "
                  "`python -m app.incident resume`", file=sys.stderr)
            return 3
        print(code)
        print(f"owner={args.owner} expires_at={expires_at} (single use; share it privately)",
              file=sys.stderr)
        return 0
    if args.cmd == "revoke":
        if not ledger.has_worker(args.worker_id):
            print(f"unknown worker: {args.worker_id}", file=sys.stderr)
            return 1
        changed = ledger.revoke_worker(args.worker_id, now)
        print("revoked" if changed else "already revoked")
        return 0
    for w in ledger.list_workers():
        state = "revoked" if w["revoked_at"] is not None else (
            "active" if w["has_key"] else "no key (re-enroll)")
        # Names are checked on the way in since 0.5.52, but an older ledger can hold
        # anything, so they are printed safely (`operator-screen-v1`).
        print(f"{say(w['worker_id'])}\towner={say(w['owner'] or '-')}\t{state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
