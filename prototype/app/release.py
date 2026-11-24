"""Signed worker releases with checksums and several signers (`release-sign-v1`).

Threat (README SECURITY #2, #8): a fake or tampered copy of the worker software handed to a
rig owner ("download the new miner here"), or one person's stolen key being enough to push
a bad release. Defense: every release is a list of file fingerprints, and it only counts
when at least `threshold` *different* people from a pinned signer list have each signed it
with their own key.

Files:
  * `release.json` (`bnh-release/1`): name, version, source commit, and every file under the
    release folder that git tracks, with its sha256 and size. Only tracked files, so `.env`,
    keys and databases that sit in the folder are never swept in. Written with a plain
    `SHA256SUMS` next to it, so anyone can check the files with `sha256sum -c` too.
  * `release.sigs.json` (`bnh-release-signatures/1`): one Ed25519 signature per signer over
    the release's sha256. Each signer adds theirs on their own machine with their own key.
  * the signer list (`bnh-release-signers/1`): names, public keys and the threshold (at
    least 2). Rigs keep their own pinned copy; a release can't bring its own list.

Commands (run from `prototype/`):
    python -m app.release keygen --out ~/bnh-private/release-signer.key --name alice
    python -m app.release make --root . --version 0.5.49 --out ../release/release.json
    python -m app.release sign --key ~/bnh-private/release-signer.key --name alice ../release/release.json
    python -m app.release verify --signers release-signers.json ../release/release.json --root .

`verify` exits 0 only when the manifest is well formed, enough distinct pinned signers
signed exactly this manifest, every signature present is valid, and (with `--root`) every
file on disk matches and nothing else in the folder could be imported by Python
(`release-folder-v1`: no extra `.py`/`.pyc`/`.so`/`.pth`, no links, and every
`__pycache__` file Python might load is byte-for-byte what this Python builds from the
checked source). It never runs, installs or downloads anything.

What it does not prove: that the signed code is safe (only that enough named people agreed
to ship it), or that a signer's machine wasn't compromised when they signed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import marshal
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.version import InvalidVersion, Version

from . import json_file
from .security import canonical_json
from .signing import (ManifestSigner, SigningKeyError, fingerprint, parse_public_key,
                      verify_manifest)
from .worker_guard import say

RULE = "release-sign-v1"
MANIFEST_FORMAT = "bnh-release/1"
SIGS_FORMAT = "bnh-release-signatures/1"
SIGNERS_FORMAT = "bnh-release-signers/1"
CHECK_FORMAT = "bnh-release-check/1"
SIGN_PURPOSE = "bnh-release-signature/1"  # separate from job and report signatures
SUMS_NAME = "SHA256SUMS"
MIN_THRESHOLD = 2
MAX_FILES = 5000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}\Z")
HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}\Z")
# Folders a running rig keeps next to the code (keys, databases, virtualenvs). Python caches
# (`__pycache__`) are not skipped: they are checked against the source (`release-folder-v1`).
IGNORED_DIRS = {"data", "venv", "node_modules"}
VERSION_RE = re.compile(r"^[0-9A-Za-z.+!_-]{1,64}\Z")


class ReleaseError(ValueError):
    """A release, signature or signer file can't be used, or a command was refused."""


def _now_iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def inside_git_folder(path: Path) -> Path | None:
    for parent in [path.resolve().parent, *path.resolve().parent.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _write_new(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write a file that must not exist yet (never overwrite)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _replace(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _dump(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_json(path: Path, what: str) -> Any:
    # Release files come from whoever published the download: read them with the limits in
    # app/json_file.py (`json-file-v1`): size, depth, no NaN, no key written twice.
    try:
        return json_file.load(path, what)
    except FileNotFoundError as exc:
        raise ReleaseError(f"{what} {say(str(path))} does not exist") from exc
    except (OSError, ValueError) as exc:
        raise ReleaseError(str(exc) if isinstance(exc, json_file.UnsafeJSONError)
                           else f"{what} {say(str(path))} is not readable") from None


# ---- paths ------------------------------------------------------------------------------

def check_rel_path(path: Any) -> str | None:
    """Problem with one manifest path, or None. Paths are relative, forward-slash, inside the
    release folder, with no `..`, no empty parts and nothing hidden-looking like `.git`."""
    if not isinstance(path, str) or not path or len(path) > 400:
        return "path must be a non-empty string of at most 400 characters"
    if "\\" in path or "\x00" in path or any(ord(c) < 32 for c in path):
        return "path has a backslash or control character"
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return "path must be relative"
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "path has an empty, '.' or '..' part"
    if parts[0] == ".git" or ".git" in parts:
        return "path is inside .git"
    return None


# ---- manifest ---------------------------------------------------------------------------

def manifest_sha256(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest)).hexdigest()


def check_manifest(manifest: Any) -> list[str]:
    """Problems with a release manifest's shape (not its files on disk)."""
    if not isinstance(manifest, dict):
        return ["release manifest is not a JSON object"]
    problems: list[str] = []
    expected = {"format", "name", "version", "source_commit", "made_at", "files", "rule"}
    if set(manifest) != expected:
        extra = sorted(set(manifest) - expected)
        missing = sorted(expected - set(manifest))
        problems.append(f"release manifest fields wrong (extra {extra}, missing {missing})")
    if manifest.get("format") != MANIFEST_FORMAT:
        problems.append(f"release format must be {MANIFEST_FORMAT}")
    if manifest.get("rule") != RULE:
        problems.append(f"release rule must be {RULE}")
    if not isinstance(manifest.get("name"), str) or not NAME_RE.match(manifest.get("name", "")):
        problems.append("release name must be lowercase letters, digits, '-' or '_'")
    try:
        Version(str(manifest.get("version")))
        if not isinstance(manifest.get("version"), str) \
                or not VERSION_RE.match(manifest["version"]):
            raise InvalidVersion  # no spaces, line breaks or other hidden characters
    except InvalidVersion:
        problems.append("release version is not a valid version number")
    commit = manifest.get("source_commit")
    if not (isinstance(commit, str) and COMMIT_RE.match(commit)):
        problems.append("source_commit must be a 40-character lowercase git commit hash")
    if not isinstance(manifest.get("made_at"), str):
        problems.append("made_at must be a time string")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        problems.append("release has no files")
        return problems
    if len(files) > MAX_FILES:
        problems.append(f"release lists more than {MAX_FILES} files")
    seen: set[str] = set()
    paths: list[str] = []
    for i, entry in enumerate(files):
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
            problems.append(f"file #{i + 1} must have exactly path, sha256 and size")
            continue
        path = entry["path"]
        bad = check_rel_path(path)
        if bad:
            problems.append(f"file #{i + 1}: {bad}")
            continue
        if path.casefold() in seen:
            problems.append(f"{path}: listed twice (or twice differing only in case)")
        seen.add(path.casefold())
        paths.append(path)
        if not (isinstance(entry["sha256"], str) and HEX64.match(entry["sha256"])):
            problems.append(f"{path}: sha256 must be 64 lowercase hex characters")
        size = entry["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            problems.append(f"{path}: size must be a whole number 0 or more")
    if paths != sorted(paths):
        problems.append("files must be sorted by path")
    return problems


def git_tracked_files(root: Path) -> tuple[list[str], str]:
    """Files git tracks under `root` (relative to it) and the current commit. Refuses a
    working tree with uncommitted changes under `root`, so the manifest matches the commit."""
    def git(*args: str) -> bytes:
        try:
            return subprocess.run(["git", "-C", str(root), *args], check=True,
                                  capture_output=True, stdin=subprocess.DEVNULL,
                                  timeout=60).stdout
        except FileNotFoundError as exc:
            raise ReleaseError("git is not installed") from exc
        except subprocess.CalledProcessError as exc:
            raise ReleaseError(f"{root} is not inside a git checkout") from exc

    commit = git("rev-parse", "HEAD").decode().strip()
    dirty = git("status", "--porcelain", "--untracked-files=no", "--", ".").decode().strip()
    if dirty:
        raise ReleaseError(f"{root} has uncommitted changes to tracked files; commit them "
                           "first so the release matches its commit")
    raw = git("ls-files", "-z", "--", ".")
    return sorted(p for p in raw.decode("utf-8").split("\0") if p), commit


def make_manifest(root: Path, name: str, version: str, now: float | None = None) -> dict[str, Any]:
    root = Path(root)
    paths, commit = git_tracked_files(root)
    if not paths:
        raise ReleaseError(f"git tracks no files under {root}")
    files = []
    for rel in paths:
        bad = check_rel_path(rel)
        if bad:
            raise ReleaseError(f"{rel}: {bad}")
        full = root / rel
        if full.is_symlink() or not full.is_file():
            raise ReleaseError(f"{rel} is a symlink or not a regular file; releases hold "
                               "plain files only")
        files.append({"path": rel, "sha256": sha256_file(full), "size": full.stat().st_size})
    manifest = {"format": MANIFEST_FORMAT, "rule": RULE, "name": name, "version": version,
                "source_commit": commit, "made_at": _now_iso(now), "files": files}
    problems = check_manifest(manifest)
    if problems:
        raise ReleaseError("; ".join(problems))
    return manifest


def sums_text(manifest: dict[str, Any]) -> str:
    """`sha256sum -c` compatible lines, so the files can be checked without this tool."""
    return "".join(f"{f['sha256']}  {f['path']}\n" for f in manifest["files"])


def write_release(manifest: dict[str, Any], out: Path) -> Path:
    out = Path(out)
    sums = out.with_name(SUMS_NAME)
    for path in (out, sums, sigs_path_for(out)):
        if path.exists():
            raise ReleaseError(f"{path} already exists; releases are never overwritten")
    _write_new(out, _dump(manifest))
    _write_new(sums, sums_text(manifest).encode("utf-8"))
    return sums


# ---- signer list ------------------------------------------------------------------------

def check_signers(signers: Any) -> list[str]:
    if not isinstance(signers, dict):
        return ["signer list is not a JSON object"]
    problems: list[str] = []
    if signers.get("format") != SIGNERS_FORMAT:
        problems.append(f"signer list format must be {SIGNERS_FORMAT}")
    entries = signers.get("signers")
    if not isinstance(entries, list) or not entries:
        return problems + ["signer list has no signers"]
    names: set[str] = set()
    keys: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"signer #{i + 1} is not an object")
            continue
        name, key = entry.get("name"), entry.get("public_key")
        if not isinstance(name, str) or not NAME_RE.match(name):
            problems.append(f"signer #{i + 1}: name must be lowercase letters, digits, - or _")
        elif name in names:
            problems.append(f"signer {name} listed twice")
        names.add(name if isinstance(name, str) else "")
        try:
            parse_public_key(key if isinstance(key, str) else "")
            if key != key.strip().lower():
                raise ValueError
        except ValueError:
            problems.append(f"signer #{i + 1}: public_key must be 64 lowercase hex characters")
            continue
        if key in keys:
            problems.append(f"signer #{i + 1}: the same public key is listed twice (one key "
                            "must not count as two people)")
        keys.add(key)
    threshold = signers.get("threshold")
    if not isinstance(threshold, int) or isinstance(threshold, bool):
        problems.append("threshold must be a whole number")
    elif threshold < MIN_THRESHOLD:
        problems.append(f"threshold must be at least {MIN_THRESHOLD}: one person alone must "
                        "not be able to sign a release")
    elif threshold > len(entries):
        problems.append(f"threshold {threshold} is more than the {len(entries)} signers listed")
    return problems


def load_signers(path: Path) -> dict[str, Any]:
    signers = _load_json(path, "signer list")
    problems = check_signers(signers)
    if problems:
        raise ReleaseError("signer list: " + "; ".join(problems))
    return signers


# ---- signatures -------------------------------------------------------------------------

def sigs_path_for(manifest_path: Path) -> Path:
    manifest_path = Path(manifest_path)
    stem = manifest_path.name[:-5] if manifest_path.name.endswith(".json") else manifest_path.name
    return manifest_path.with_name(stem + ".sigs.json")


def signed_fields(manifest: dict[str, Any]) -> dict[str, Any]:
    return {"purpose": SIGN_PURPOSE, "name": manifest["name"], "version": manifest["version"],
            "release_sha256": manifest_sha256(manifest)}


def sign_release(manifest_path: Path, key_path: Path, signer_name: str,
                 sigs_path: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Add one signer's signature to the release's signature file (created if missing)."""
    if not isinstance(signer_name, str) or not NAME_RE.match(signer_name):
        raise ReleaseError("signer name must be lowercase letters, digits, '-' or '_'")
    manifest = _load_json(manifest_path, "release manifest")
    problems = check_manifest(manifest)
    if problems:
        raise ReleaseError("won't sign a malformed release: " + "; ".join(problems))
    key_path = Path(key_path)
    if not key_path.exists():
        raise ReleaseError(f"{key_path} does not exist (plug in the key's drive?)")
    try:
        signer = ManifestSigner.load(key_path)
    except SigningKeyError as exc:
        raise ReleaseError(str(exc)) from exc
    sigs_path = Path(sigs_path) if sigs_path else sigs_path_for(manifest_path)
    release_sha = manifest_sha256(manifest)
    if sigs_path.exists():
        sigs = _load_json(sigs_path, "signature file")
        if not isinstance(sigs, dict) or sigs.get("format") != SIGS_FORMAT \
                or not isinstance(sigs.get("signatures"), list):
            raise ReleaseError(f"{sigs_path} is not a {SIGS_FORMAT} file")
        if sigs.get("release_sha256") != release_sha:
            raise ReleaseError(f"{sigs_path} belongs to a different release (or this one "
                               "was changed after others signed it)")
    else:
        sigs = {"format": SIGS_FORMAT, "release_sha256": release_sha, "signatures": []}
    for existing in sigs["signatures"]:
        if not isinstance(existing, dict):
            continue
        if existing.get("signer") == signer_name:
            raise ReleaseError(f"{signer_name} has already signed this release")
        if existing.get("public_key") == signer.public_key_hex:
            raise ReleaseError("this key has already signed this release under the name "
                               f"{existing.get('signer')!r}")
    entry = {"signer": signer_name, "public_key": signer.public_key_hex,
             "signature": signer.sign(signed_fields(manifest)), "signed_at": _now_iso(now)}
    sigs["signatures"].append(entry)
    data = _dump(sigs)
    if sigs_path.exists():
        _replace(sigs_path, data)
    else:
        _write_new(sigs_path, data)
    return entry


def keygen(out: Path, name: str) -> dict[str, str]:
    """New signer key, owner-only, never overwriting and never inside a git folder."""
    if not NAME_RE.match(name or ""):
        raise ReleaseError("signer name must be lowercase letters, digits, '-' or '_'")
    out = Path(out)
    if out.exists():
        raise ReleaseError(f"{out} already exists; never overwrite a signing key")
    repo = inside_git_folder(out)
    if repo is not None:
        raise ReleaseError(f"{out} is inside the git folder {repo}; keep signing keys out of "
                           "repositories (a USB stick or ~/bnh-private)")
    signer = ManifestSigner.load_or_create(out)
    return {"name": name, "public_key": signer.public_key_hex,
            "fingerprint": signer.fingerprint}


# ---- verify -----------------------------------------------------------------------------

# ---- the release folder on disk (`release-folder-v1`) -----------------------------------
#
# `verify --root` is run on a folder someone else handed over (a download, a USB stick, a
# "fixed" copy). Python runs more than `.py` files: a sourceless `json.pyc` or an extension
# module `json.so` next to the code shadows the standard library (the folder is first on
# sys.path under `python -m`), a `__pycache__/worker.cpython-313.pyc` whose header matches
# the source's time and size is run *instead of* the checked source, and a linked folder
# named like a module is imported through the link. All three passed `release-sign-v1`
# and ran their code (see tests/test_release_folder.py). Now anything Python could import
# that isn't in the signed release is a problem, and a compiled cache only passes when it
# is byte-for-byte what this Python builds from the checked source.

FOLDER_RULE = "release-folder-v1"
# Every ending Python can import or load (plus `.pth`, `.pyw`, `.pyd` and the old `.pyo`).
CODE_SUFFIXES = tuple(sorted(set(importlib.machinery.all_suffixes())
                             | {".py", ".pyc", ".pyo", ".pyw", ".pyd", ".pth", ".so"}))
CACHE_NAME_RE = re.compile(r"^(?P<stem>[^./]+)\.(?P<tag>[^./]+)(?:\.opt-(?P<opt>[12]))?\.pyc\Z")
MAX_FOLDER_ENTRIES = 100_000  # an honest prototype/ holds a few hundred
MAX_LISTED_NOTES = 50         # extra non-code files listed one by one before summarising


def _open_regular(full: Path) -> int:
    """File descriptor for a plain file, never following a final symlink and never
    blocking on a pipe or device. Raises OSError otherwise."""
    fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{full} is not a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_regular(full: Path, size: int) -> bytes | None:
    """The file's bytes if it is a plain file of exactly `size` bytes, else None. Reads at
    most `size + 1` bytes (the signed size), so a huge file is never read in full."""
    try:
        fd = _open_regular(full)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(size + 1)
    return data if len(data) == size else None


def expected_cache(source: bytes, source_path: Path, source_mtime: float, flags: int,
                   opt: int) -> bytes | None:
    """The exact `.pyc` this Python writes for `source` (CPython's layout: magic, flags,
    then time+size or a source hash, then the compiled code). Our own compile of a source
    that already matched its signed hash; untrusted cache bytes are only compared, never
    unmarshalled (marshal isn't safe on hostile data)."""
    if flags not in (0, 1, 3):
        return None
    try:
        code = compile(source, str(source_path), "exec", dont_inherit=True, optimize=opt)
    except (SyntaxError, ValueError):
        return None
    head = bytearray(importlib.util.MAGIC_NUMBER) + flags.to_bytes(4, "little")
    if flags == 0:
        head += (int(source_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
        head += (len(source) & 0xFFFFFFFF).to_bytes(4, "little")
    else:
        head += importlib.util.source_hash(source)
    return bytes(head) + marshal.dumps(code)


def _is_code(name: str) -> bool:
    return name.endswith(CODE_SUFFIXES)


def _scan_folder(root: Path, listed: set[str]) -> tuple[list[str], list[str], list[str]]:
    """Walk the folder without following links. Returns (problems, extra non-code files,
    `__pycache__` folders to check). Hidden names and IGNORED_DIRS are skipped."""
    problems: list[str] = []
    extras: list[str] = []
    caches: list[str] = []
    stack = [""]
    seen = 0
    while stack:
        rel_dir = stack.pop()
        try:
            with os.scandir(root / rel_dir if rel_dir else root) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            problems.append(f"{rel_dir or '.'}/: can't read this folder, so can't confirm "
                            "it holds no extra code (fix its permissions and check again)")
            continue
        for entry in entries:
            seen += 1
            if seen > MAX_FOLDER_ENTRIES:
                problems.append(f"more than {MAX_FOLDER_ENTRIES} files and folders; not an "
                                "honest worker folder (stopped looking)")
                return problems, extras, caches
            name = entry.name
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if name.startswith("."):
                continue
            try:
                is_link = entry.is_symlink()
                is_dir = not is_link and entry.is_dir(follow_symlinks=False)
            except OSError:
                problems.append(f"{rel}: can't be inspected")
                continue
            if is_dir:
                if name == "__pycache__":
                    caches.append(rel)
                elif name not in IGNORED_DIRS:
                    stack.append(rel)
                continue
            if rel in listed:
                continue  # compared against its signed hash in check_files
            if is_link:
                if name in IGNORED_DIRS:
                    continue
                problems.append(f"{rel}: link not in the signed release (a link can bring "
                                "in code from anywhere; Python follows it)")
            elif _is_code(name):
                problems.append(f"{rel}: Python code not in the signed release (Python can "
                                "import it)")
            else:
                extras.append(rel)
    return problems, extras, caches


def _check_cache_folder(root: Path, rel_dir: str, verified: dict[str, dict[str, Any]],
                        problems: list[str], notes: list[str]) -> None:
    """Every cache in one `__pycache__` that Python might load must be exactly what this
    Python builds from the checked source next to it."""
    folder = root / rel_dir
    parent = rel_dir.rsplit("/", 1)[0] if "/" in rel_dir else ""
    fix = ("delete the __pycache__ folders and check again (always safe: Python rebuilds "
           "them from the checked source)")
    unused = 0
    try:
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        problems.append(f"{rel_dir}/: can't read this folder, so can't check its caches; {fix}")
        return
    for entry in entries[:MAX_FOLDER_ENTRIES]:
        rel = f"{rel_dir}/{entry.name}"
        try:
            plain = entry.is_file(follow_symlinks=False)
        except OSError:
            plain = False
        if not plain:
            problems.append(f"{rel}: not a plain file inside a Python cache folder; {fix}")
            continue
        m = CACHE_NAME_RE.match(entry.name)
        source_rel = f"{parent}/{m['stem']}.py" if (m and parent) else (
            f"{m['stem']}.py" if m else "")
        if not m or source_rel not in verified:
            unused += 1  # Python never looks this name up (or its source isn't in the release)
            continue
        if m["tag"] != sys.implementation.cache_tag:
            problems.append(f"{rel}: compiled by another Python ({m['tag']}); this check can "
                            f"only compare {sys.implementation.cache_tag} caches; {fix}")
            continue
        src = verified[source_rel]
        if src.get("data") is None:
            continue  # its source already failed; that problem is reported
        expected = None
        fd = None
        try:
            fd = _open_regular(root / rel)
            with os.fdopen(fd, "rb") as fh:
                fd = None
                head = fh.read(16)
                if len(head) == 16:
                    flags = int.from_bytes(head[4:8], "little")
                    expected = expected_cache(src["data"], src["path"], src["mtime"], flags,
                                              int(m["opt"] or 0))
                # Reads at most one byte past the expected length: enough to see extra data.
                ok = expected is not None and head + fh.read(len(expected)) == expected
        except OSError:
            ok = False
        finally:
            if fd is not None:
                os.close(fd)
        if not ok:
            problems.append(f"{rel}: compiled cache doesn't match the checked source; Python "
                            f"could run it instead of {source_rel}; {fix}")
    if unused:
        notes.append(f"{rel_dir}/: {unused} cache file(s) Python won't load for this release "
                     "(other tools, or files no longer in the release); left alone")


def check_files(manifest: dict[str, Any], root: Path) -> tuple[list[str], list[str]]:
    """Compare the folder on disk with the manifest (`release-folder-v1`). Problems: a
    listed file missing, changed, linked or outside; anything Python could import that isn't
    listed (`.py`, `.pyc`, `.so`, `.pth`...); a link; a compiled cache that isn't exactly
    what this Python builds from the checked source; a folder that can't be read. Other
    extra files are notes (rigs keep logs and reports there)."""
    root = Path(root)
    problems: list[str] = []
    notes: list[str] = []
    resolved_root = root.resolve()
    verified: dict[str, dict[str, Any]] = {}
    for entry in manifest["files"]:
        rel = entry["path"]
        full = root / PurePosixPath(rel)
        try:
            inside = full.resolve().is_relative_to(resolved_root)
        except OSError:
            inside = False
        if full.is_symlink() or not inside:
            problems.append(f"{rel}: is a symlink or points outside the release folder")
            verified[rel] = {}
            continue
        if not full.exists():
            problems.append(f"{rel}: missing")
            verified[rel] = {}
            continue
        data = _read_regular(full, entry["size"])
        if data is None and not full.is_file():
            problems.append(f"{rel}: not a plain file (a pipe, device or folder)")
            verified[rel] = {}
            continue
        if data is None or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            problems.append(f"{rel}: contents differ from the signed release")
            verified[rel] = {}
            continue
        verified[rel] = {"data": data if rel.endswith(".py") else None,
                         "path": resolved_root / PurePosixPath(rel),
                         "mtime": full.stat().st_mtime}
    scan_problems, extras, caches = _scan_folder(root, set(verified))
    problems += scan_problems
    for rel_dir in caches:
        _check_cache_folder(root, rel_dir, verified, problems, notes)
    for rel in extras[:MAX_LISTED_NOTES]:
        notes.append(f"{rel}: extra file not in the release (not code; left alone)")
    if len(extras) > MAX_LISTED_NOTES:
        notes.append(f"... and {len(extras) - MAX_LISTED_NOTES} more extra files (not code)")
    return problems, notes


def verify_release(manifest_path: Path, signers_path: Path, *, sigs_path: Path | None = None,
                   root: Path | None = None, not_older_than: str | None = None) -> dict[str, Any]:
    """Full check. Returns a `bnh-release-check/1` report; `ok` is True only if every check
    passed. Raises ReleaseError for unusable inputs (missing files, bad signer list)."""
    signers = load_signers(signers_path)
    manifest = _load_json(manifest_path, "release manifest")
    sigs_path = Path(sigs_path) if sigs_path else sigs_path_for(manifest_path)
    sigs = _load_json(sigs_path, "signature file")
    problems = check_manifest(manifest)
    notes: list[str] = []
    report: dict[str, Any] = {"format": CHECK_FORMAT, "rule": RULE,
                              "threshold": signers["threshold"],
                              "signers_listed": len(signers["signers"]),
                              "valid_signers": [], "ok": False}
    if problems:
        report["problems"] = problems
        report["notes"] = notes
        return report
    report.update(name=manifest["name"], version=manifest["version"],
                  source_commit=manifest["source_commit"], files=len(manifest["files"]),
                  release_sha256=manifest_sha256(manifest))
    pinned = {s["name"]: s["public_key"] for s in signers["signers"]}
    fields = signed_fields(manifest)
    valid: list[str] = []
    if not isinstance(sigs, dict) or sigs.get("format") != SIGS_FORMAT \
            or not isinstance(sigs.get("signatures"), list):
        problems.append(f"signature file is not a {SIGS_FORMAT} file")
    else:
        if sigs.get("release_sha256") != report["release_sha256"]:
            problems.append("signature file names a different release_sha256 (the release "
                            "was changed after signing, or the files were mixed up)")
        counted_keys: set[str] = set()
        for i, entry in enumerate(sigs["signatures"]):
            if not isinstance(entry, dict):
                problems.append(f"signature #{i + 1} is not an object")
                continue
            who, key, sig = entry.get("signer"), entry.get("public_key"), entry.get("signature")
            if who not in pinned:
                notes.append(f"signature #{i + 1} from {who!r}: not on your signer list; "
                             "not counted")
                continue
            if key != pinned[who]:
                problems.append(f"signature #{i + 1} claims to be {who} but uses a different "
                                "key than your signer list pins")
                continue
            if not verify_manifest(fields, sig if isinstance(sig, str) else "", key):
                problems.append(f"signature #{i + 1} from {who} does not match this release")
                continue
            if who in valid or key in counted_keys:
                notes.append(f"signature #{i + 1}: {who} signed twice; counted once")
                continue
            valid.append(who)
            counted_keys.add(key)
    report["valid_signers"] = sorted(valid)
    if len(valid) < signers["threshold"]:
        problems.append(f"only {len(valid)} of the required {signers['threshold']} different "
                        "signers signed this release")
    if not_older_than is not None:
        try:
            floor = Version(not_older_than)
        except InvalidVersion as exc:
            raise ReleaseError(f"--not-older-than {not_older_than!r} is not a version") from exc
        if Version(manifest["version"]) < floor:
            problems.append(f"version {manifest['version']} is older than {not_older_than} "
                            "(an old signed release could bring back a fixed bug)")
    if root is not None:
        file_problems, file_notes = check_files(manifest, root)
        problems += file_problems
        notes += file_notes
        report["checked_root"] = True
        report["folder_rule"] = FOLDER_RULE
    else:
        report["checked_root"] = False
        notes.append("files on disk not checked (no --root given)")
    report["problems"] = problems
    report["notes"] = notes
    report["ok"] = not problems
    return report


def report_text(report: dict[str, Any]) -> str:
    # File names come from someone else's folder: print them without terminal control codes.
    lines = []
    if report.get("name"):
        lines.append(f"release {say(report['name'])} {say(report['version'])} "
                     f"(commit {report['source_commit'][:12]}, {report['files']} files)")
    lines.append(f"signers: {len(report['valid_signers'])} valid of {report['threshold']} "
                 f"required ({', '.join(report['valid_signers']) or 'none'})")
    for p in report["problems"]:
        lines.append(f"FAIL  {say(p, 600)}")
    for n in report["notes"]:
        lines.append(f"note  {say(n, 600)}")
    lines.append("OK: enough different signers signed exactly these files" if report["ok"]
                 else "NOT OK: don't run this release")
    return "\n".join(lines)


# ---- CLI --------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.release",
                                     description="Signed worker releases (several signers)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("keygen", help="make one signer's private key (outside git)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--name", required=True)
    p = sub.add_parser("make", help="list every git-tracked file with its sha256")
    p.add_argument("--root", type=Path, default=Path("."))
    p.add_argument("--name", default="bnh-worker")
    p.add_argument("--version", required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("sign", help="add your signature to a release")
    p.add_argument("manifest", type=Path)
    p.add_argument("--key", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--sigs", type=Path, default=None)
    p = sub.add_parser("verify", help="check signatures (and files with --root)")
    p.add_argument("manifest", type=Path)
    p.add_argument("--signers", type=Path, required=True)
    p.add_argument("--sigs", type=Path, default=None)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--not-older-than", default=None)
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "keygen":
            info = keygen(args.out, args.name)
            print(f"Created {args.out} (readable by you only). Never share or commit it.")
            print(f"fingerprint {info['fingerprint']}")
            print("Add this line to the signer list:")
            print(json.dumps({"name": info["name"], "public_key": info["public_key"]}))
            return 0
        if args.cmd == "make":
            if not NAME_RE.match(args.name):
                raise ReleaseError("--name must be lowercase letters, digits, '-' or '_'")
            manifest = make_manifest(args.root, args.name, args.version)
            sums = write_release(manifest, args.out)
            print(f"Wrote {args.out} and {sums}: {len(manifest['files'])} files from commit "
                  f"{manifest['source_commit'][:12]}.")
            print(f"release_sha256 {manifest_sha256(manifest)}")
            print("Next: each signer runs `python -m app.release sign` with their own key.")
            return 0
        if args.cmd == "sign":
            entry = sign_release(args.manifest, args.key, args.name, args.sigs)
            print(f"Signed as {entry['signer']} "
                  f"(key {fingerprint(entry['public_key'])}).")
            return 0
        report = verify_release(args.manifest, args.signers, sigs_path=args.sigs,
                                root=args.root, not_older_than=args.not_older_than)
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else report_text(report))
        return 0 if report["ok"] else 1
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
