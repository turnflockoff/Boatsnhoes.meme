from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    secret: str
    database_path: Path
    lease_seconds: int = 60
    max_attempts: int = 3
    # Phase 4: share of "schema-only" results re-run by a different worker (0.0-1.0).
    audit_rate: float = 0.25
    # Required for operator endpoints (challenges, audits, trust). Unset = those are disabled.
    operator_token: str | None = None
    # Phase 4: when a re-run disagrees, ask a third worker to break the tie.
    tiebreak: bool = True
    # Threat model #4: audits go to a random active worker, not whoever polls first.
    # A worker counts as active this long after its last job request.
    auditor_active_seconds: int = 300
    # How long the chosen worker has to pick up its audit before it is re-assigned.
    audit_assign_seconds: int = 120


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _rate_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false")


def get_settings() -> Settings:
    secret = os.getenv("PROTOTYPE_SECRET", "local-demo-only-change-me")
    db = Path(os.getenv("DATABASE_PATH", "./data/prototype.sqlite3"))
    if len(secret) < 24:
        raise ValueError("PROTOTYPE_SECRET must be at least 24 characters")
    operator_token = os.getenv("OPERATOR_TOKEN") or None
    if operator_token is not None and len(operator_token) < 24:
        raise ValueError("OPERATOR_TOKEN must be at least 24 characters")
    return Settings(
        secret=secret,
        database_path=db,
        lease_seconds=_int_env("LEASE_SECONDS", 60, 5, 3600),
        max_attempts=_int_env("MAX_ATTEMPTS", 3, 1, 20),
        audit_rate=_rate_env("AUDIT_RATE", 0.25),
        operator_token=operator_token,
        tiebreak=_bool_env("TIEBREAK", True),
        auditor_active_seconds=_int_env("AUDITOR_ACTIVE_SECONDS", 300, 10, 86400),
        audit_assign_seconds=_int_env("AUDIT_ASSIGN_SECONDS", 120, 5, 86400),
    )
