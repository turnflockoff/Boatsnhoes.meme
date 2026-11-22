"""Phase 5: simulated reward reports (off-chain points only).

A report turns the ledger into per-worker point totals for a time period, using a
versioned, deterministic scoring rule. The same ledger and the same period always give
the same report and the same `report_sha256`, so anyone with a copy of the database can
re-run it and check the numbers (`python -m app.rewards verify`).

Nothing here mints, sends or promises anything. Points are simulation bookkeeping.

Scoring rule `sim-points-v1` (integer arithmetic only):
    Every accepted result is in one public settlement bucket (see ledger.SETTLEMENT):
      confirmed   checked exactly, or an independent re-run agreed      -> payable
      unverified  shape-checked only, not picked for a re-run (final)    -> payable
      pending     a hidden re-run is still queued                        -> not yet
      held        disputed, outvoted, unresolved, or failed a hidden test -> not payable
    payable_points = confirmed + unverified.
    Unverified work counts because spot-checking only works if unsampled honest work
    is paid; the deterrent is that sampled fakes end up held. v1 has no extra penalty
    for a worker caught cheating (open decision, see the threat model).

Statuses are read as they are when the report is built. A pending re-run that settles
later changes the report for the same period, and `verify` will say so.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .ledger import SETTLEMENT, Ledger
from .security import canonical_json, sha256_text

REPORT_TYPE = "bnh-simulated-reward-report"
NOTICE = ("Simulated off-chain points only. Not tokens, not money, and not a promise of "
          "any future payout.")
BUCKETS = ("confirmed", "unverified", "pending", "held")
ACTIVITY = ("leases", "rejected", "lease_timeouts", "bad_submissions")

SCORING_RULES: dict[str, dict[str, Any]] = {
    "sim-points-v1": {
        "version": "sim-points-v1",
        "payable_buckets": ["confirmed", "unverified"],
        "not_payable_buckets": ["pending", "held"],
        "rule": "payable_points = sum(reward_points) of results in payable buckets",
        "penalties": "none",
    },
}
CURRENT_SCORING = "sim-points-v1"


class ReportError(ValueError):
    pass


def report_hash(report: dict[str, Any]) -> str:
    body = {k: v for k, v in report.items() if k != "report_sha256"}
    return sha256_text(canonical_json(body).decode("utf-8"))


def build_report(ledger: Ledger, since: int, until: int,
                 scoring: str = CURRENT_SCORING) -> dict[str, Any]:
    """Build the report for results accepted in `since <= accepted_at < until`."""
    if scoring not in SCORING_RULES:
        raise ReportError(f"unknown scoring version {scoring!r}")
    if since < 0 or until <= since:
        raise ReportError("period must satisfy 0 <= since < until")
    rule = SCORING_RULES[scoring]
    payable = set(rule["payable_buckets"])

    entries = []
    workers: dict[str, dict[str, Any]] = {}

    def worker(worker_id: str) -> dict[str, Any]:
        return workers.setdefault(worker_id, {
            "worker_id": worker_id, "accepted_results": 0,
            "points": dict.fromkeys(BUCKETS, 0), "payable_points": 0,
            "activity": dict.fromkeys(ACTIVITY, 0)})

    for row in ledger.reward_rows(since, until):
        # Unknown statuses fail safe into "held", matching the public ledger.
        bucket = SETTLEMENT.get(row["audit_status"], "held")
        entries.append({
            "job_id": row["job_id"], "worker_id": row["worker_id"],
            "task_type": row["task_type"], "accepted_at": row["accepted_at"],
            "reward_points": row["reward_points"], "settlement": bucket,
            "verifier_method": row["verifier_method"], "confidence": row["confidence"]})
        w = worker(row["worker_id"])
        w["accepted_results"] += 1
        w["points"][bucket] += row["reward_points"]
        if bucket in payable:
            w["payable_points"] += row["reward_points"]

    for worker_id, counts in ledger.activity_counts(since, until).items():
        w = worker(worker_id)
        for key, n in counts.items():
            w["activity"][key] += n

    rows = [workers[k] for k in sorted(workers)]
    totals = {"accepted_results": len(entries),
              "points": {b: sum(e["reward_points"] for e in entries if e["settlement"] == b)
                         for b in BUCKETS}}
    totals["payable_points"] = sum(totals["points"][b] for b in rule["payable_buckets"])
    balanced = (sum(w["payable_points"] for w in rows) == totals["payable_points"]
                and all(sum(w["points"][b] for w in rows) == totals["points"][b]
                        for b in BUCKETS))
    problems = ledger.check_consistency()
    report = {
        "report_type": REPORT_TYPE,
        "scoring": rule,
        "period": {"since": since, "until": until},
        "notice": NOTICE,
        "workers": rows,
        "totals": totals,
        "entries": entries,
        "checks": {"consistency_problems": problems, "worker_sums_match_totals": balanced,
                   "ok": balanced and not problems},
    }
    report["report_sha256"] = report_hash(report)
    return report


def verify_report(report: dict[str, Any], ledger: Ledger) -> list[str]:
    """Return a list of problems; empty means the report matches the ledger exactly."""
    problems = []
    if report.get("report_type") != REPORT_TYPE:
        return ["not a reward report"]
    if report.get("report_sha256") != report_hash(report):
        problems.append("report_sha256 does not match the report body (file was edited)")
    try:
        period = report["period"]
        fresh = build_report(ledger, int(period["since"]), int(period["until"]),
                             report["scoring"]["version"])
    except (KeyError, TypeError, ValueError) as exc:
        return problems + [f"cannot rebuild report: {exc}"]
    if fresh["report_sha256"] != report.get("report_sha256"):
        problems.append("ledger gives a different report for this period")
        old = {e["job_id"]: e for e in report.get("entries", [])}
        new = {e["job_id"]: e for e in fresh["entries"]}
        for job_id in sorted(old.keys() | new.keys()):
            if old.get(job_id) != new.get(job_id):
                problems.append(f"entry differs: {job_id}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.rewards",
                                     description="Simulated reward reports (points only).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rep = sub.add_parser("report", help="build a report from the ledger database")
    rep.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    rep.add_argument("--since", type=int, default=0, help="unix seconds, inclusive")
    rep.add_argument("--until", type=int, default=None, help="unix seconds, exclusive")
    rep.add_argument("--out", type=Path, default=None, help="write JSON here (else stdout)")
    ver = sub.add_parser("verify", help="re-run a saved report against the ledger")
    ver.add_argument("report", type=Path)
    ver.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    ledger = Ledger(args.db)
    if args.cmd == "report":
        until = args.until if args.until is not None else int(time.time()) + 1
        try:
            report = build_report(ledger, args.since, until)
        except ReportError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.out:
            args.out.write_text(text)
            print(f"wrote {args.out} sha256={report['report_sha256']}")
        else:
            sys.stdout.write(text)
        return 0 if report["checks"]["ok"] else 1
    problems = verify_report(json.loads(args.report.read_text()), ledger)
    for p in problems:
        print(p)
    print("OK: report matches the ledger" if not problems else "MISMATCH")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
