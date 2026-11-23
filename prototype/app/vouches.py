"""Who agreed with a rig that was later caught (`vouch-check-v1`, threat model #3).

A colluding auditor confirms a friend's fake answer: both results become "confirmed" and
nothing looks wrong. Random auditor picks (0.5.8) make that rarer, but when it happens the
software can't see it. What *can* be seen later is that the friend got caught: it failed a
hidden known-answer test or was outvoted in a tie-breaker (a "strike", app/audit_policy.py).
From then on, every earlier agreement with that rig is worth a second look:

    leads                rigs whose re-run (or original) agreed with a caught rig's answer.
                         An honest rig shows up here too, because a cheater is often honest
                         on most jobs, so a lead is a reason to check, not evidence. Two
                         caught rigs that agreed with each other (`also_caught`) come first
    weak confirmations   "confirmed" results whose only independent agreement came from a
                         caught rig: the confirmation rests on the word of a rig that has
                         since been caught

The cheap, fair check for a lead is a hidden known-answer test aimed at it (`target_worker`
on `POST /v1/challenges`, or `python -m app.challenge_pool issue ... --worker <id>`): an honest
rig passes and costs nothing; a colluder has to answer honestly or get a strike of its own.
`/v1/operator/health` warns about leads that are not revoked, not caught themselves (the
strike warning already names those) and have never passed a hidden test, because those are
the ones nothing has checked independently.

What this is not:
  - Read-only. No points, statuses, tiers or auditor picks change. What should happen to
    weakly confirmed points is part of scoring v2 (owner decision).
  - Operator-only. It names which rigs audited which, so it lives behind `OPERATOR_TOKEN`
    (`GET /v1/operator/vouches`) or needs the database file (CLI). It contains no prompts,
    answers or expected answers.

CLI (opens the database read-only):
    python -m app.vouches [--db ./data/prototype.sqlite3] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .ledger import Ledger

VOUCH_RULE = "vouch-check-v1"
ROLE_BY_KIND = {"work": "original", "audit": "audit", "tiebreak": "tie-breaker"}


def _next_step(worker_id: str) -> str:
    return (f"Aim a hidden known-answer test at it: POST /v1/challenges with "
            f"target_worker={worker_id}, or `python -m app.challenge_pool issue --pool <file> "
            f"--url <coordinator> --count 1 --worker {worker_id}`. An honest rig passes; "
            f"if it fails it gets a strike of its own.")


def build_report(groups: list[dict[str, Any]], record: dict[str, dict[str, Any]],
                 now: int) -> dict[str, Any]:
    """Pure function over Ledger.agreement_groups() and Ledger.check_record()."""
    caught = {w for w, r in record.items() if r["strikes"] > 0}
    cases: list[dict[str, Any]] = []
    leads: dict[str, dict[str, Any]] = {}
    for group in groups:
        agreed = group["agreed"]
        if len({a["worker_id"] for a in agreed}) < 2:
            continue
        if not any(a["worker_id"] in caught for a in agreed):
            continue
        weak = []
        for a in agreed:
            partners = {b["worker_id"] for b in agreed if b["worker_id"] != a["worker_id"]}
            if partners and partners <= caught:
                weak.append(a)
            for p in sorted(partners & caught):
                lead = leads.setdefault(a["worker_id"], {"agreed_with_caught": 0,
                                                         "caught_partners": {}})
                lead["agreed_with_caught"] += 1
                lead["caught_partners"][p] = lead["caught_partners"].get(p, 0) + 1
        cases.append({
            "case_id": group["root"], "task_type": group["task_type"],
            "agreed": [{"role": ROLE_BY_KIND.get(a["job_kind"], a["job_kind"]),
                        "job_id": a["job_id"], "worker_id": a["worker_id"],
                        "owner": record.get(a["worker_id"], {}).get("owner"),
                        "caught": a["worker_id"] in caught,
                        "points": int(a["reward_points"] or 0),
                        "accepted_at": a["accepted_at"]} for a in agreed],
            "weak_confirmations": [a["job_id"] for a in weak],
            "weak_points": sum(int(a["reward_points"] or 0) for a in weak),
        })
    lead_rows = []
    for worker_id, lead in leads.items():
        r = record.get(worker_id, {})
        lead_rows.append({
            "worker_id": worker_id, "owner": r.get("owner"),
            "revoked": bool(r.get("revoked")),
            "also_caught": worker_id in caught,
            "agreed_with_caught": lead["agreed_with_caught"],
            "caught_partners": dict(sorted(lead["caught_partners"].items())),
            "hidden_tests_passed": int(r.get("hidden_tests_passed", 0)),
            "next_step": _next_step(worker_id),
        })
    lead_rows.sort(key=lambda x: (not x["also_caught"], -x["agreed_with_caught"],
                                  x["worker_id"]))
    caught_rows = [{"worker_id": w, "owner": record[w]["owner"],
                    "strikes": record[w]["strikes"],
                    "first_struck_result_at": record[w]["first_struck_result_at"],
                    "revoked": bool(record[w]["revoked"])} for w in sorted(caught)]
    return {
        "rule": VOUCH_RULE, "checked_at": now, "caught_rigs": caught_rows,
        "cases": cases, "leads": lead_rows,
        "weak_confirmations": {"results": sum(len(c["weak_confirmations"]) for c in cases),
                               "points": sum(c["weak_points"] for c in cases)},
        "unchecked_leads": [x["worker_id"] for x in lead_rows
                            if not x["revoked"] and not x["also_caught"]
                            and x["hidden_tests_passed"] == 0],
        "note": "Read-only. A lead agreed with a rig that was later caught; honest rigs "
                "show up too, so it is a reason to aim a hidden test, not proof.",
    }


def vouch_report(ledger: Ledger, now: int) -> dict[str, Any]:
    return build_report(ledger.agreement_groups(), ledger.check_record(), now)


def render_text(report: dict[str, Any]) -> str:
    caught = report["caught_rigs"]
    weak = report["weak_confirmations"]
    lines = [(f"Vouch check ({report['rule']}): {len(caught)} caught rig(s), "
              f"{len(report['leads'])} lead(s), {weak['results']} weak confirmation(s) "
              f"worth {weak['points']} simulated point(s).")]
    if not caught:
        lines.append("No rig has a strike, so there is nothing to follow up.")
    for c in caught:
        lines.append(f"  caught: {c['worker_id']} (owner {c['owner']}, strikes {c['strikes']}"
                     f"{', revoked' if c['revoked'] else ''})")
    for lead in report["leads"]:
        partners = ", ".join(f"{w} x{n}" for w, n in lead["caught_partners"].items())
        flags = [f for f, on in (("ALSO CAUGHT", lead["also_caught"]),
                                 ("revoked", lead["revoked"])) if on]
        lines += ["", (f"lead: {lead['worker_id']} (owner {lead['owner']}"
                       f"{', ' + ', '.join(flags) if flags else ''})"),
                  f"  agreed with caught rig(s): {partners}",
                  f"  hidden tests passed: {lead['hidden_tests_passed']}"]
        if not lead["revoked"]:
            lines.append(f"  next: {lead['next_step']}")
    lines += ["", report["note"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.vouches",
                                     description="List rigs that agreed with a rig that was "
                                                 "later caught. Read-only.")
    parser.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    if not args.db.exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    report = vouch_report(Ledger(args.db, read_only=True), int(time.time()))
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
