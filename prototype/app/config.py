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
    )
