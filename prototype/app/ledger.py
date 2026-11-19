from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS submissions (
                    job_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    reward_points INTEGER NOT NULL,
                    accepted_at INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    capabilities TEXT NOT NULL,
                    registered_at INTEGER NOT NULL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def register_worker(self, worker_id: str, capabilities: str, now: int) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO workers(worker_id, capabilities, registered_at) VALUES(?,?,?) "
                "ON CONFLICT(worker_id) DO UPDATE SET capabilities=excluded.capabilities",
                (worker_id, capabilities, now),
            )

    def has_worker(self, worker_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM workers WHERE worker_id=?", (worker_id,)).fetchone() is not None

    def record_submission(self, job_id: str, worker_id: str, result_hash: str, points: int, now: int) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO submissions(job_id, worker_id, result_hash, reward_points, accepted_at) "
                "VALUES(?,?,?,?,?)",
                (job_id, worker_id, result_hash, points, now),
            )

    def entries(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT job_id, worker_id, result_hash, reward_points, accepted_at "
                "FROM submissions ORDER BY accepted_at, job_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def total_points(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT COALESCE(SUM(reward_points),0) AS total FROM submissions").fetchone()
            return int(row["total"])
