"""Publish signed reward reports and ledger copies in one fixed public place (`reports-v1`).

The repo's `reports/` folder is that place. GitHub keeps its full history, and Netlify
serves the same files at https://boatsnhoes.meme/reports/ (the page there lists them).
Every closed period gets its own folder, and nothing already published is ever replaced,
because `ledger_copy compare` can only prove that history wasn't rewritten if every old
copy is still there.

    python -m app.publish_report add report.json ledger-copy.sqlite3 --public-key <hex>
    python -m app.publish_report check [--public-key <hex>]

`add` refuses unless all of this holds:
  * the report is a signed reward report, unedited, signed by the coordinator key you give
    (`--public-key` or BNH_COORDINATOR_KEY; `python -m app.signing` prints it), and it
    passes its own checks;
  * the database file is a redacted ledger copy (`ledger_copy export`), never the live
    ledger, which holds worker keys, prompts and answers;
  * the copy ends exactly where the report's period ends, and re-running the report on the
    copy gives the identical report;
  * the period ended at least `--min-wait-hours` ago (default 168 = 7 days). A copy shows
    which past jobs were hidden tests and re-checks, so sharing it later gives rigs less to
    work with (threat model #14);
  * the period doesn't overlap one already published and comes after it;
  * no accepted result falls in a gap: between the last published period's end and this
    period's start (or, for the first period, before its start) the copy has no accepted
    results, so every result lands in exactly one published period (`reports-gap-v1`);
  * the previous published copy and this one pass `ledger_copy compare` (the earlier
    history is unchanged).
It then writes `reports/<period>/report.json`, `ledger-copy.sqlite3` and `SHA256SUMS`, and
adds the period to `reports/index.json`. It doesn't touch git: commit and open a pull
request yourself, and it goes live on the site when that is merged into main.

`check` re-checks everything already in the folder (file hashes, signatures, every report
against its copy, every copy against the one before) and changes nothing. CI runs it.
Simulated, off-chain points only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from . import json_file, rewards
from .ledger import Ledger
from .ledger_compare import CompareError, compare_copies
from .ledger_copy import COPY_FORMAT, copy_info
from .json_file import MAX_REPORT_BYTES
from .signing import fingerprint, parse_public_key
from .worker_guard import say

RULE = "reports-v1"
INDEX_FORMAT = "bnh-reports-index/1"
INDEX_NAME = "index.json"
REPORT_NAME = "report.json"
COPY_NAME = "ledger-copy.sqlite3"
SUMS_NAME = "SHA256SUMS"
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "reports"
DEFAULT_MIN_WAIT_HOURS = 168
SITE_URL = "https://boatsnhoes.meme/reports/"
NOTICE = ("Simulated, off-chain points from a test network. Not tokens, not money, and no "
          "promise of either.")


class PublishError(ValueError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def period_id(since: int, until: int) -> str:
    """Folder name for a period, in UTC: `2026-12-01T0000Z_2026-12-08T0000Z`."""
    fmt = lambda t: time.strftime("%Y-%m-%dT%H%MZ", time.gmtime(t))  # noqa: E731
    return f"{fmt(since)}_{fmt(until)}"


def empty_index() -> dict[str, Any]:
    return {"format": INDEX_FORMAT, "rule": RULE, "notice": NOTICE, "site": SITE_URL,
            "coordinator": None, "periods": []}


def load_index(folder: Path) -> dict[str, Any]:
    path = Path(folder) / INDEX_NAME
    if not path.exists():
        return empty_index()
    try:
        index = json_file.load(path, INDEX_NAME)
    except (OSError, ValueError) as exc:
        raise PublishError(f"{INDEX_NAME} is not readable: {exc}") from None
    if not isinstance(index, dict) or index.get("format") != INDEX_FORMAT \
            or not isinstance(index.get("periods"), list):
        raise PublishError(f"{path} is not a {INDEX_FORMAT} index")
    return index


def _trusted_key(value: str | None) -> str:
    key = (value or os.environ.get("BNH_COORDINATOR_KEY") or "").strip().lower()
    if not key:
        raise PublishError("give the coordinator's public key (--public-key or "
                           "BNH_COORDINATOR_KEY; `python -m app.signing` prints it)")
    try:
        parse_public_key(key)
    except ValueError as exc:
        raise PublishError("that public key is not a valid Ed25519 key") from exc
    return key


def _is_copy(path: Path) -> dict[str, Any]:
    try:
        info = copy_info(path)
    except FileNotFoundError:
        raise PublishError(f"file not found: {path}") from None
    except sqlite3.DatabaseError as exc:
        raise PublishError(f"{path.name} is not a ledger file: {exc}") from None
    if info is None:
        raise PublishError(f"{path.name} is a full ledger, not a redacted copy. Never publish "
                           "the live ledger (it holds worker keys, prompts and answers); "
                           "make a copy with `python -m app.ledger_copy export`")
    if info["format"] != COPY_FORMAT:
        raise PublishError(f"{path.name} has unknown copy format {info['format']!r}")
    return info


def _unreported(copy_path: Path, start: int, since: int) -> list[int]:
    """`accepted_at` of every result in the copy with `start <= accepted_at < since`: the
    stretch no published period covers (`reports-gap-v1`). Copies hold everything before
    their `until`, so the newer copy alone shows what a gap would leave out."""
    if since <= start:
        return []
    return [int(r["accepted_at"])
            for r in Ledger(copy_path, untrusted=True).reward_rows(start, since)]


def _gap_problem(times: list[int], start: int, since: int, first: bool) -> str:
    if first:
        return (f"{len(times)} accepted result(s) before this first period starts ({since}) "
                "would never appear in any published report; start the first period at "
                f"{min(times)} or earlier")
    return (f"{len(times)} accepted result(s) between the last published period's end "
            f"({start}) and this period's start ({since}) would never appear in any "
            f"published report; start this period at {start}")


def plan(report_path: Path, copy_path: Path, folder: Path, public_key: str | None,
         now: int, min_wait_hours: int = DEFAULT_MIN_WAIT_HOURS) -> dict[str, Any]:
    """Run every check `add` needs and return the index entry it would write. Writes nothing."""
    report_path, copy_path, folder = Path(report_path), Path(copy_path), Path(folder)
    key = _trusted_key(public_key)
    if min_wait_hours < 0:
        raise PublishError("--min-wait-hours can't be negative")
    try:
        report = json_file.load(report_path, "report", max_bytes=MAX_REPORT_BYTES)
    except FileNotFoundError:
        raise PublishError(f"file not found: {say(str(report_path))}") from None
    except ValueError as exc:
        raise PublishError(str(exc)) from None
    if not isinstance(report, dict) or report.get("report_type") != rewards.REPORT_TYPE:
        raise PublishError(f"{report_path.name} is not a reward report")
    if "signature" not in report:
        raise PublishError("the report is not signed; make it with `rewards report --sign`")
    problems = rewards.check_signature(report, key)
    if problems:
        raise PublishError("signature check failed: " + "; ".join(problems))
    checks = report.get("checks")
    if not isinstance(checks, dict) or checks.get("ok") is not True:
        raise PublishError("the report fails its own checks")
    period = report.get("period")
    if not isinstance(period, dict) or not _is_int(period.get("since")) \
            or not _is_int(period.get("until")):
        raise PublishError("the report has no valid period")
    since, until = period["since"], period["until"]

    info = _is_copy(copy_path)
    if info["until"] != until:
        raise PublishError(f"the copy ends at {info['until']} but the report's period ends at "
                           f"{until}; export the copy with --until {until}")
    wait = min_wait_hours * 3600
    if now - until < wait:
        ready = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(until + wait))
        raise PublishError(f"the period ended less than {min_wait_hours} hours ago; publish "
                           f"on or after {ready} (threat model #14: copies show which jobs "
                           "were hidden tests, so they are shared late)")
    problems = rewards.verify_report(report, Ledger(copy_path, untrusted=True), key)
    if problems:
        raise PublishError("the report does not match the copy: " + "; ".join(problems[:5]))

    index = load_index(folder)
    for entry in index["periods"]:
        label, problem = _entry_shape(entry)
        if problem:
            raise PublishError(f"{INDEX_NAME} is damaged ({label}: {problem}); run "
                               "`publish_report check` and fix it first")
    pid = period_id(since, until)
    if any(p.get("id") == pid for p in index["periods"]) or (folder / pid).exists():
        raise PublishError(f"period {pid} is already published; published files are never "
                           "replaced")
    previous = None
    if index["periods"]:
        last = max(index["periods"], key=lambda p: int(p["until"]))
        if since < int(last["until"]):
            raise PublishError(f"this period starts before the last published one ends "
                               f"({last['id']}); periods can't overlap or go back in time")
        prev_copy = folder / last["files"]["ledger_copy"]["path"]
        try:
            result = compare_copies(prev_copy, copy_path)
        except CompareError as exc:
            raise PublishError(f"could not compare with the previous copy: {exc}") from None
        if not result["ok"]:
            raise PublishError("the new copy changes history already published in "
                               f"{last['id']}: " + "; ".join(result["problems"][:5]))
        previous = {"id": last["id"], "history_unchanged": True}
    # `reports-gap-v1`: nothing accepted may fall between two periods (or before the first).
    start = int(last["until"]) if previous else 0
    gap = _unreported(copy_path, start, since)
    if gap:
        raise PublishError(_gap_problem(gap, start, since, first=not index["periods"]))

    return {
        "id": pid, "since": since, "until": until, "published_at": now,
        **_summary(report),
        "report_sha256": report["report_sha256"],
        "signed_by": {"public_key": key, "fingerprint": fingerprint(key)},
        "files": {
            "report": {"path": f"{pid}/{REPORT_NAME}", "sha256": _sha256(report_path),
                       "bytes": report_path.stat().st_size},
            "ledger_copy": {"path": f"{pid}/{COPY_NAME}", "sha256": _sha256(copy_path),
                            "bytes": copy_path.stat().st_size},
        },
        "previous": previous,
    }


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    """The numbers the /reports/ page shows for a period, taken from its signed report."""
    totals = report.get("totals")
    totals = totals if isinstance(totals, dict) else {}
    workers = report.get("workers")
    scoring = report.get("scoring")

    def whole(value: Any) -> Any:
        return value if _is_int(value) else None

    return {"scoring_version": scoring.get("version") if isinstance(scoring, dict) else None,
            "rigs": len(workers) if isinstance(workers, list) else 0,
            "accepted_results": whole(totals.get("accepted_results", 0)),
            "payable_points": whole(totals.get("payable_points", 0))}


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def publish(report_path: Path, copy_path: Path, folder: Path, public_key: str | None,
            now: int, min_wait_hours: int = DEFAULT_MIN_WAIT_HOURS) -> dict[str, Any]:
    """Check, then write the period's folder and add it to the index."""
    folder = Path(folder)
    entry = plan(report_path, copy_path, folder, public_key, now, min_wait_hours)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / entry["id"]
    staging = folder / f".{entry['id']}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        # Byte-for-byte copies: the published files are exactly the ones that were checked.
        shutil.copyfile(report_path, staging / REPORT_NAME)
        shutil.copyfile(copy_path, staging / COPY_NAME)
        for name, meta in ((REPORT_NAME, entry["files"]["report"]),
                           (COPY_NAME, entry["files"]["ledger_copy"])):
            if _sha256(staging / name) != meta["sha256"]:
                raise PublishError(f"{name} changed while it was being copied; try again")
        (staging / SUMS_NAME).write_text(
            f"{entry['files']['report']['sha256']}  {REPORT_NAME}\n"
            f"{entry['files']['ledger_copy']['sha256']}  {COPY_NAME}\n")
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    index = load_index(folder)
    index["periods"].append(entry)
    index["periods"].sort(key=lambda p: int(p["until"]))
    index["coordinator"] = dict(entry["signed_by"])
    _write_json(folder / INDEX_NAME, index)
    return entry


# Fields `add` writes. `check` refuses anything else, because the page shows the index to
# visitors and CI must vouch for every value on it (`json-file-v1`).
INDEX_KEYS = {"format", "rule", "notice", "site", "coordinator", "periods"}
ENTRY_KEYS = {"id", "since", "until", "published_at", "scoring_version", "report_sha256",
              "signed_by", "rigs", "accepted_results", "payable_points", "files", "previous"}
PERIOD_ID_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{4}Z_\d{4}-\d{2}-\d{2}T\d{4}Z")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2 ** 53


def _signed_by(key: Any) -> dict[str, str] | None:
    """The `signed_by` block `add` writes for this key, or None if it isn't a valid key."""
    if not isinstance(key, str):
        return None
    try:
        parse_public_key(key)
    except ValueError:
        return None
    if key != key.strip().lower():
        return None
    return {"public_key": key, "fingerprint": fingerprint(key)}


def _entry_shape(p: Any) -> tuple[str, str | None]:
    """(printable id, problem or None) for one index entry, before anything else uses it."""
    if not isinstance(p, dict):
        return "?", "an index entry is not an object"
    pid = p.get("id")
    label = say(str(pid), 60)
    if not isinstance(pid, str) or not PERIOD_ID_RE.fullmatch(pid):
        return label, "index entry has no valid period id"
    extra = sorted(set(p) - ENTRY_KEYS)
    missing = sorted(ENTRY_KEYS - set(p))
    if extra or missing:
        return label, ("index entry has unexpected fields " + say(", ".join(extra), 80)
                       if extra else "index entry is incomplete (" + ", ".join(missing) + ")")
    if not all(_is_int(p[k]) for k in ("since", "until", "published_at", "rigs",
                                         "accepted_results", "payable_points")):
        return label, "index entry has a number that isn't a whole number"
    files = p["files"]
    if not isinstance(files, dict) or set(files) != {"report", "ledger_copy"} or not all(
            isinstance(files[k], dict) and set(files[k]) == {"path", "sha256", "bytes"}
            for k in files):
        return label, "index entry's files block is malformed"
    if files["report"]["path"] != f"{pid}/{REPORT_NAME}" \
            or files["ledger_copy"]["path"] != f"{pid}/{COPY_NAME}":
        # The page links to these paths and check reads them: only the two fixed names.
        return label, "index entry points at files outside its own period folder"
    if not isinstance(p["signed_by"], dict) or _signed_by(p["signed_by"].get("public_key")) \
            != p["signed_by"]:
        return label, ("index entry's signed_by is not a valid key with its own fingerprint "
                       "(the page shows that fingerprint)")
    return label, None


def check_folder(folder: Path, public_keys: list[str] | str | None = None) -> list[str]:
    """Re-check everything published. Returns problems; empty means all good.

    With `public_keys` (or BNH_COORDINATOR_KEY), every period must be signed by one of them;
    after a key rotation, pass the old and the new key. Without, it only checks that the
    folder is consistent with itself, which proves nothing about who signed it.

    The index is what the /reports/ page shows, so every value in it (rig, result and point
    counts, scoring version, fingerprints, file sizes, "history unchanged") must match the
    signed report and copy, not just the file hashes (`json-file-v1`)."""
    folder = Path(folder)
    if not (folder / INDEX_NAME).exists():
        return [f"{INDEX_NAME} is missing"]
    try:
        index = load_index(folder)
    except PublishError as exc:
        return [str(exc)]
    if isinstance(public_keys, str):
        public_keys = [public_keys]
    trusted = {_trusted_key(k) for k in public_keys} if public_keys else (
        {_trusted_key(None)} if os.environ.get("BNH_COORDINATOR_KEY") else set())
    problems: list[str] = []
    extra = sorted(set(index) - INDEX_KEYS)
    if extra:
        problems.append(f"{INDEX_NAME} has unexpected fields: {say(', '.join(extra), 80)}")
    for name, value in (("rule", RULE), ("notice", NOTICE), ("site", SITE_URL)):
        if index.get(name) != value:
            problems.append(f"{INDEX_NAME}: '{name}' was changed")
    good = []
    listed = set()
    for p in index["periods"]:
        label, problem = _entry_shape(p)
        if problem:
            problems.append(f"{label}: {problem}")
            if isinstance(p, dict) and isinstance(p.get("id"), str):
                listed.add(p["id"])
            continue
        if p["id"] in listed:
            problems.append(f"{label}: listed twice")
            continue
        listed.add(p["id"])
        good.append(p)
    periods = sorted(good, key=lambda p: p["until"])
    newest = periods[-1]["signed_by"] if periods and len(good) == len(index["periods"]) \
        else None
    if periods and len(good) == len(index["periods"]) and index.get("coordinator") != newest:
        problems.append(f"{INDEX_NAME}: 'coordinator' (shown on the page) isn't the key that "
                        "signed the newest period")
    if not index["periods"] and index.get("coordinator") is not None:
        problems.append(f"{INDEX_NAME}: 'coordinator' is set but nothing is published")
    previous_copy: Path | None = None
    previous_until = None
    previous_id = None
    for p in periods:
        pid = p["id"]
        since, until = p["since"], p["until"]
        report_meta, copy_meta = p["files"]["report"], p["files"]["ledger_copy"]
        key = p["signed_by"]["public_key"]
        if pid != period_id(since, until):
            problems.append(f"{pid}: folder name doesn't match its period")
        if previous_until is not None and since < previous_until:
            problems.append(f"{pid}: overlaps the period before it")
        if p["published_at"] < until:
            problems.append(f"{pid}: published_at is before the period ended")
        expected_previous = None if previous_id is None else {"id": previous_id,
                                                              "history_unchanged": True}
        if p["previous"] != expected_previous:
            problems.append(f"{pid}: 'previous' doesn't name the period before it")
        report_path = folder / report_meta["path"]
        copy_path = folder / copy_meta["path"]
        if (folder / pid).is_symlink() or report_path.is_symlink() or copy_path.is_symlink():
            problems.append(f"{pid}: a published file or folder is a symlink")
            continue
        missing = [x.name for x in (report_path, copy_path) if not x.is_file()]
        if missing:
            problems.append(f"{pid}: missing {', '.join(missing)}")
            continue
        for path, meta in ((report_path, report_meta), (copy_path, copy_meta)):
            if _sha256(path) != meta.get("sha256"):
                problems.append(f"{pid}: {path.name} doesn't match its sha256 in the index")
            if path.stat().st_size != meta.get("bytes"):
                problems.append(f"{pid}: {path.name}'s size in the index is wrong")
        sums = folder / pid / SUMS_NAME
        expected = (f"{report_meta.get('sha256')}  {REPORT_NAME}\n"
                    f"{copy_meta.get('sha256')}  {COPY_NAME}\n")
        if not sums.is_file() or sums.is_symlink() or sums.stat().st_size > 4096 \
                or sums.read_bytes() != expected.encode():
            problems.append(f"{pid}: {SUMS_NAME} is missing or doesn't match the index")
        if trusted and key not in trusted:
            problems.append(f"{pid}: signed by {fingerprint(key)}, not a key you trust ("
                            + ", ".join(sorted(fingerprint(k) for k in trusted)) + ")")
        try:
            report = json_file.load(report_path, REPORT_NAME, max_bytes=MAX_REPORT_BYTES)
            info = _is_copy(copy_path)
        except (ValueError, PublishError) as exc:
            problems.append(f"{pid}: {exc}")
            continue
        if not isinstance(report, dict) or report.get("report_type") != rewards.REPORT_TYPE:
            problems.append(f"{pid}: {REPORT_NAME} is not a reward report")
            continue
        if info["until"] != until or (report.get("period") or {}).get("until") != until \
                or (report.get("period") or {}).get("since") != since:
            problems.append(f"{pid}: report, copy and index disagree on the period")
        if report.get("report_sha256") != p.get("report_sha256"):
            problems.append(f"{pid}: report_sha256 doesn't match the index")
        shown = _summary(report)
        for name, value in shown.items():
            if p[name] != value:
                problems.append(f"{pid}: the index shows {name} {say(p[name], 40)}, the "
                                f"signed report says {say(value, 40)}")
        for issue in rewards.verify_report(report, Ledger(copy_path, untrusted=True), key):
            problems.append(f"{pid}: {issue}")
        start = 0 if previous_until is None else previous_until
        try:
            gap = _unreported(copy_path, start, since)
        except sqlite3.DatabaseError as exc:
            problems.append(f"{pid}: can't read the copy to look for gaps: {exc}")
            gap = []
        if gap:
            problems.append(f"{pid}: {len(gap)} accepted result(s) from {start} to {since} "
                            "are in no published period (a gap before this one)")
        if previous_copy is not None:
            try:
                result = compare_copies(previous_copy, copy_path)
                if not result["ok"]:
                    problems.append(f"{pid}: changes history from the period before it: "
                                    + "; ".join(result["problems"][:3]))
            except CompareError as exc:
                problems.append(f"{pid}: can't compare with the period before it: {exc}")
        previous_copy, previous_until, previous_id = copy_path, until, pid
    for child in sorted(folder.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and child.name not in listed:
            problems.append(f"folder {say(child.name, 60)} is not listed in {INDEX_NAME}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.publish_report", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="publish one closed period (report + ledger copy)")
    add.add_argument("report", type=Path)
    add.add_argument("copy", type=Path)
    add.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    add.add_argument("--public-key", default=None)
    add.add_argument("--min-wait-hours", type=int, default=DEFAULT_MIN_WAIT_HOURS)
    add.add_argument("--dry-run", action="store_true", help="run every check, write nothing")
    chk = sub.add_parser("check", help="re-check everything already published")
    chk.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    chk.add_argument("--public-key", action="append", default=None,
                     help="trusted coordinator key; repeat it after a key rotation")
    args = parser.parse_args(argv)
    now = int(time.time())

    if args.cmd == "check":
        try:
            problems = check_folder(args.dir, args.public_key)
        except PublishError as exc:
            problems = [str(exc)]
        for p in problems:
            print(f"PROBLEM: {p}")
        if problems:
            return 1
        count = len(load_index(args.dir)["periods"])
        print(f"OK: {count} published period(s) check out (hashes, signatures, reports "
              "against copies, history unchanged between copies)")
        if count and not (args.public_key or os.environ.get("BNH_COORDINATOR_KEY")):
            print("No trusted key given, so this only shows the folder is consistent with "
                  "itself. Add --public-key <coordinator key> to check who signed it.")
        return 0

    try:
        if args.dry_run:
            entry = plan(args.report, args.copy, args.dir, args.public_key, now,
                         args.min_wait_hours)
        else:
            entry = publish(args.report, args.copy, args.dir, args.public_key, now,
                            args.min_wait_hours)
    except PublishError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"{'would publish' if args.dry_run else 'published'} {entry['id']}: "
          f"{entry['rigs']} rig(s), {entry['accepted_results']} result(s), "
          f"{entry['payable_points']} simulated points, signed by "
          f"{entry['signed_by']['fingerprint']}")
    if entry["previous"]:
        print(f"history of {entry['previous']['id']} is unchanged in the new copy")
    if not args.dry_run:
        print(f"Next: commit {args.dir.name}/{entry['id']}/ and {args.dir.name}/{INDEX_NAME}, "
              f"open a pull request, and once it's merged into main it is live at "
              f"{SITE_URL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
