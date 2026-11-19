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


def get_settings() -> Settings:
    secret = os.getenv("PROTOTYPE_SECRET", "local-demo-only-change-me")
    db = Path(os.getenv("DATABASE_PATH", "./data/prototype.sqlite3"))
    if len(secret) < 24:
        raise ValueError("PROTOTYPE_SECRET must be at least 24 characters")
    return Settings(secret=secret, database_path=db)
