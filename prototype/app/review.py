"""Operator review queue (threat model #7): held points that need a person.

Some held results can't be settled by the software on its own:
    - a dispute whose tie-breaker never ran (often only two rigs were online), or for which
      no tie-breaker was scheduled (`TIEBREAK=false`);
    - an `unresolved` dispute: the tie-breaker agreed with neither side;
    - a dispute the tie-breaker answered but wasn't allowed to settle, because a rig on
      the winning side isn't `established` yet (`TIEBREAK_DECIDES=established-only`,
      `tiebreak-trust-v1`, threat model #6);
    - a failed hidden test job: maybe a cheat, maybe honest GPU nondeterminism (#13).
Until now these just sat in the "held" bucket and the operator had to dig through
`/v1/audits` and the events table to find them. This module gathers them into one list of
cases, each with every answer side by side, which answers agree under the task's own
tolerance rule, the rigs and owner labels involved, how many points are held, and since when.

Each case has a `stage`:
    needs_review          nothing in the software will settle it; a person should look
    waiting               a tie-breaker is queued or running; check back later
    decided_by_majority   two answers agreed against one. Listed for spot checks: two
                          colluding rigs can outvote an honest one (threat model #6)

What this is not:
  - Read-only. It never changes a status, a point or the audit trail. There is no
    "override" yet: what a person's decision should do to points is part of scoring v2,
    which is an owner decision. Today the tools are: compare the answers, look at the rigs'
    trust reports, and revoke a rig if needed.
  - Operator-only. It shows answers and the expected answers of hidden tests, so it lives
    behind `OPERATOR_TOKEN` (`GET /v1/operator/review`) or needs the database file (CLI).

CLI (needs the database file; opened read-only):
    python -m app.review [--db ./data/prototype.sqlite3] [--all] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import tasks
from .ledger import SETTLEMENT, Ledger
from .screen import say_lines

REVIEW_VERSION = "review-queue-v1"
STAGES = ("needs_review", "waiting", "decided_by_majority")
ROLE_BY_KIND = {"work": "original", "audit": "audit", "tiebreak": "tie-breaker",
                "challenge": "hidden-test answer"}
INPUT_PREVIEW_CHARS = 300

Agree = Callable[[str, str], bool]


def _agree_for(task_type: str) -> Agree:
    try:
        return tasks.get(task_type).results_agree
    except tasks.TaskError:  # a task type this build doesn't know: byte-exact
        return lambda a, b: a == b


def _mark_agreement(answers: list[dict[str, Any]], agree: Agree) -> None:
    for a in answers:
        a["agrees_with"] = [b["role"] for b in answers if b is not a
                            and a["result"] is not None and b["result"] is not None
                            and agree(a["result"], b["result"])]


def _answer(job: dict[str, Any]) -> dict[str, Any]:
    return {"role": ROLE_BY_KIND.get(job["job_kind"], job["job_kind"]), "job_id": job["job_id"],
            "worker_id": job["worker_id"], "owner": job["owner"],
            "audit_status": job["audit_status"], "result": job["result"]}


def _held_points(jobs: list[dict[str, Any]]) -> int:
    return sum(int(j["reward_points"] or 0) for j in jobs
               if j["audit_status"] and SETTLEMENT.get(j["audit_status"]) == "held")


def _dispute_case(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind = {j["job_kind"]: j for j in jobs}
    tiebreak = by_kind.get("tiebreak")
    statuses = {j["audit_status"] for j in jobs if j["audit_status"]}
    if "disputed" in statuses:
        if tiebreak is not None and tiebreak["status"] in ("queued", "leased"):
            stage, why = "waiting", "the audit disagreed; a tie-breaker is queued or running"
        elif tiebreak is not None and tiebreak["status"] == "accepted":
            stage, why = "needs_review", ("the tie-breaker answered, but a rig on the side it "
                                          "agreed with isn't established yet, so the vote "
                                          "didn't settle it (TIEBREAK_DECIDES=established-"
                                          "only). See who agrees with whom below")
        elif tiebreak is None:
            stage, why = "needs_review", ("the audit disagreed and no tie-breaker was "
                                          "scheduled (TIEBREAK may be off)")
        else:
            stage, why = "needs_review", (f"the audit disagreed and the tie-breaker ended "
                                          f"without a result ({tiebreak['status']}); often "
                                          "only two rigs were online")
    elif "unresolved" in statuses:
        stage, why = "needs_review", "three runs, three different answers"
    else:
        stage, why = "decided_by_majority", ("two answers agreed against one; the odd one "
                                             "out is held. Two colluding rigs can outvote "
                                             "an honest one (threat model #6)")
    answers = [_answer(j) for j in jobs if j["accepted_at"] is not None]
    audit = by_kind.get("audit")
    held_since = audit["accepted_at"] if audit and audit["accepted_at"] is not None else None
    return {"kind": "dispute", "stage": stage, "why": why, "answers": answers,
            "held_since": held_since}


def _challenge_case(job: dict[str, Any]) -> dict[str, Any]:
    answers = [{"role": "expected", "job_id": job["job_id"], "worker_id": None, "owner": None,
                "audit_status": None, "result": job["expected_result"]}, _answer(job)]
    return {"kind": "failed_hidden_test", "stage": "needs_review",
            "why": ("the rig's answer to a hidden test job didn't match the expected answer. "
                    "Could be cheating, a wrong model, or honest GPU differences (#13)"),
            "answers": answers, "held_since": job["accepted_at"]}


NEXT_STEP = {
    "needs_review": ("Compare the answers and look at each rig's trust report "
                     "(/v1/workers/<id>/trust). Revoke a rig if it looks wrong. Points stay "
                     "held: there is no override yet (scoring v2 is an owner decision)."),
    "waiting": "Nothing to do yet; check again after the tie-breaker's deadline.",
    "decided_by_majority": ("Optional spot check: if the two agreeing rigs share a person "
                            "behind different owner labels, the vote means less."),
}


def build_queue(groups: list[dict[str, Any]], now: int,
                strikes: dict[str, int] | None = None) -> dict[str, Any]:
    """Pure function over Ledger.review_groups(), so it is easy to test and re-run."""
    strikes = strikes or {}
    cases = []
    for group in groups:
        jobs = group["jobs"]
        root = next((j for j in jobs if j["job_id"] == group["root"]), None)
        if root is None:
            continue
        if root["job_kind"] == "challenge":
            case = _challenge_case(root)
        else:
            case = _dispute_case(jobs)
        _mark_agreement(case["answers"], _agree_for(root["task_type"]))
        for a in case["answers"]:
            if a["worker_id"] is not None:
                a["worker_strikes"] = int(strikes.get(a["worker_id"], 0))
        payload = root["payload"]
        held_since = case["held_since"]
        cases.append({
            "case_id": root["job_id"], **case, "task_type": root["task_type"],
            "input_preview": None if payload is None else payload[:INPUT_PREVIEW_CHARS],
            "points_held": _held_points(jobs),
            "age_seconds": None if held_since is None else max(0, now - int(held_since)),
            "needs_person": case["stage"] == "needs_review",
            "next_step": NEXT_STEP[case["stage"]],
        })
    cases.sort(key=lambda c: (STAGES.index(c["stage"]),
                              c["held_since"] if c["held_since"] is not None else now,
                              c["case_id"]))
    counts = {s: sum(1 for c in cases if c["stage"] == s) for s in STAGES}
    needing = [c for c in cases if c["needs_person"]]
    return {
        "review_version": REVIEW_VERSION,
        "checked_at": now,
        "counts": counts,
        "points_held": sum(c["points_held"] for c in cases),
        "oldest_needs_review_age_seconds": max((c["age_seconds"] or 0 for c in needing),
                                               default=None),
        "cases": cases,
        "note": "Read-only: nothing here changes points or statuses. Agreement between "
                "rigs is evidence, not proof.",
    }


def review_queue(ledger: Ledger, now: int) -> dict[str, Any]:
    strikes = {w: h["strikes"] for w, h in ledger.audit_history().items()}
    return build_queue(ledger.review_groups(), now, strikes)


def _short(text: str | None, limit: int = 160) -> str:
    """One line of at most `limit` characters. Answers come from rigs and inputs from the
    submitter, so anything a terminal could act on (ESC codes, invisible marks) is shown as
    `?` (`operator-screen-v1`); the JSON view and the API keep the exact text."""
    if text is None:
        return "(not available)"
    one_line = "".join(ch if ch.isprintable() else "?" for ch in " ".join(text.split()))
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def render_text(queue: dict[str, Any], show_all: bool = False) -> str:
    c = queue["counts"]
    lines = [f"Review queue ({queue['review_version']}): {c['needs_review']} need a person, "
             f"{c['waiting']} waiting for a tie-breaker, {c['decided_by_majority']} decided "
             f"by majority. {queue['points_held']} simulated point(s) held."]
    shown = [x for x in queue["cases"] if show_all or x["needs_person"]]
    if not shown:
        lines.append("Nothing needs a person right now." if not show_all else "No held cases.")
    for case in shown:
        age = case["age_seconds"]
        age_text = "" if age is None else f", held {age // 3600}h {age % 3600 // 60}m"
        lines += ["", f"[{case['stage']}] {case['case_id']} ({case['task_type']}, "
                      f"{case['points_held']} point(s){age_text})",
                  f"  why: {case['why']}",
                  f"  input: {_short(case['input_preview'])}"]
        for a in case["answers"]:
            who = "operator" if a["worker_id"] is None else (
                f"{a['worker_id']} (owner {a['owner']}, strikes {a.get('worker_strikes', 0)})")
            agrees = ", ".join(a["agrees_with"]) or "nobody"
            lines.append(f"  - {a['role']} by {who}: {_short(a['result'])}  "
                         f"[agrees with: {agrees}]")
        lines.append(f"  next: {case['next_step']}")
    lines += ["", queue["note"]]
    return say_lines(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.review",
                                     description="List held results that need a person. "
                                                 "Read-only.")
    parser.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    parser.add_argument("--all", action="store_true",
                        help="also list cases waiting for a tie-breaker or decided by majority")
    parser.add_argument("--json", action="store_true", help="print the full queue as JSON")
    args = parser.parse_args(argv)
    if not args.db.exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    queue = review_queue(Ledger(args.db, read_only=True), int(time.time()))
    if args.json:
        sys.stdout.write(json.dumps(queue, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(queue, show_all=args.all))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
