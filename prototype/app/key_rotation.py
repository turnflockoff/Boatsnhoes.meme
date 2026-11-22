"""Planned rotation of the coordinator's signing key (threat model #9, follow-up).

Workers pin the coordinator's public key and refuse any change, so a fake coordinator can't
just serve its own key. That also meant there was no safe way to *replace* the key: every
worker had to be told the new one by hand. A rotation fixes that for planned changes:

1. A new Ed25519 key is made.
2. A short "handover" statement is written: old public key -> new public key, with a
   sequence number and time. It is signed by BOTH keys: the old key says "I hand over to
   this key", the new key proves the new key really exists and agrees.
3. The statement is added to a public history file next to the key, then the new key
   replaces the old one. The old private key is not kept.

A worker that pinned the old key sees the coordinator serve a new one, checks the chain of
handovers from its pinned key to the served key, and only then updates its pin.

What it does NOT protect against: if someone stole the OLD private key, they can sign a
handover to a key of their own. A handover only helps for planned rotations. After a
suspected theft, give every worker the new key out of band (`--coordinator-key`) and tell
them not to trust handovers signed by the stolen key.

Stop the coordinator before rotating: a running coordinator keeps signing with the key it
loaded at start-up until it restarts.

    python -m app.key_rotation rotate  [--key-file ./data/coordinator-signing.key]
    python -m app.key_rotation show    [--key-file ...]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .signing import ManifestSigner, SigningKeyError, fingerprint, parse_public_key, \
    verify_manifest

HISTORY_FORMAT = "bnh-key-history/1"
ROTATION_PURPOSE = "bnh-coordinator-key-rotation/1"
STATEMENT_FIELDS = ("purpose", "sequence", "old_public_key", "new_public_key", "rotated_at")


class RotationError(Exception):
    """A handover chain is missing, broken, forged or doesn't connect two keys."""


def history_path(key_path: Path) -> Path:
    """Public history file kept next to the private key (it holds no secrets)."""
    key_path = Path(key_path)
    return key_path.with_name(key_path.name + ".history.json")


def pending_path(key_path: Path) -> Path:
    """Where a new key waits between writing the handover and swapping it in."""
    key_path = Path(key_path)
    return key_path.with_name(key_path.name + ".next")


# -- reading and checking ----------------------------------------------------------------
def load_history(path: Path) -> list[dict[str, Any]]:
    """The list of handovers in `path` (empty if the file doesn't exist)."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RotationError(f"{path} is not readable JSON") from exc
    if not isinstance(data, dict) or data.get("format") != HISTORY_FORMAT \
            or not isinstance(data.get("rotations"), list):
        raise RotationError(f"{path} is not a {HISTORY_FORMAT} file")
    return data["rotations"]


def statement_fields(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: entry[k] for k in STATEMENT_FIELDS}


def check_history(rotations: list[dict[str, Any]]) -> None:
    """Raise RotationError unless every handover is well formed, signed by both its old and
    new key, numbered 1, 2, 3..., links to the one before it, and never reuses a key."""
    seen: set[str] = set()
    previous_new: str | None = None
    for index, entry in enumerate(rotations, start=1):
        if not isinstance(entry, dict):
            raise RotationError(f"handover {index} is not an object")
        missing = [k for k in (*STATEMENT_FIELDS, "signatures") if k not in entry]
        if missing:
            raise RotationError(f"handover {index} is missing {', '.join(missing)}")
        if entry["purpose"] != ROTATION_PURPOSE:
            raise RotationError(f"handover {index} has the wrong purpose")
        if entry["sequence"] != index:
            raise RotationError(f"handover {index} is numbered {entry['sequence']!r}")
        old, new = str(entry["old_public_key"]), str(entry["new_public_key"])
        for key in (old, new):
            try:
                parse_public_key(key)
            except ValueError as exc:
                raise RotationError(f"handover {index}: {exc}") from exc
        if previous_new is not None and old != previous_new:
            raise RotationError(f"handover {index} does not start from the key handover "
                                f"{index - 1} ended on")
        if old == new or new in seen:
            raise RotationError(f"handover {index} reuses an earlier key")
        seen.update((old, new))
        sigs = entry["signatures"]
        if not isinstance(sigs, dict):
            raise RotationError(f"handover {index} has no signatures")
        fields = statement_fields(entry)
        if not verify_manifest(fields, str(sigs.get("old", "")), old):
            raise RotationError(f"handover {index} is not signed by the old key "
                                f"{fingerprint(old)}")
        if not verify_manifest(fields, str(sigs.get("new", "")), new):
            raise RotationError(f"handover {index} is not signed by the new key "
                                f"{fingerprint(new)}")
        previous_new = new


def follow_chain(rotations: list[dict[str, Any]], pinned: str, served: str) -> list[str]:
    """Return the keys from `pinned` to `served` (both included) if a valid chain of
    handovers connects them, else raise RotationError. Only forward steps count: a worker
    never moves back to a key that was already handed over."""
    pinned, served = pinned.strip().lower(), served.strip().lower()
    check_history(rotations)
    if pinned == served:
        return [pinned]
    start = next((i for i, e in enumerate(rotations) if e["old_public_key"] == pinned), None)
    if start is None:
        raise RotationError(f"no handover starts from the pinned key {fingerprint(pinned)}")
    path = [pinned] + [e["new_public_key"] for e in rotations[start:]]
    if path[-1] != served:
        raise RotationError(f"the handovers end on {fingerprint(path[-1])}, but the "
                            f"coordinator serves {fingerprint(served)}")
    return path


def retired_keys(rotations: list[dict[str, Any]]) -> set[str]:
    """Every public key this coordinator has handed over from (checked chain only)."""
    check_history(rotations)
    return {e["old_public_key"] for e in rotations}


def check_current_key(key_path: Path, current_public_key: str) -> list[dict[str, Any]]:
    """Called when the coordinator starts. Returns the checked history, or raises
    SigningKeyError if the history doesn't end on the key that was just loaded (for example
    a rotation was interrupted, or the key file was swapped by hand)."""
    try:
        rotations = load_history(history_path(key_path))
        check_history(rotations)
    except RotationError as exc:
        raise SigningKeyError(f"key history problem: {exc}") from exc
    if rotations and rotations[-1]["new_public_key"] != current_public_key:
        hint = ("a rotation was interrupted; run `python -m app.key_rotation rotate` to "
                "finish it" if pending_path(key_path).exists() else
                "the key file doesn't match its handover history; restore the right key file")
        raise SigningKeyError(f"{key_path}: {hint}")
    return rotations


# -- rotating ----------------------------------------------------------------------------
def _write_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
        fh.flush()
        os.fsync(fh.fileno())


def _write_history(path: Path, rotations: list[dict[str, Any]]) -> None:
    """Replace the history file in one step (write a temporary file, then rename)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as fh:
        json.dump({"format": HISTORY_FORMAT, "rotations": rotations}, fh, indent=2,
                  sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def rotate(key_path: Path, now: int) -> dict[str, Any]:
    """Replace the coordinator key at `key_path` with a new one and record a handover signed
    by both. Safe to re-run after a crash: it finishes a half-done rotation instead of
    starting a second one. Returns the handover that is now in force."""
    key_path = Path(key_path)
    if not key_path.exists():
        raise SigningKeyError(f"{key_path} does not exist; start the coordinator once (or run "
                              "`python -m app.signing`) to create the first key")
    hist_file, next_file = history_path(key_path), pending_path(key_path)
    try:
        rotations = load_history(hist_file)
        check_history(rotations)
    except RotationError as exc:
        raise SigningKeyError(f"key history problem: {exc}") from exc
    old = ManifestSigner.load_or_create(key_path)

    if next_file.exists():
        pending = ManifestSigner.load_or_create(next_file)
        if rotations and rotations[-1]["new_public_key"] == pending.public_key_hex \
                and rotations[-1]["old_public_key"] == old.public_key_hex:
            os.replace(next_file, key_path)  # history already written: finish the swap
            return rotations[-1]
        next_file.unlink()  # crashed before the history was written: start again

    new_key = Ed25519PrivateKey.generate()
    _write_private_key(next_file, new_key)
    new = ManifestSigner(new_key)
    statement = {"purpose": ROTATION_PURPOSE, "sequence": len(rotations) + 1,
                 "old_public_key": old.public_key_hex, "new_public_key": new.public_key_hex,
                 "rotated_at": int(now)}
    entry = statement | {"old_fingerprint": old.fingerprint,
                         "new_fingerprint": new.fingerprint,
                         "signatures": {"old": old.sign(statement), "new": new.sign(statement)}}
    check_history(rotations + [entry])
    _write_history(hist_file, rotations + [entry])
    os.replace(next_file, key_path)  # the old private key is gone after this line
    return entry


def main(argv: list[str] | None = None) -> int:
    import time

    from .config import default_signing_key_path

    parser = argparse.ArgumentParser(description="Rotate or show the coordinator signing key")
    parser.add_argument("action", choices=("rotate", "show"))
    parser.add_argument("--key-file", type=Path, default=None,
                        help="private key file (default: COORDINATOR_SIGNING_KEY_FILE or "
                             "./data/coordinator-signing.key)")
    args = parser.parse_args(argv)
    path = args.key_file or default_signing_key_path()
    try:
        if args.action == "rotate":
            entry = rotate(path, int(time.time()))
            print(f"rotated     {entry['old_fingerprint']} -> {entry['new_fingerprint']}")
            print(f"handover    #{entry['sequence']} saved in {history_path(path)}")
            print("Restart the coordinator now. Workers that pinned the old key will follow "
                  "the signed handover; workers given a key by hand need the new one:")
            print(f"public_key  {entry['new_public_key']}")
            return 0
        if not path.exists():
            print(f"error: {path} does not exist")
            return 2
        signer = ManifestSigner.load_or_create(path)
        rotations = check_current_key(path, signer.public_key_hex)
        print(f"current     {signer.fingerprint}")
        for e in rotations:
            print(f"handover #{e['sequence']}  {fingerprint(e['old_public_key'])} -> "
                  f"{fingerprint(e['new_public_key'])}  at {e['rotated_at']}")
        if not rotations:
            print("no rotations yet")
        return 0
    except SigningKeyError as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
