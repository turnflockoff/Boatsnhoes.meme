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

Scoring rule `sim-points-v2-forfeit` (opt-in, threat model #15; `REWARD_SCORING` or
`report --scoring`). Same buckets, plus one penalty:
    A worker is `struck` for the period if any of its results accepted in the period is a
    strike (audit_policy.STRIKE_STATUSES: failed a hidden test, or outvoted by a
    tie-breaker). A struck worker's `unverified` points for that period are forfeited:
      payable_points = confirmed            (struck)
      payable_points = confirmed + unverified (not struck)
    Why: under v1 a cheater keeps the points for every fake nobody re-ran, so faking pays
    as long as the audit rate is below 100%. Forfeiting the unchecked rest of the period
    makes one caught fake cost all of that period's unchecked work. Work that passed an
    independent check (`confirmed`) is never forfeited. Held cases with no strike
    (`disputed`, `unresolved`, a tie-breaker held for a person) forfeit nothing.
    The report shows only `struck` and `forfeited_points` per worker, never which result
    caused the strike or whether it was a hidden test.

Statuses are read as they are when the report is built. A pending re-run that settles
later changes the report for the same period, and `verify` will say so.

Signed reports (threat model #10): `--sign` has the coordinator sign a closed period's
report with its Ed25519 key (the same key that signs job manifests, with a separate
`purpose` field so a report signature can never pass as a job signature). Anyone with the
JSON file and the coordinator's public key can then check, without the database, that the
coordinator issued it and nobody edited it: `python -m app.rewards verify r.json --no-db
--public-key <hex>`. A signature proves who issued the numbers, not that they are right;
only a re-run against the ledger (`verify --db`) checks the numbers.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from . import json_file, ledger_clock
from .audit_policy import STRIKE_STATUSES
from .ledger import SETTLEMENT, Ledger
from .security import canonical_json, sha256_text
from .json_file import MAX_REPORT_BYTES, UnsafeJSONError
from .signing import ManifestSigner, SigningKeyError, fingerprint, parse_public_key, verify_manifest
from .worker_guard import say

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
    "sim-points-v2-forfeit": {
        "version": "sim-points-v2-forfeit",
        "payable_buckets": ["confirmed", "unverified"],
        "not_payable_buckets": ["pending", "held"],
        "rule": ("payable_points = sum(reward_points) of results in payable buckets, "
                 "minus forfeited_points"),
        "penalties": ("a worker with a strike (failed hidden test or outvoted) among its "
                      "results accepted in the period forfeits its unverified points for "
                      "that period; confirmed points are kept"),
        "forfeit_on_strike": ["unverified"],
    },
}
CURRENT_SCORING = "sim-points-v1"  # the default until the owner picks a v2 rule
SIGNATURE_PURPOSE = "bnh-reward-report-signature/1"
UNHASHED = ("report_sha256", "signature")  # fields not covered by report_sha256


class ReportError(ValueError):
    pass


def report_hash(report: dict[str, Any]) -> str:
    body = {k: v for k, v in report.items() if k not in UNHASHED}
    return sha256_text(canonical_json(body).decode("utf-8"))


def build_report(ledger: Ledger, since: int, until: int,
                 scoring: str = CURRENT_SCORING) -> dict[str, Any]:
    """Build the report for results accepted in `since <= accepted_at < until`."""
    if scoring not in SCORING_RULES:
        raise ReportError(f"unknown scoring version {scoring!r}")
    if since < 0 or until <= since:
        raise ReportError("period must satisfy 0 <= since < until")
    limit = ledger.copy_until()
    if limit is not None and until > limit:
        raise ReportError(f"this ledger copy only covers up to {limit}; "
                          "a report past that point would be missing data")
    rule = SCORING_RULES[scoring]
    payable = set(rule["payable_buckets"])

    entries = []
    workers: dict[str, dict[str, Any]] = {}
    struck: set[str] = set()

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

        if row["audit_status"] in STRIKE_STATUSES:
            struck.add(row["worker_id"])

    for worker_id, counts in ledger.activity_counts(since, until).items():
        w = worker(worker_id)
        for key, n in counts.items():
            w["activity"][key] += n

    forfeit = rule.get("forfeit_on_strike")
    if forfeit is not None:  # v1 reports keep their exact shape (and hashes)
        for worker_id, w in workers.items():
            lost = sum(w["points"][b] for b in forfeit) if worker_id in struck else 0
            w["struck"] = worker_id in struck
            w["forfeited_points"] = lost
            w["payable_points"] -= lost

    rows = [workers[k] for k in sorted(workers)]
    totals = {"accepted_results": len(entries),
              "points": {b: sum(e["reward_points"] for e in entries if e["settlement"] == b)
                         for b in BUCKETS}}
    totals["payable_points"] = sum(totals["points"][b] for b in rule["payable_buckets"])
    if forfeit is not None:
        totals["forfeited_points"] = sum(w["forfeited_points"] for w in rows)
        totals["payable_points"] -= totals["forfeited_points"]
    balanced = (sum(w["payable_points"] for w in rows) == totals["payable_points"]
                and all(sum(w["points"][b] for w in rows) == totals["points"][b]
                        for b in BUCKETS)
                and all(w["payable_points"] >= 0 for w in rows))
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


def signed_fields(report: dict[str, Any]) -> dict[str, Any]:
    """The exact message the coordinator signs. `report_sha256` covers the whole body;
    `purpose` keeps a report signature from ever being valid as a job-manifest signature."""
    scoring = report.get("scoring")
    return {"purpose": SIGNATURE_PURPOSE,
            "report_type": report.get("report_type"),
            "report_sha256": report.get("report_sha256"),
            "scoring_version": scoring.get("version") if isinstance(scoring, dict) else None,
            "period": report.get("period")}


def sign_report(report: dict[str, Any], signer: ManifestSigner, now: int) -> dict[str, Any]:
    """Return a signed copy. Refuses a period that hasn't ended yet (its numbers can still
    change, and a live per-job view can reveal hidden tests, threat model #14), a report
    that fails its own checks, and a report whose hash doesn't match its body."""
    try:
        until = int(report["period"]["until"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReportError("report has no valid period") from exc
    if until > now:
        raise ReportError("period is still open; sign only after `until` has passed")
    if not (report.get("checks") or {}).get("ok"):
        raise ReportError("report fails its own checks; fix the ledger before signing")
    if report.get("report_sha256") != report_hash(report):
        raise ReportError("report_sha256 does not match the report body")
    signed = {k: v for k, v in report.items() if k != "signature"}
    signed["signature"] = {"algorithm": "ed25519", "public_key": signer.public_key_hex,
                           "fingerprint": signer.fingerprint,
                           "value": signer.sign(signed_fields(signed))}
    return signed


def check_signature(report: dict[str, Any], trusted_public_key: str | None = None) -> list[str]:
    """Offline check, no database: was this report signed, unedited, by the trusted key?
    Without `trusted_public_key` it only checks the report against the key it names, which
    proves nothing until a person compares that key's fingerprint with the coordinator's.

    The signature block's `fingerprint` field is not signed (it is only a label), so it must
    match the key the block names, or a report signed by anyone could show the
    coordinator's fingerprint (`json-file-v1`). Print `fingerprint(public_key)`, never the
    field itself."""
    if not isinstance(report, dict) or report.get("report_type") != REPORT_TYPE:
        return ["not a reward report"]
    sig = report.get("signature")
    if not isinstance(sig, dict):
        return ["report is not signed"]
    problems = []
    if report.get("report_sha256") != report_hash(report):
        problems.append("report_sha256 does not match the report body (file was edited)")
    named = sig.get("public_key")
    try:
        if not isinstance(named, str):
            raise ValueError("not a string")
        parse_public_key(named)
    except ValueError:
        return problems + ["signature names an invalid public key"]
    if sig.get("fingerprint") != fingerprint(named):
        problems.append("the signature's fingerprint label doesn't belong to the key that "
                        "signed it (edited to look like another key)")
    if trusted_public_key is not None:
        trusted = trusted_public_key.strip().lower()
        try:
            parse_public_key(trusted)
        except ValueError:
            return problems + ["trusted public key is invalid"]
        if named.lower() != trusted:
            problems.append(f"signed by a different key (fingerprint {fingerprint(named)}, "
                            f"expected {fingerprint(trusted)})")
            return problems
    if not verify_manifest(signed_fields(report), str(sig.get("value", "")), named):
        problems.append("signature does not match the report")
    return problems


def verify_report(report: dict[str, Any], ledger: Ledger,
                  trusted_public_key: str | None = None) -> list[str]:
    """Return a list of problems; empty means the report matches the ledger exactly.
    With `trusted_public_key`, the report must also carry a valid signature by that key."""
    problems = []
    if not isinstance(report, dict) or report.get("report_type") != REPORT_TYPE:
        return ["not a reward report"]
    if report.get("report_sha256") != report_hash(report):
        problems.append("report_sha256 does not match the report body (file was edited)")
    if trusted_public_key is not None or "signature" in report:
        problems += [p for p in check_signature(report, trusted_public_key)
                     if p not in problems]
    try:
        period = report["period"]
        fresh = build_report(ledger, int(period["since"]), int(period["until"]),
                             report["scoring"]["version"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return problems + [f"cannot rebuild report: {say(type(exc).__name__)} "
                           f"({say(str(exc), 80)})"]
    if fresh["report_sha256"] != report.get("report_sha256"):
        problems.append("ledger gives a different report for this period")
        entries = report.get("entries")
        old = {str(e.get("job_id")): e for e in entries if isinstance(e, dict)} \
            if isinstance(entries, list) else {}
        new = {e["job_id"]: e for e in fresh["entries"]}
        for job_id in sorted(old.keys() | new.keys()):
            if old.get(job_id) != new.get(job_id):
                problems.append(f"entry differs: {say(job_id, 80)}")
    return problems


def _trusted_key(cli_value: str | None) -> str | None:
    value = cli_value or os.environ.get("BNH_COORDINATOR_KEY") or None
    return value.strip() if value else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.rewards",
                                     description="Simulated reward reports (points only).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rep = sub.add_parser("report", help="build a report from the ledger database")
    rep.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    rep.add_argument("--since", type=int, default=0, help="unix seconds, inclusive")
    rep.add_argument("--until", type=int, default=None,
                     help="unix seconds, exclusive (default: now; must be past to --sign)")
    rep.add_argument("--out", type=Path, default=None, help="write JSON here (else stdout)")
    rep.add_argument("--scoring", default=None, choices=sorted(SCORING_RULES),
                     help=f"scoring rule (default: REWARD_SCORING, else {CURRENT_SCORING})")
    rep.add_argument("--sign", action="store_true",
                     help="sign with the coordinator's key (closed periods only)")
    rep.add_argument("--key-file", type=Path, default=None,
                     help="coordinator private key (default: COORDINATOR_SIGNING_KEY_FILE or "
                          "./data/coordinator-signing.key)")
    ver = sub.add_parser("verify", help="check a saved report (signature and/or ledger)")
    ver.add_argument("report", type=Path)
    ver.add_argument("--db", type=Path, default=Path("./data/prototype.sqlite3"))
    ver.add_argument("--no-db", action="store_true",
                     help="check only the hash and signature; no database needed")
    ver.add_argument("--public-key", default=None,
                     help="coordinator public key you trust (default: BNH_COORDINATOR_KEY)")
    args = parser.parse_args(argv)

    report: Any = None
    if args.cmd == "verify":
        # A report to verify usually came from someone else: read it with the limits in
        # app/json_file.py (`json-file-v1`) and print only what we computed ourselves.
        try:
            report = json_file.load(args.report, "report", max_bytes=MAX_REPORT_BYTES)
        except FileNotFoundError:
            print(f"report not found: {say(str(args.report))}", file=sys.stderr)
            return 2
        except UnsafeJSONError as exc:
            print(f"refusing to read it: {exc}", file=sys.stderr)
            return 2
        if not isinstance(report, dict) or report.get("report_type") != REPORT_TYPE:
            print("not a reward report")
            print("MISMATCH")
            return 1

    if args.cmd == "verify" and args.no_db:
        trusted = _trusted_key(args.public_key)
        problems = check_signature(report, trusted)
        for p in problems:
            print(p)
        if problems:
            print("MISMATCH")
            return 1
        print(f"signed by {fingerprint(report['signature']['public_key'])}")
        if trusted is None:
            print("Compare that fingerprint with the coordinator's (python -m app.signing); "
                  "without a trusted key, anyone could have signed this.")
        print("OK: signature and hash check out (numbers not re-checked; use --db for that)")
        return 0

    if not args.db.exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2
    # A shared ledger copy is opened read-only so checking it never changes its sha256.
    # Someone else's copy is vetted first (app/copy_guard.py, `copy-open-v1`).
    from . import ledger_role
    from .ledger_copy import copy_info
    try:
        is_copy = copy_info(args.db) is not None
        # `ledger-role-v1`: a backup (or a ledger a restore moved aside) is only read,
        # never written: a signature recorded there would never reach the coordinator.
        role = ledger_role.of_file(args.db)
        not_live = role not in (None, ledger_role.LIVE)
        if not_live and args.cmd == "report" and args.sign:
            raise ledger_role.NotLiveLedger(role, str(args.db))
        if not_live and not ledger_role.is_ours(role):
            raise ledger_role.NotLiveLedger(role, str(args.db))
        ledger = Ledger(args.db, untrusted=is_copy, read_only=not_live)
    except sqlite3.DatabaseError as exc:
        print(f"refusing to read {args.db}: {exc}", file=sys.stderr)
        return 2
    except ledger_role.NotLiveLedger as exc:
        print(f"refusing to sign: {exc}" if args.cmd == "report" and args.sign
              else f"refusing to read: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "report":
        now = int(time.time())
        if args.sign and not is_copy:
            try:
                # `ledger-clock-v1`: the signature is recorded in the ledger below, never
                # with a time earlier than one already there.
                now = ledger_clock.tool_now(ledger.latest_time())
            except (ledger_clock.ClockBehindError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
        if args.until is not None:
            until = args.until
        else:
            until = now if args.sign else now + 1
        try:
            from .config import reward_scoring_env
            scoring = args.scoring or reward_scoring_env()
            report = build_report(ledger, args.since, until, scoring)
            if args.sign:
                from .config import default_signing_key_path
                key_path = args.key_file or default_signing_key_path()
                if not key_path.exists():  # never mint a fresh key nobody has pinned
                    raise SigningKeyError(f"coordinator key file not found: {key_path}")
                signer = ManifestSigner.load_or_create(key_path)
                report = sign_report(report, signer, now)
                if not is_copy:  # the signed period's end becomes a recorded time
                    ledger.record_signed_report(now, args.since, until,
                                                report["report_sha256"])
        except (ReportError, SigningKeyError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.out:
            args.out.write_text(text)
            signed = f" signed_by={report['signature']['fingerprint']}" if args.sign else ""
            print(f"wrote {args.out} sha256={report['report_sha256']}{signed}")
        else:
            sys.stdout.write(text)
        return 0 if report["checks"]["ok"] else 1
    # An explicit --public-key demands a signature; the env key only applies to signed files.
    trusted = args.public_key or (_trusted_key(None) if "signature" in report else None)
    problems = verify_report(report, ledger, trusted)
    for p in problems:
        print(p)
    if not problems and isinstance(report.get("signature"), dict):
        print(f"signed by {fingerprint(report['signature']['public_key'])}")
    print("OK: report matches the ledger" if not problems else "MISMATCH")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
