"""Check every locked package for known security problems (`vuln-check-v1`).

`requirements.lock` pins each package to one exact version (see `app.deps_check`). A version
that was fine the day it was locked can later turn out to have a security hole. This tool
asks the Python Package Index (PyPI) about each exact locked version. PyPI's answer includes
the known problems for that version from the Open Source Vulnerabilities database (OSV),
the same source `pip-audit` uses by default.

    python -m app.vuln_check [--lock requirements.lock] [--exceptions vuln-exceptions.json]
                             [--out vuln-check.json]

For every package in the lock (Windows-only ones too), one plain GET to
`https://pypi.org/pypi/<name>/<version>/json`:
  * `vulns` pass: PyPI knows no problem for that exact version.
  * `vulns` fail: it knows one. Records that are the same problem under different names
    (a GHSA id, a PYSEC id, a CVE number) are shown once, with the version that fixes it.
    Fix: raise the version in requirements.txt and re-make the lock (see its header).
  * `vulns` note: a problem a person looked at and accepted for now, listed in
    `vuln-exceptions.json` (`bnh-vuln-exceptions/1`) with the package, the problem's id,
    why it's acceptable, the day it was reviewed and an end date at most 90 days later. On
    the end date it fails again until someone looks again. An exception that no longer
    matches anything is a note (remove it); a malformed or expired one fails.
  * `lookup` fail: PyPI couldn't be reached (after one retry), didn't know the version, or
    answered about a different package or without any vulnerability data. "Couldn't check"
    never counts as clean.

Prints one line per check, writes a `bnh-vuln-check/1` JSON report with `--out` (never over
an existing file), exits 0 when nothing failed, 1 when something did, 2 on a usage error or
an unreadable lock or exceptions file. It reads only public data and sends only package
names and versions; it installs, changes and uploads nothing. It only knows problems that
have been reported and published, so a pass means "nothing known today", not "safe".
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from packaging.utils import canonicalize_name

from app import deps_check

SCHEMA = "bnh-vuln-check/1"
RULE = "vuln-check-v1"
EXCEPTIONS_SCHEMA = "bnh-vuln-exceptions/1"
DEFAULT_LOCK = deps_check.DEFAULT_LOCK
DEFAULT_EXCEPTIONS = deps_check.PROTOTYPE_DIR / "vuln-exceptions.json"
PYPI_JSON = "https://pypi.org/pypi"
MAX_EXCEPTION_DAYS = 90
DETAIL_CHARS = 200


class LookupFailed(Exception):
    """PyPI couldn't tell us about this version (network trouble, unknown version, ...)."""


# A fetcher takes (canonical name, version) and returns PyPI's JSON answer as a dict,
# or raises LookupFailed.
Fetcher = Callable[[str, str], dict]


# ------------------------------------------------------------------ fetching

def make_pypi_fetch(client=None, *, base: str = PYPI_JSON, retries: int = 1,
                    pause: float = 2.0, sleep: Callable[[float], None] = time.sleep) -> Fetcher:
    """Return a fetcher that asks PyPI's JSON API. `client` is an httpx.Client (tests pass one
    with a fake transport). Network errors, 429 and 5xx answers are retried `retries` times."""
    import httpx

    own = client is None
    if own:
        client = httpx.Client(timeout=20.0, follow_redirects=True,
                              headers={"User-Agent": "bnh-vuln-check/1 (+https://boatsnhoes.meme)"})

    def fetch(name: str, version: str) -> dict:
        url = f"{base}/{quote(name, safe='')}/{quote(version, safe='')}/json"
        last = ""
        for attempt in range(retries + 1):
            if attempt:
                sleep(pause)
            try:
                r = client.get(url)
            except httpx.HTTPError as e:
                last = f"couldn't reach PyPI ({type(e).__name__})"
                continue
            if r.url.host != httpx.URL(base).host:
                raise LookupFailed(f"PyPI redirected to another site ({r.url.host})")
            if r.status_code == 404:
                raise LookupFailed("PyPI has no such version (404)")
            if r.status_code == 429 or r.status_code >= 500:
                last = f"PyPI answered {r.status_code}"
                continue
            if r.status_code != 200:
                raise LookupFailed(f"PyPI answered {r.status_code}")
            try:
                data = r.json()
            except ValueError:
                raise LookupFailed("PyPI's answer wasn't JSON") from None
            if not isinstance(data, dict):
                raise LookupFailed("PyPI's answer wasn't a JSON object")
            return data
        raise LookupFailed(last + (f", tried {retries + 1} times" if retries else ""))

    fetch.close = client.close if own else (lambda: None)  # type: ignore[attr-defined]
    return fetch


# ------------------------------------------------------------------ exceptions file

def _date(value, field: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"'{field}' must be a date like 2026-11-24, got {value!r}") from None


def load_exceptions(path: Path) -> dict:
    """Read the exceptions file. A missing file means "no exceptions"."""
    if not path.exists():
        return {"schema": EXCEPTIONS_SCHEMA, "exceptions": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != EXCEPTIONS_SCHEMA:
        raise ValueError(f"{path.name}: expected schema {EXCEPTIONS_SCHEMA!r}")
    if not isinstance(data.get("exceptions"), list):
        raise ValueError(f"{path.name}: needs an 'exceptions' list (it can be empty)")
    return data


def check_exceptions(data: dict, today: dt.date, report: deps_check.Report) -> list[dict]:
    """Validate each exception; return the usable ones (well-formed and not expired)."""
    usable: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, ex in enumerate(data.get("exceptions", []), start=1):
        where = f"exception {i}"
        if not isinstance(ex, dict):
            report.add("fail", "exception", f"{where} is not an object")
            continue
        pkg = canonicalize_name(str(ex.get("package", "")).strip()) if str(ex.get("package", "")).strip() else ""
        vid = str(ex.get("id", "")).strip()
        reason = str(ex.get("reason", "")).strip()
        if not pkg or not vid:
            report.add("fail", "exception", f"{where} needs a 'package' and the problem's 'id'")
            continue
        where = f"exception for {pkg} {vid}"
        if len(reason) < 20:
            report.add("fail", "exception", f"{where}: 'reason' must say in a sentence why it's acceptable", pkg)
            continue
        try:
            reviewed = _date(ex.get("reviewed"), "reviewed")
            expires = _date(ex.get("expires"), "expires")
        except ValueError as e:
            report.add("fail", "exception", f"{where}: {e}", pkg)
            continue
        if reviewed > today:
            report.add("fail", "exception", f"{where}: reviewed date {reviewed} is in the future", pkg)
            continue
        if expires <= reviewed or (expires - reviewed).days > MAX_EXCEPTION_DAYS:
            report.add("fail", "exception", f"{where}: 'expires' must be after 'reviewed' and at most "
                       f"{MAX_EXCEPTION_DAYS} days later", pkg)
            continue
        if (pkg, vid.upper()) in seen:
            report.add("fail", "exception", f"{where} is listed twice", pkg)
            continue
        seen.add((pkg, vid.upper()))
        if today >= expires:
            report.add("fail", "exception", f"{where} ended on {expires}; look at it again: upgrade, "
                       "or renew it with a new reason and dates", pkg)
            continue
        usable.append({"package": pkg, "id": vid, "reason": reason,
                       "reviewed": str(reviewed), "expires": str(expires), "used": False})
    return usable


# ------------------------------------------------------------------ findings

def group_vulns(records: list) -> tuple[list[dict], int]:
    """Merge records that name the same problem (shared id or alias). Withdrawn records are
    dropped. Returns (groups, number withdrawn)."""
    live = []
    withdrawn = 0
    for rec in records:
        if not isinstance(rec, dict) or not str(rec.get("id", "")).strip():
            continue
        if rec.get("withdrawn"):
            withdrawn += 1
            continue
        live.append(rec)

    groups: list[dict] = []
    for rec in live:
        names = {str(rec["id"]).strip()} | {str(a).strip() for a in rec.get("aliases") or [] if str(a).strip()}
        hits = [g for g in groups if g["names"] & {n.upper() for n in names}]
        merged = {"names": {n.upper() for n in names}, "records": [rec]}
        for g in hits:
            merged["names"] |= g["names"]
            merged["records"] += g["records"]
            groups.remove(g)
        groups.append(merged)

    out = []
    for g in groups:
        recs = g["records"]
        ids = sorted({str(r["id"]).strip() for r in recs})
        aliases = sorted({str(a).strip() for r in recs for a in (r.get("aliases") or [])} - set(ids))
        fixed = sorted({str(v) for r in recs for v in (r.get("fixed_in") or [])})
        text = next((str(r.get("summary") or r.get("details") or "").strip() for r in recs
                     if (r.get("summary") or r.get("details"))), "")
        text = " ".join(text.split())
        if len(text) > DETAIL_CHARS:
            text = text[: DETAIL_CHARS - 3] + "..."
        link = next((str(r["link"]) for r in sorted(recs, key=lambda r: str(r["id"])) if r.get("link")), "")
        out.append({"ids": ids, "aliases": aliases, "fixed_in": fixed, "summary": text,
                    "link": link, "_names": g["names"]})
    out.sort(key=lambda f: f["ids"][0])
    return out, withdrawn


def _label(f: dict) -> str:
    cves = [a for a in f["aliases"] + f["ids"] if a.upper().startswith("CVE-")]
    return ", ".join(f["ids"]) + (f" ({', '.join(sorted(set(cves)))})" if cves else "")


def check_vulns(lock_text: str, exceptions: dict, fetch: Fetcher, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    report = deps_check.Report("vulns")
    entries = deps_check.parse_lock(lock_text, report)
    if not entries:
        report.add("fail", "lookup", "the lock lists no packages to check")
    usable = check_exceptions(exceptions, today, report)
    findings: list[dict] = []
    checked = 0

    for e in entries:
        try:
            data = fetch(e.name, e.version)
        except LookupFailed as err:
            report.add("fail", "lookup", f"{e.name}=={e.version}: {err}; couldn't check it", e.name)
            continue
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        got_name = canonicalize_name(str(info.get("name", ""))) if info.get("name") else ""
        if got_name != e.name or str(info.get("version", "")) != e.version:
            report.add("fail", "lookup", f"{e.name}=={e.version}: PyPI answered about "
                       f"{got_name or '?'}=={info.get('version', '?')}; couldn't check it", e.name)
            continue
        records = data.get("vulnerabilities")
        if not isinstance(records, list):
            report.add("fail", "lookup", f"{e.name}=={e.version}: PyPI's answer has no vulnerability "
                       "list; couldn't check it", e.name)
            continue
        checked += 1
        groups, withdrawn = group_vulns(records)
        if withdrawn:
            report.add("note", "vulns", f"{e.name}=={e.version}: {withdrawn} withdrawn report(s) ignored", e.name)
        if not groups:
            report.add("pass", "vulns", f"{e.name}=={e.version}: no known problems", e.name)
            continue
        for g in groups:
            names = g.pop("_names")
            fix = f"fixed in {', '.join(g['fixed_in'])}" if g["fixed_in"] else "no fixed version listed yet"
            ex = next((x for x in usable if x["package"] == e.name and x["id"].upper() in names), None)
            finding = {"package": e.name, "version": e.version, **g, "accepted": None}
            if ex:
                ex["used"] = True
                finding["accepted"] = {k: ex[k] for k in ("id", "reason", "reviewed", "expires")}
                report.add("note", "vulns", f"{e.name}=={e.version}: {_label(g)}, {fix}; accepted until "
                           f"{ex['expires']} ({ex['reason']})", e.name)
            else:
                report.add("fail", "vulns", f"{e.name}=={e.version}: {_label(g)}, {fix}. "
                           f"{g['summary'] or 'No description.'} Raise the version in requirements.txt "
                           "and re-make the lock, or add a reviewed entry to vuln-exceptions.json", e.name)
            findings.append(finding)

    for ex in usable:
        if not ex["used"]:
            report.add("note", "exception", f"exception for {ex['package']} {ex['id']} matches nothing "
                       "in the lock any more; remove it", ex["package"])

    result = report.summary()
    result.update({
        "schema": SCHEMA,
        "rule": RULE,
        "checked_on": str(today),
        "source": "PyPI JSON API (OSV data)",
        "packages": len(entries),
        "packages_checked": checked,
        "findings": findings,
    })
    return result


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None, fetch: Fetcher | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.vuln_check", description=__doc__.split("\n")[0])
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    ap.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    ap.add_argument("--out", type=Path)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if args.out and args.out.exists():
        print(f"error: {args.out} already exists; not overwriting it", file=sys.stderr)
        return 2
    try:
        lock_text = args.lock.read_text(encoding="utf-8")
        exceptions = load_exceptions(args.exceptions)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    own = fetch is None
    fetch = fetch or make_pypi_fetch()
    try:
        result = check_vulns(lock_text, exceptions, fetch)
    finally:
        if own:
            fetch.close()  # type: ignore[attr-defined]
    deps_check._print(result)
    print(f"{result['packages_checked']} of {result['packages']} packages checked against PyPI "
          f"on {result['checked_on']}; {len(result['findings'])} known problem(s)")
    if args.out:
        try:
            with args.out.open("x", encoding="utf-8") as f:
                f.write(json.dumps(result, indent=2) + "\n")
        except OSError as e:
            print(f"error: couldn't write {args.out}: {e}", file=sys.stderr)
            return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
