"""`public-counts-v1` (threat model #14): batched public job counts.

The public views (`GET /health`, `GET /v1/ledger`) show how many jobs are queued, leased,
accepted and so on. Shown live, they gave away re-checks: when a rig's result is picked
for one, the coordinator queues the re-check the instant the result is accepted, so
`queued` went up by one right after that rig's submit and not after the others. Leaving
re-checks out of the counts would only move the tell to the auditor, who would see the
counts *not* move when it claimed one.

So the counts are read once per clock-aligned window (`interval` seconds, e.g. 12:00:00,
12:15:00, ...) and the same numbers are repeated until the next window starts, for
everyone and every kind of job. A rig watching them sees at most one change per window,
the sum of everything that happened in it. Windows follow the clock, not the requests, so
asking at a clever moment can't restart one. The operator's live view doesn't use this.

One snapshot per coordinator process. Run a single process (the prototype does): separate
processes would each take their own snapshot at different moments.
"""
from __future__ import annotations

import copy
import threading
from typing import Any, Callable

RULE = "public-counts-v1"


class BatchedCounts:
    def __init__(self, interval: int, read: Callable[[], Any]) -> None:
        if interval < 0:
            raise ValueError("interval must be 0 (live) or a positive number of seconds")
        self.interval = interval
        self._read = read
        self._lock = threading.Lock()
        self._window: int | None = None
        self._counts: Any = {}
        self._taken_at = 0

    def take(self, now: int) -> tuple[Any, int, int | None]:
        """This window's reading, when it was taken, and when the next window starts
        (None when live). Reads at most once per window, however often it is asked
        (`ledger-pages-v1` also uses this for the public consistency check, which reads
        the whole ledger)."""
        if not self.interval:
            return self._read(), now, None
        window = now // self.interval
        with self._lock:
            if self._window != window:
                # Read before switching windows: if the read fails, nothing is cached.
                self._counts, self._taken_at, self._window = self._read(), now, window
            value, taken_at = copy.deepcopy(self._counts), self._taken_at
        return value, taken_at, (window + 1) * self.interval

    def view(self, now: int) -> dict[str, Any]:
        """The counts to show the public at `now`, and when they were taken."""
        counts, taken_at, next_at = self.take(now)
        return {"jobs": counts, "jobs_counted_at": taken_at, "jobs_next_count_at": next_at,
                "public_counts_interval_seconds": self.interval}


def live(read: Callable[[], dict[str, int]], now: int) -> dict[str, Any]:
    """The operator's view: same shape, always current."""
    return BatchedCounts(0, read).view(now)
