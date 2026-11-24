"""Asymmetric job-manifest signatures (threat model #9).

The coordinator signs every job manifest with an Ed25519 private key that never leaves the
coordinator machine. Workers only hold the matching public key, so they can check a manifest
but cannot forge one. (The old HMAC scheme gave every worker the same secret, which also let
any worker sign fake jobs for other workers.)

Signature format stored on jobs and sent to workers: "ed25519:<128 hex chars>".

Print the coordinator's public key and fingerprint (creating the key if it is missing):
    python -m app.signing [--key-file ./data/coordinator-signing.key]
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import secret_file
from .security import canonical_json

SIGNATURE_PREFIX = "ed25519:"
PUBLIC_KEY_HEX_LEN = 64  # 32 raw bytes
SIGNATURE_HEX_LEN = 128  # 64 raw bytes


class SigningKeyError(Exception):
    """The coordinator's signing key file is missing, unreadable, unsafe or invalid."""


def fingerprint(public_key_hex: str) -> str:
    """Short, comparable name for a public key: sha256 of its raw bytes, first 16 hex chars,
    shown in 4-char groups so a person can compare it over the phone."""
    digest = hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()[:16]
    return "-".join(digest[i:i + 4] for i in range(0, 16, 4))


def parse_public_key(public_key_hex: str) -> Ed25519PublicKey:
    value = (public_key_hex or "").strip().lower()
    if len(value) != PUBLIC_KEY_HEX_LEN:
        raise ValueError("coordinator public key must be 64 hex characters (Ed25519)")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("coordinator public key must be hex") from exc
    return Ed25519PublicKey.from_public_bytes(raw)


class ManifestSigner:
    """Holds the coordinator's private key. Only the coordinator process creates one."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key
        raw = private_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                    serialization.PublicFormat.Raw)
        self.public_key_hex = raw.hex()
        self.fingerprint = fingerprint(self.public_key_hex)

    def __repr__(self) -> str:  # never print key material by accident
        return f"ManifestSigner(fingerprint={self.fingerprint!r})"

    def sign(self, fields: dict[str, Any]) -> str:
        return SIGNATURE_PREFIX + self._key.sign(canonical_json(fields)).hex()

    @classmethod
    def load(cls, path: Path) -> ManifestSigner:
        """Load an existing key; never create one. Used for the offline handover key, where a
        missing file means "plug in the USB stick", not "make a new key"."""
        path = Path(path)
        if not path.exists():
            raise SigningKeyError(f"{path} does not exist")
        return cls.load_or_create(path)

    @classmethod
    def load_or_create(cls, path: Path) -> ManifestSigner:
        """Load the key at `path`, or create it (readable by this user only) if absent.
        Refuses a key file that other users on the machine can read."""
        path = Path(path)
        if not path.exists():
            key = Ed25519PrivateKey.generate()
            pem = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            try:
                # Owner-only file in an owner-only new folder, never through a planted
                # link (`secret-file-v1`). The folder itself isn't required to be private:
                # the handover key may sit on a USB stick.
                secret_file.write_new(path, pem, "signing key", folder=False)
            except FileExistsError:
                return cls.load_or_create(path)  # another process created it first
            return cls(key)
        # `secret-file-v1`: checked on the open file (a pipe no longer hangs this, and the
        # file can't be swapped between the permission check and the read).
        try:
            data = secret_file.read(path, "signing key", secret=True, folder=False)
        except secret_file.UnsafeFileError as exc:
            raise SigningKeyError(str(exc)) from None
        try:
            key = serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError) as exc:
            raise SigningKeyError(f"{path} is not a readable PEM private key") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise SigningKeyError(f"{path} is not an Ed25519 key")
        return cls(key)


def verify_manifest(fields: dict[str, Any], signature: str, public_key_hex: str) -> bool:
    """True only if `signature` is an Ed25519 signature over exactly `fields` by the key
    `public_key_hex`. Anything malformed (old HMAC hex, wrong prefix, bad hex) is False."""
    if not isinstance(signature, str) or not signature.startswith(SIGNATURE_PREFIX):
        return False
    sig_hex = signature[len(SIGNATURE_PREFIX):]
    if len(sig_hex) != SIGNATURE_HEX_LEN:
        return False
    try:
        sig = bytes.fromhex(sig_hex)
        parse_public_key(public_key_hex).verify(sig, canonical_json(fields))
    except (ValueError, InvalidSignature):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    from .config import default_signing_key_path

    parser = argparse.ArgumentParser(description="Show the coordinator's public signing key")
    parser.add_argument("--key-file", type=Path, default=None,
                        help="private key file (default: COORDINATOR_SIGNING_KEY_FILE or "
                             "./data/coordinator-signing.key)")
    args = parser.parse_args(argv)
    path = args.key_file or default_signing_key_path()
    try:
        signer = ManifestSigner.load_or_create(path)
    except SigningKeyError as exc:
        print(f"error: {exc}")
        return 2
    print(f"public_key  {signer.public_key_hex}")
    print(f"fingerprint {signer.fingerprint}")
    print("Give workers the public key (BNH_COORDINATOR_KEY). Never share the key file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
