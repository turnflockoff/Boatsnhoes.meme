from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: dict[str, Any]) -> bytes:
    """The exact bytes that get signed: sorted keys, no spaces. Signer and verifier must agree."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
