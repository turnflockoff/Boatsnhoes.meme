"""Check the prototype's pinned dependencies and their licenses (`deps-check-v1`).

`requirements.txt` says which packages the prototype needs and which version ranges work.
`requirements.lock` pins every package that gets installed (including the ones those pull
in) to one exact version with sha256 hashes, so `pip install --require-hashes -r
requirements.lock` installs the same files every time and refuses a file that was swapped
on the package index. `licenses.json` records the license of each locked package, at that
version, after a person read it.

    python -m app.deps_check lock [--out deps-lock.json]
    python -m app.deps_check installed [--out deps-installed.json]

`lock` reads files only (CI runs it):
  * every package in the lock is pinned with `==` to one exact version and carries at least
    one sha256 hash; nothing in it points pip at another package index, a URL or a local
    folder, or pulls in another file;
  * every package `requirements.txt` asks for is in the lock, at a version inside its range;
  * every locked package has a `licenses.json` entry for exactly that version, with a reason
    it is there and a license on the allowed list (an `OR` choice needs one allowed option,
    an `AND` needs all; `WITH` exceptions are refused); entries for packages no longer in the
    lock are refused too. A new package or a version bump fails until someone re-reads its
    license.

`installed` checks the Python environment it runs in (CI runs it after installing the lock):
  * every locked package that applies to this computer is installed at exactly the locked
    version;
  * the license each installed package declares in its own metadata matches `licenses.json`.
    Metadata that only names a family ("BSD License") or says nothing is a note, not a pass.
  * packages installed but not in the lock are noted (pip, setuptools and wheel are ignored).

Both print one line per check, write a `bnh-deps-check/1` JSON report with `--out` (never
over an existing file), and exit 0 when nothing failed, 1 when something did, 2 on a usage
error or a missing/unreadable file. Neither installs, downloads or changes anything.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Iterable

from packaging.markers import InvalidMarker, Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

SCHEMA = "bnh-deps-check/1"
RULE = "deps-check-v1"
LICENSES_SCHEMA = "bnh-dep-licenses/1"
PROTOTYPE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = PROTOTYPE_DIR / "requirements.lock"
DEFAULT_REQUIREMENTS = PROTOTYPE_DIR / "requirements.txt"
DEFAULT_LICENSES = PROTOTYPE_DIR / "licenses.json"

# Installed in every virtual environment by Python itself; not part of the lock.
INSTALLER_PACKAGES = {"pip", "setuptools", "wheel"}

_HASH_RE = re.compile(r"--hash[=\s]+(\S+)")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_EXACT_PIN_RE = re.compile(r"^==\s*([A-Za-z0-9][A-Za-z0-9.+!_-]*)\Z")

# Plain-text license names seen in package metadata (the `License` field or a classifier),
# mapped to SPDX ids. Only unambiguous names; "BSD License" alone says no clause count.
_LICENSE_ALIASES = {
    "mit": "MIT", "mit license": "MIT", "the mit license": "MIT", "mit-0": "MIT-0",
    "mit no attribution": "MIT-0",
    "bsd-2-clause": "BSD-2-Clause", "bsd 2-clause": "BSD-2-Clause", "simplified bsd": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause", "bsd 3-clause": "BSD-3-Clause", "new bsd": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause", "modified bsd": "BSD-3-Clause",
    "apache-2.0": "Apache-2.0", "apache 2.0": "Apache-2.0", "apache 2": "Apache-2.0",
    "apache license 2.0": "Apache-2.0", "apache license, version 2.0": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "isc": "ISC", "isc license": "ISC", "isc license (iscl)": "ISC",
    "psf-2.0": "PSF-2.0", "psf": "PSF-2.0", "python software foundation license": "PSF-2.0",
    "mpl-2.0": "MPL-2.0", "mpl 2.0": "MPL-2.0", "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
}
# Names that say which family but not which exact license.
_LICENSE_FAMILIES = {
    "bsd": "BSD", "bsd license": "BSD",
    "apache": "Apache", "apache software license": "Apache", "apache license": "Apache",
}
_FAMILY_OF = {"BSD-2-Clause": "BSD", "BSD-3-Clause": "BSD", "Apache-2.0": "Apache"}


# ------------------------------------------------------------------ report

@dataclass
class Report:
    command: str
    checks: list[dict] = field(default_factory=list)

    def add(self, status: str, name: str, detail: str, package: str | None = None) -> None:
        c = {"status": status, "name": name, "detail": detail}
        if package:
            c["package"] = package
        self.checks.append(c)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for c in self.checks:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        return {
            "schema": SCHEMA,
            "rule": RULE,
            "command": self.command,
            "ok": counts.get("fail", 0) == 0,
            "counts": counts,
            "checks": self.checks,
        }


# ------------------------------------------------------------------ lock file

@dataclass
class LockEntry:
    name: str          # canonical (PEP 503) name
    version: str
    marker: str | None
    hashes: list[str]
    line: int


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash-continued lines and drop comments. Returns (first line number, text)."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw
        # A comment starts at '#' at the line start or after whitespace (not inside a hash).
        m = re.search(r"(^|\s)#", line)
        if m:
            line = line[: m.start()]
        cont = line.rstrip().endswith("\\")
        if cont:
            line = line.rstrip()[:-1]
        if not buf:
            start = n
        buf.append(line.strip())
        if not cont:
            joined = " ".join(p for p in buf if p).strip()
            if joined:
                out.append((start, joined))
            buf = []
    if buf:
        joined = " ".join(p for p in buf if p).strip()
        if joined:
            out.append((start, joined))
    return out


def parse_lock(text: str, report: Report) -> list[LockEntry]:
    """Parse a hash-pinned requirements file, adding a `fail` for anything unsafe or loose."""
    entries: list[LockEntry] = []
    seen: dict[str, int] = {}
    for n, line in _logical_lines(text):
        if line.startswith("-"):
            opt = line.split()[0]
            report.add("fail", "lock-option",
                       f"line {n}: '{opt}' is not allowed in the lock; it can change where packages "
                       "come from or pull in other files. Only pinned packages with --hash belong here")
            continue
        hashes = _HASH_RE.findall(line)
        req_text = _HASH_RE.sub("", line).strip()
        leftover = [t for t in req_text.split() if t.startswith("-")]
        if leftover:
            report.add("fail", "lock-option", f"line {n}: unexpected option {leftover[0]!r} on a package line")
            continue
        try:
            req = Requirement(req_text)
        except InvalidRequirement as e:
            report.add("fail", "lock-pin", f"line {n}: can't read {req_text!r} ({e})")
            continue
        name = canonicalize_name(req.name)
        if req.url:
            report.add("fail", "lock-pin", f"line {n}: {name} comes from a URL ({req.url}); "
                       "lock packages must come from the package index by exact version", name)
            continue
        m = _EXACT_PIN_RE.match(str(req.specifier))
        if not m or "*" in m.group(1):
            report.add("fail", "lock-pin", f"line {n}: {name} is not pinned to one exact version "
                       f"(found {str(req.specifier) or 'no version'!r}; need '==X.Y.Z')", name)
            continue
        version = m.group(1)
        bad = [h for h in hashes if not _SHA256_RE.match(h)]
        if not hashes:
            report.add("fail", "lock-hash", f"line {n}: {name}=={version} has no --hash; without one, "
                       "pip can't tell if the file it downloads was swapped", name)
            continue
        if bad:
            report.add("fail", "lock-hash", f"line {n}: {name}=={version} has a hash that isn't a "
                       f"well-formed sha256 ({bad[0][:20]}...)", name)
            continue
        if name in seen:
            report.add("fail", "lock-duplicate", f"line {n}: {name} is listed twice "
                       f"(first on line {seen[name]})", name)
            continue
        seen[name] = n
        marker = str(req.marker) if req.marker else None
        entries.append(LockEntry(name, version, marker, hashes, n))
    return entries


def parse_requirements(text: str, report: Report) -> list[Requirement]:
    reqs: list[Requirement] = []
    for n, line in _logical_lines(text):
        if line.startswith("-"):
            report.add("fail", "requirements", f"requirements.txt line {n}: option {line.split()[0]!r} "
                       "isn't supported here; list packages only")
            continue
        try:
            reqs.append(Requirement(line))
        except InvalidRequirement as e:
            report.add("fail", "requirements", f"requirements.txt line {n}: can't read {line!r} ({e})")
    return reqs


# ------------------------------------------------------------------ licenses

def _tokens(expr: str) -> list[str]:
    return re.findall(r"\(|\)|[^\s()]+", expr)


def license_allowed(expr: str, allowed: Iterable[str]) -> tuple[bool, str]:
    """Is an SPDX expression acceptable? OR needs one allowed side, AND needs both.

    Returns (ok, reason). `WITH` exceptions and anything unparseable are refused.
    """
    allowed_l = {a.lower() for a in allowed}
    toks = _tokens(expr)
    pos = 0

    def peek() -> str | None:
        return toks[pos] if pos < len(toks) else None

    def take() -> str:
        nonlocal pos
        t = toks[pos]
        pos += 1
        return t

    def parse_or() -> bool:
        ok = parse_and()
        while peek() and peek().upper() == "OR":
            take()
            right = parse_and()
            ok = ok or right
        return ok

    def parse_and() -> bool:
        ok = parse_atom()
        while peek() and peek().upper() == "AND":
            take()
            right = parse_atom()
            ok = ok and right
        return ok

    def parse_atom() -> bool:
        t = peek()
        if t is None:
            raise ValueError("expression ends early")
        if t == "(":
            take()
            ok = parse_or()
            if peek() != ")":
                raise ValueError("missing ')'")
            take()
            return ok
        if t in (")",) or t.upper() in ("AND", "OR", "WITH"):
            raise ValueError(f"unexpected {t!r}")
        take()
        if peek() and peek().upper() == "WITH":
            raise ValueError("license exceptions ('WITH') need a person to decide")
        return t.lower() in allowed_l

    try:
        if not toks:
            return False, "no license given"
        ok = parse_or()
        if pos != len(toks):
            raise ValueError(f"unexpected {toks[pos]!r}")
    except ValueError as e:
        return False, str(e)
    return ok, "allowed" if ok else "not on the allowed list"


def _expr_ids(expr: str) -> set[str]:
    return {t for t in _tokens(expr) if t not in ("(", ")") and t.upper() not in ("AND", "OR", "WITH")}


def _norm_expr(expr: str) -> str:
    return " ".join(t.upper() if t.upper() in ("AND", "OR", "WITH") else t.lower() for t in _tokens(expr))


def declared_license(md) -> tuple[str, str | None, set[str]]:
    """What a package's metadata says about its license.

    Returns (kind, value, ids): kind is 'expression' (PEP 639 License-Expression),
    'names' (SPDX ids read from the License field or classifiers), 'family' (only "BSD" or
    "Apache", no version), or 'unknown'.
    """
    expr = (md.get("License-Expression") or "").strip()
    if expr:
        return "expression", expr, _expr_ids(expr)
    ids: set[str] = set()
    families: set[str] = set()
    lic = (md.get("License") or "").strip()
    if lic and "\n" not in lic and len(lic) <= 80:
        key = lic.lower()
        if key in _LICENSE_ALIASES:
            ids.add(_LICENSE_ALIASES[key])
        elif key in _LICENSE_FAMILIES:
            families.add(_LICENSE_FAMILIES[key])
    for c in md.get_all("Classifier") or []:
        parts = [p.strip() for p in c.split("::")]
        if not parts or parts[0] != "License" or len(parts) < 2:
            continue
        key = parts[-1].lower()
        if key in _LICENSE_ALIASES:
            ids.add(_LICENSE_ALIASES[key])
        elif key in _LICENSE_FAMILIES:
            families.add(_LICENSE_FAMILIES[key])
    if ids:
        return "names", ", ".join(sorted(ids)), ids
    if families:
        return "family", ", ".join(sorted(families)), families
    return "unknown", None, set()


def compare_license(reviewed: str, md) -> tuple[str, str]:
    """Compare the reviewed license with a package's metadata. Returns (status, detail)."""
    kind, value, ids = declared_license(md)
    reviewed_ids = {i.lower() for i in _expr_ids(reviewed)}
    if kind == "expression":
        if _norm_expr(value) == _norm_expr(reviewed):
            return "pass", f"metadata says {value}"
        return "fail", f"metadata says {value!r} but licenses.json says {reviewed!r}"
    if kind == "names":
        lower = {i.lower() for i in ids}
        if lower == reviewed_ids:
            return "pass", f"metadata names {value}"
        if lower <= reviewed_ids and " OR " in f" {reviewed.upper()} ":
            return "pass", f"metadata names {value}, one of the reviewed options ({reviewed})"
        return "fail", f"metadata names {value} but licenses.json says {reviewed!r}"
    if kind == "family":
        fams = {_FAMILY_OF.get(i) for i in _expr_ids(reviewed)}
        if ids & fams:
            return "note", f"metadata only says {value}; reviewed as {reviewed} (check the license file if unsure)"
        return "fail", f"metadata says {value} but licenses.json says {reviewed!r}"
    return "note", f"metadata names no license; licenses.json says {reviewed} (check by hand)"


def load_licenses(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != LICENSES_SCHEMA:
        raise ValueError(f"{path.name}: expected schema {LICENSES_SCHEMA!r}")
    if not isinstance(data.get("allowed"), list) or not isinstance(data.get("packages"), dict):
        raise ValueError(f"{path.name}: needs an 'allowed' list and a 'packages' object")
    return data


# ------------------------------------------------------------------ checks

def check_lock(lock_text: str, requirements_text: str, licenses: dict) -> dict:
    report = Report("lock")
    entries = parse_lock(lock_text, report)
    by_name = {e.name: e for e in entries}
    if not entries:
        report.add("fail", "lock-pin", "the lock lists no packages")
    else:
        report.add("pass", "lock-pin", f"{len(entries)} packages, each pinned to one version with sha256 hashes")

    for req in parse_requirements(requirements_text, report):
        name = canonicalize_name(req.name)
        entry = by_name.get(name)
        if entry is None:
            report.add("fail", "requirements", f"{name} is in requirements.txt but not in the lock; "
                       "re-make the lock", name)
            continue
        try:
            inside = req.specifier.contains(Version(entry.version), prereleases=True)
        except InvalidVersion:
            inside = False
        if inside:
            report.add("pass", "requirements", f"{name}=={entry.version} is inside {req.specifier or 'any version'}", name)
        else:
            report.add("fail", "requirements", f"{name}=={entry.version} in the lock is outside "
                       f"requirements.txt's {req.specifier}; re-make the lock", name)

    allowed = licenses.get("allowed", [])
    pkgs = {canonicalize_name(k): v for k, v in licenses.get("packages", {}).items()}
    for e in entries:
        info = pkgs.get(e.name)
        if not isinstance(info, dict):
            report.add("fail", "license", f"{e.name}=={e.version} has no licenses.json entry; read its "
                       "license and add it (with why it's needed)", e.name)
            continue
        if str(info.get("version")) != e.version:
            report.add("fail", "license", f"{e.name}: licenses.json reviewed version {info.get('version')}, "
                       f"the lock has {e.version}; re-check its license and update the entry", e.name)
            continue
        if not str(info.get("why", "")).strip():
            report.add("fail", "license", f"{e.name}: licenses.json doesn't say why it's needed", e.name)
            continue
        lic = str(info.get("license", "")).strip()
        ok, reason = license_allowed(lic, allowed)
        if ok:
            report.add("pass", "license", f"{e.name}=={e.version}: {lic}", e.name)
        else:
            report.add("fail", "license", f"{e.name}=={e.version}: {lic or '(none)'} is refused ({reason}); "
                       "a person must decide before it can be used", e.name)
    for name in sorted(set(pkgs) - set(by_name)):
        report.add("fail", "license", f"licenses.json lists {name}, which is not in the lock; remove the entry", name)
    return report.summary()


def _marker_applies(marker: str | None, env: dict | None) -> bool:
    if not marker:
        return True
    try:
        return Marker(marker).evaluate(env)
    except InvalidMarker:
        return True


def check_installed(lock_text: str, licenses: dict, distributions: Iterable | None = None,
                    env: dict | None = None) -> dict:
    report = Report("installed")
    entries = parse_lock(lock_text, report)
    dists = list(importlib_metadata.distributions() if distributions is None else distributions)
    installed: dict[str, object] = {}
    for d in dists:
        name = d.metadata.get("Name") if d.metadata else None
        if name:
            installed.setdefault(canonicalize_name(name), d)
    pkgs = {canonicalize_name(k): v for k, v in licenses.get("packages", {}).items()}

    skipped = 0
    locked_names = set()
    for e in entries:
        locked_names.add(e.name)
        if not _marker_applies(e.marker, env):
            skipped += 1
            continue
        d = installed.get(e.name)
        if d is None:
            report.add("fail", "installed", f"{e.name}=={e.version} is in the lock but not installed", e.name)
            continue
        if d.version != e.version:
            report.add("fail", "installed", f"{e.name}: installed {d.version}, the lock says {e.version}; "
                       "install with: pip install --require-hashes -r requirements.lock", e.name)
            continue
        report.add("pass", "installed", f"{e.name}=={e.version}", e.name)
        reviewed = str((pkgs.get(e.name) or {}).get("license", "")).strip()
        if not reviewed:
            report.add("fail", "license-metadata", f"{e.name}: no reviewed license in licenses.json", e.name)
            continue
        status, detail = compare_license(reviewed, d.metadata)
        report.add(status, "license-metadata", f"{e.name}: {detail}", e.name)
    if skipped:
        report.add("note", "installed", f"{skipped} locked package(s) don't apply to this computer "
                   "(e.g. Windows-only) and were skipped")
    extra = sorted(set(installed) - locked_names - INSTALLER_PACKAGES)
    for name in extra:
        report.add("note", "not-locked", f"{name}=={installed[name].version} is installed but not in the lock", name)
    return report.summary()


# ------------------------------------------------------------------ CLI

def _print(result: dict) -> None:
    for c in result["checks"]:
        print(f"{c['status'].upper():5} {c['name']:17} {c['detail']}")
    counts = result.get("counts", {})
    print(f"\n{'OK' if result['ok'] else 'PROBLEMS FOUND'}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.deps_check", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd, helptext in (("lock", "check the lock, requirements.txt and licenses.json (reads files only)"),
                          ("installed", "check this Python environment against the lock")):
        p = sub.add_parser(cmd, help=helptext)
        p.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
        p.add_argument("--licenses", type=Path, default=DEFAULT_LICENSES)
        if cmd == "lock":
            p.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
        p.add_argument("--out", type=Path)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if args.out and args.out.exists():
        print(f"error: {args.out} already exists; not overwriting it", file=sys.stderr)
        return 2
    try:
        lock_text = args.lock.read_text(encoding="utf-8")
        licenses = load_licenses(args.licenses)
        if args.cmd == "lock":
            result = check_lock(lock_text, args.requirements.read_text(encoding="utf-8"), licenses)
        else:
            result = check_installed(lock_text, licenses)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    _print(result)
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
