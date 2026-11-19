from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(fields: dict[str, Any], secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical_json(fields), hashlib.sha256).hexdigest()


def verify_manifest(fields: dict[str, Any], signature: str, secret: str) -> bool:
    expected = sign_manifest(fields, secret)
    return hmac.compare_digest(expected, signature)
