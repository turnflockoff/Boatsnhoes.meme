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

Offline handover key (`handover-key-v1`, threat model #9 follow-up): on its own, a
handover only helps for planned rotations. Someone who stole the coordinator's private key
file could sign a handover to a key of their own, and every worker would follow it. To close
that, the owner can make a second Ed25519 key that ONLY signs handovers and is kept offline
(a USB stick in a drawer), never on the coordinator machine:

    python -m app.key_rotation make-handover-key --out /media/usb/bnh-handover.key

Put its PUBLIC key in `COORDINATOR_HANDOVER_PUBLIC_KEY`. The coordinator then announces it
(signed by the coordinator key) and workers pin it. From then on a worker only follows a
handover that ALSO carries a valid signature by the pinned handover key, so the coordinator
key file alone is no longer enough to move workers. Rotating then needs the stick:

    python -m app.key_rotation rotate --handover-key-file /media/usb/bnh-handover.key

What it still does NOT protect against: a thief with the coordinator key can sign jobs until
you rotate, and workers that never pinned a handover key still follow any handover signed by
the old and new key. After a suspected theft, rotate with the handover key at once; workers
that have no handover key pinned need the new key out of band (`--coordinator-key`).

Stop the coordinator before rotating: a running coordinator keeps signing with the key it
loaded at start-up until it restarts.

    python -m app.key_rotation rotate  [--key-file ./data/coordinator-signing.key]
                                       [--handover-key-file /media/usb/bnh-handover.key]
    python -m app.key_rotation show    [--key-file ...]
    python -m app.key_rotation make-handover-key --out /media/usb/bnh-handover.key
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
# The coordinator key vouches for the handover key it uses, so a man in the middle can't
# slip a different one to a worker that is pinning it for the first time.
ANNOUNCE_PURPOSE = "bnh-handover-key-announcement/1"
HANDOVER_RULE = "handover-key-v1"


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


def follow_chain(rotations: list[dict[str, Any]], pinned: str, served: str,
                 handover_key: str | None = None) -> list[str]:
    """Return the keys from `pinned` to `served` (both included) if a valid chain of
    handovers connects them, else raise RotationError. Only forward steps count: a worker
    never moves back to a key that was already handed over.

    With `handover_key` (the offline key this worker pinned, `handover-key-v1`), every step
    the worker takes must ALSO be signed by that key. Steps before the worker's pinned key
    are not checked: they may be older than the handover key itself."""
    pinned, served = pinned.strip().lower(), served.strip().lower()
    check_history(rotations)
    if pinned == served:
        return [pinned]
    start = next((i for i, e in enumerate(rotations) if e["old_public_key"] == pinned), None)
    if start is None:
        raise RotationError(f"no handover starts from the pinned key {fingerprint(pinned)}")
    steps = rotations[start:]
    path = [pinned] + [e["new_public_key"] for e in steps]
    if path[-1] != served:
        raise RotationError(f"the handovers end on {fingerprint(path[-1])}, but the "
                            f"coordinator serves {fingerprint(served)}")
    if handover_key is not None:
        handover_key = handover_key.strip().lower()
        if handover_key in path:
            raise RotationError("the handover key is also a coordinator key on this chain")
        for entry in steps:
            sig = str((entry["signatures"] or {}).get("handover", ""))
            if not verify_manifest(statement_fields(entry), sig, handover_key):
                raise RotationError(f"handover {entry['sequence']} is not signed by the "
                                    f"pinned handover key {fingerprint(handover_key)}")
    return path


def announcement(signer: ManifestSigner, handover_public_key: str) -> dict[str, Any]:
    """What the coordinator serves so workers can pin its offline handover key: the handover
    public key, vouched for by the coordinator's current signing key."""
    fields = {"purpose": ANNOUNCE_PURPOSE, "coordinator_public_key": signer.public_key_hex,
              "handover_public_key": handover_public_key}
    return fields | {"fingerprint": fingerprint(handover_public_key),
                     "signature": signer.sign(fields)}


def check_announcement(announced: Any, coordinator_key: str) -> str:
    """Return the handover public key in `announced` if the coordinator key the worker trusts
    signed it, else raise RotationError."""
    if not isinstance(announced, dict):
        raise RotationError("no handover key announcement")
    coordinator_key = coordinator_key.strip().lower()
    fields = {"purpose": announced.get("purpose"),
              "coordinator_public_key": announced.get("coordinator_public_key"),
              "handover_public_key": announced.get("handover_public_key")}
    if fields["purpose"] != ANNOUNCE_PURPOSE:
        raise RotationError("handover key announcement has the wrong purpose")
    if fields["coordinator_public_key"] != coordinator_key:
        raise RotationError("handover key announcement names a different coordinator key")
    handover = str(fields["handover_public_key"] or "")
    try:
        parse_public_key(handover)
    except ValueError as exc:
        raise RotationError(f"announced handover key: {exc}") from exc
    if handover == coordinator_key:
        raise RotationError("announced handover key is the coordinator key itself")
    if not verify_manifest(fields, str(announced.get("signature", "")), coordinator_key):
        raise RotationError("handover key announcement is not signed by the coordinator key")
    return handover


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


def rotate(key_path: Path, now: int, handover: ManifestSigner | None = None,
           required_handover_key: str | None = None) -> dict[str, Any]:
    """Replace the coordinator key at `key_path` with a new one and record a handover signed
    by both (and by the offline `handover` key, if given). Safe to re-run after a crash: it
    finishes a half-done rotation instead of starting a second one. Returns the handover that
    is now in force.

    `required_handover_key` is the public handover key the coordinator announces
    (`COORDINATOR_HANDOVER_PUBLIC_KEY`). When it is set, the rotation is refused unless
    `handover` is that key: workers that pinned it would refuse an unsigned handover and stop."""
    key_path = Path(key_path)
    if not key_path.exists():
        raise SigningKeyError(f"{key_path} does not exist; start the coordinator once (or run "
                              "`python -m app.signing`) to create the first key")
    if required_handover_key:
        if handover is None:
            raise SigningKeyError(
                "COORDINATOR_HANDOVER_PUBLIC_KEY is set, so workers expect every handover to "
                "be signed by the offline handover key; pass --handover-key-file")
        if handover.public_key_hex != required_handover_key.strip().lower():
            raise SigningKeyError(
                f"--handover-key-file is {handover.fingerprint}, but the coordinator announces "
                f"{fingerprint(required_handover_key.strip().lower())}")
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

    if handover is not None and handover.public_key_hex in (
            {old.public_key_hex} | {k for e in rotations
                                    for k in (e["old_public_key"], e["new_public_key"])}):
        raise SigningKeyError("the handover key must be a separate key, never a coordinator key")
    new_key = Ed25519PrivateKey.generate()
    _write_private_key(next_file, new_key)
    new = ManifestSigner(new_key)
    statement = {"purpose": ROTATION_PURPOSE, "sequence": len(rotations) + 1,
                 "old_public_key": old.public_key_hex, "new_public_key": new.public_key_hex,
                 "rotated_at": int(now)}
    signatures = {"old": old.sign(statement), "new": new.sign(statement)}
    entry = statement | {"old_fingerprint": old.fingerprint,
                         "new_fingerprint": new.fingerprint, "signatures": signatures}
    if handover is not None:
        signatures["handover"] = handover.sign(statement)
        entry["handover_fingerprint"] = handover.fingerprint
    check_history(rotations + [entry])
    _write_history(hist_file, rotations + [entry])
    os.replace(next_file, key_path)  # the old private key is gone after this line
    return entry


def make_handover_key(out: Path, signing_key_path: Path) -> ManifestSigner:
    """Create the offline handover key at `out` (readable by this user only). Refuses to
    overwrite a file, and refuses the coordinator key's own folder: a key that sits next to
    the signing key is stolen together with it, which defeats the point."""
    out = Path(out)
    if out.resolve().parent == Path(signing_key_path).resolve().parent:
        raise SigningKeyError(f"{out.parent} also holds the coordinator signing key; put the "
                              "handover key somewhere offline (e.g. a USB stick) instead")
    if out.exists():
        raise SigningKeyError(f"{out} already exists; refusing to overwrite a key")
    out.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    _write_private_key(out, key)
    return ManifestSigner(key)


def main(argv: list[str] | None = None) -> int:
    import time

    from .config import default_signing_key_path, handover_public_key_env

    parser = argparse.ArgumentParser(description="Rotate or show the coordinator signing key")
    parser.add_argument("action", choices=("rotate", "show", "make-handover-key"))
    parser.add_argument("--key-file", type=Path, default=None,
                        help="private key file (default: COORDINATOR_SIGNING_KEY_FILE or "
                             "./data/coordinator-signing.key)")
    parser.add_argument("--handover-key-file", type=Path, default=None,
                        help="offline handover key (rotate: signs the handover with it)")
    parser.add_argument("--out", type=Path, default=None,
                        help="make-handover-key: where to write the new offline key")
    args = parser.parse_args(argv)
    path = args.key_file or default_signing_key_path()
    try:
        if args.action == "make-handover-key":
            if args.out is None:
                print("error: pass --out (somewhere offline, e.g. a USB stick)")
                return 2
            signer = make_handover_key(args.out, path)
            print(f"handover key  {signer.fingerprint} saved in {args.out}")
            print("Keep this file OFFLINE (unplug the stick). It is only needed to rotate.")
            print("Then add this line to prototype/.env and restart the coordinator:")
            print(f"COORDINATOR_HANDOVER_PUBLIC_KEY={signer.public_key_hex}")
            return 0
        if args.action == "rotate":
            handover = (ManifestSigner.load(args.handover_key_file)
                        if args.handover_key_file else None)
            required = handover_public_key_env()
            entry = rotate(path, int(time.time()), handover, required)
            print(f"rotated     {entry['old_fingerprint']} -> {entry['new_fingerprint']}")
            print(f"handover    #{entry['sequence']} saved in {history_path(path)}")
            if handover is not None:
                print(f"also signed by the offline handover key {handover.fingerprint}; "
                      "unplug it again")
                if not required:
                    print("Note: COORDINATOR_HANDOVER_PUBLIC_KEY isn't set, so workers "
                          "haven't pinned this handover key yet")
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
            extra = (f"  + handover key {e.get('handover_fingerprint', '?')}"
                     if "handover" in e["signatures"] else "")
            print(f"handover #{e['sequence']}  {fingerprint(e['old_public_key'])} -> "
                  f"{fingerprint(e['new_public_key'])}  at {e['rotated_at']}{extra}")
        if not rotations:
            print("no rotations yet")
        return 0
    except SigningKeyError as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
