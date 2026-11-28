"""Emergency pause for incidents (`pause-v1`): stop the coordinator handing out work, fast.

If something looks wrong (a leaked worker key, a suspected stolen signing key, a bug paying
the wrong points, a rig flooding bad answers) the first move is usually "stop new work
while I look". Killing the server does that, but it also stops in-flight results, the
health page and revocation, and the next restart quietly starts handing out work again.

Pausing instead:
  - no job is leased (workers get `503` with `Retry-After: 60`; they still count as seen,
    so they stay eligible for audit picks after the pause);
  - no job or hidden test is created (operator included) and no enrollment code is issued
    or used;
  - no audit or tie-breaker is assigned, so the pause never counts against a rig as "not
    picked up"; assignments the pause interrupted get a fresh window on resume;
  - results for jobs leased before the pause ARE still accepted (otherwise honest work just
    times out), and revoking a worker still works;
  - `/health` says `"status": "paused"` (still HTTP 200), `/v1/operator/health` shows the
    reason and a warning;
  - the state is an event in the database, so it survives a restart and is in the audit
    trail (shared ledger copies keep that a pause happened, never the reason).

Easy to stop, harder to restart: `POST /v1/operator/pause` (operator token) or this CLI can
pause; only this CLI, run on the machine that holds the database, can resume. A leaked
operator token can stop the coordinator, never restart it.

CLI (works while the coordinator is running; SQLite handles the locking):
    python -m app.incident status [--db ./data/prototype.sqlite3] [--json]
    python -m app.incident pause  --reason "worker key for rig-b may have leaked"
    python -m app.incident resume --note "rig-b revoked, keys rotated, logs checked"

What it doesn't do: it doesn't stop a worker running a job it already has, revoke anyone,
rotate keys, or take the site down. See docs/INCIDENT-RESPONSE.md for the full plan.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import ledger_clock, ledger_role
from .config import _int_env
from .ledger import PAUSE_VERSION, Ledger
from .screen import say_lines

DEFAULT_DB = Path("./data/prototype.sqlite3")


def status(ledger: Ledger, now: int) -> dict[str, Any]:
    pause = ledger.pause_state()
    return {"rule": PAUSE_VERSION, "paused": pause is not None,
            "paused_for_seconds": now - pause["since"] if pause else None,
            **({"since": pause["since"], "reason": pause["reason"], "by": pause["by"]}
               if pause else {}),
            "history": ledger.pause_history()}


def _render(state: dict[str, Any]) -> str:
    if not state["paused"]:
        lines = ["Coordinator is RUNNING (not paused)."]
    else:
        lines = [f"Coordinator is PAUSED since {state['since']} "
                 f"({state['paused_for_seconds']} s, by {state['by']}).",
                 f"Reason: {state['reason']}",
                 "Resume with: python -m app.incident resume --note '<what you checked/fixed>'"]
    past = [h for h in state["history"] if h["event"] == "coordinator_resumed"]
    if past:
        lines.append(f"Past pauses: {len(past)}; last resumed at {past[-1]['at']}.")
    return say_lines(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.incident",
                                     description="Pause or resume the coordinator during an "
                                                 "incident.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="is the coordinator paused? (read-only)")
    st.add_argument("--json", action="store_true")
    pa = sub.add_parser("pause", help="stop handing out and creating work, now")
    pa.add_argument("--reason", required=True,
                    help="short note for the audit trail (operator-only, never public)")
    re_ = sub.add_parser("resume", help="hand out work again")
    re_.add_argument("--note", required=True,
                     help="what you checked or fixed (kept in the audit trail)")
    args = parser.parse_args(argv)

    if not args.db.exists():
        # Never create an empty database here: a typo in --db would "pause" nothing.
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    now = int(time.time())
    if args.cmd == "status":
        state = status(Ledger(args.db, read_only=True), now)
        if args.json:
            sys.stdout.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write(_render(state))
        return 0

    try:
        assign_seconds = _int_env("AUDIT_ASSIGN_SECONDS", 120, 5, 86400)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        ledger = Ledger(args.db, assign_seconds=assign_seconds)
    except ledger_role.NotLiveLedger as exc:  # `ledger-role-v1`: pausing a backup does nothing
        print(str(exc), file=sys.stderr)
        return 2
    try:
        # `ledger-clock-v1`: never earlier than the newest time already in the ledger.
        now = ledger_clock.tool_now(ledger.latest_time())
    except (ledger_clock.ClockBehindError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        if args.cmd == "pause":
            newly, since = ledger.pause(now, args.reason, by="cli")
            if newly:
                print(f"PAUSED at {since}. No jobs handed out or created, no new rigs. "
                      "Results already in progress are still accepted; revoking still works.")
            else:
                print(f"Already paused since {since}; kept the original time and reason.")
            return 0
        if not args.note.strip():
            print("--note can't be empty: say what you checked or fixed", file=sys.stderr)
            return 2
        done = ledger.resume(now, args.note, by="cli")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if done is None:
        print("Not paused; nothing to resume.")
        return 1
    print(f"RESUMED after {done['paused_seconds']} s. "
          f"{done['assignments_extended']} audit/test assignment(s) got a fresh window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
