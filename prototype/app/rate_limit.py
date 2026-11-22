"""Per-client request limits and a lock-out after repeated failed logins.

Two simple rules, both kept in memory (a restart clears them):

1. **Request rate** (`RATE_LIMIT_PER_MINUTE`, default 240, 0 = off). Each client address
   gets a bucket that holds that many requests and refills at the same rate per minute.
   An empty bucket gets `429 Too Many Requests` with a `Retry-After` header. This stops one
   machine from flooding the coordinator with polls or junk submissions.
2. **Failed logins** (`AUTH_FAILURE_LIMIT`, default 10, 0 = off, within
   `AUTH_FAILURE_WINDOW_SECONDS`, default 600). A wrong worker key, operator token,
   submitter token or enrollment code counts as one failure. A client with that many
   failures in the window is refused (`429`) on every endpoint until the oldest failure
   falls out of the window. A *missing* credential doesn't count: that's a setup mistake,
   not a guess.

Worker keys and enrollment codes are 256-bit random values, so nobody guesses those. The
lock-out matters for the operator and submitter tokens, which the owner types in and which
only need to be 24 characters.

`/health` is never limited, so a monitor can't be locked out by someone else's traffic.

Limits:
- "Client" means the address the connection came from, or, behind a reverse proxy listed
  in `TRUSTED_PROXIES`, the address that proxy reports (app/client_address.py). Behind a
  home router doing NAT, many machines share one address and share its limits. One rig with a
  bad key could lock out the other rigs on the same address for the window.
- Someone with many addresses gets a bucket per address. This slows a flood; it doesn't
  stop a distributed one (that needs the hosting provider's DDoS protection).
- Logs never contain the address. They carry a short `client_tag` (a keyed hash with a
  random key made at start-up), so one client's lines can be grouped without writing its
  address to disk. The operator health page, which needs the operator token, shows the
  address of a locked-out client so the operator can block it in a firewall.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

# Endpoints that are never limited (liveness checks for monitors / load balancers).
EXEMPT_PATHS = frozenset({"/health"})

# Forget clients idle this long once the table gets big, so it can't grow without bound.
_PRUNE_AT = 10_000


@dataclass
class _Client:
    tokens: float
    updated: float
    failures: deque[float] = field(default_factory=deque)


class RateLimiter:
    def __init__(self, per_minute: int, auth_failure_limit: int, auth_failure_window: int,
                 clock: Callable[[], float]) -> None:
        self.per_minute = per_minute
        self.auth_failure_limit = auth_failure_limit
        self.auth_failure_window = auth_failure_window
        self.clock = clock
        self._clients: dict[str, _Client] = {}
        self._lock = threading.Lock()
        self._tag_key = secrets.token_bytes(32)
        self.limited_requests = 0
        self.locked_out_requests = 0
        self.auth_failures = 0

    def tag(self, client: str) -> str:
        """A short, stable-until-restart label for a client that doesn't reveal its address."""
        return hmac.new(self._tag_key, client.encode(), hashlib.sha256).hexdigest()[:12]

    def _get(self, client: str, t: float) -> _Client:
        entry = self._clients.get(client)
        if entry is None:
            if len(self._clients) >= _PRUNE_AT:
                self._prune(t)
            entry = _Client(tokens=float(self.per_minute), updated=t)
            self._clients[client] = entry
        return entry

    def _prune(self, t: float) -> None:
        idle = max(60.0, float(self.auth_failure_window))
        for name in [n for n, c in self._clients.items()
                     if t - c.updated > idle and not c.failures]:
            del self._clients[name]

    def _drop_old_failures(self, entry: _Client, t: float) -> None:
        while entry.failures and entry.failures[0] <= t - self.auth_failure_window:
            entry.failures.popleft()

    def _locked_for(self, entry: _Client, t: float) -> float:
        """Seconds until this client may try again after too many failures (0 = not locked)."""
        if not self.auth_failure_limit:
            return 0.0
        self._drop_old_failures(entry, t)
        if len(entry.failures) < self.auth_failure_limit:
            return 0.0
        # Locked until enough old failures leave the window to drop below the limit.
        release = entry.failures[len(entry.failures) - self.auth_failure_limit]
        return max(0.0, release + self.auth_failure_window - t)

    def check(self, client: str, path: str) -> tuple[str, int] | None:
        """Call once per request. None = go ahead; otherwise (reason, retry_after_seconds)
        with reason "locked_out" or "rate_limited"."""
        if path in EXEMPT_PATHS:
            return None
        with self._lock:
            t = self.clock()
            entry = self._get(client, t)
            locked = self._locked_for(entry, t)
            if locked > 0:
                self.locked_out_requests += 1
                return "locked_out", max(1, math.ceil(locked))
            if not self.per_minute:
                entry.updated = t
                return None
            rate = self.per_minute / 60.0
            entry.tokens = min(float(self.per_minute),
                               entry.tokens + max(0.0, t - entry.updated) * rate)
            entry.updated = t
            if entry.tokens < 1.0:
                self.limited_requests += 1
                return "rate_limited", max(1, math.ceil((1.0 - entry.tokens) / rate))
            entry.tokens -= 1.0
            return None

    def record_auth_failure(self, client: str) -> bool:
        """Count one wrong credential. Returns True if this failure locked the client out."""
        with self._lock:
            t = self.clock()
            entry = self._get(client, t)
            self.auth_failures += 1
            if not self.auth_failure_limit:
                return False
            self._drop_old_failures(entry, t)
            entry.failures.append(t)
            return len(entry.failures) == self.auth_failure_limit

    def snapshot(self) -> dict[str, Any]:
        """Operator-only view: totals since start and clients locked out right now."""
        with self._lock:
            t = self.clock()
            locked = []
            for name, entry in self._clients.items():
                seconds = self._locked_for(entry, t)
                if seconds > 0:
                    locked.append({"client": name, "client_tag": self.tag(name),
                                   "failures_in_window": len(entry.failures),
                                   "retry_after_seconds": max(1, math.ceil(seconds))})
            return {
                "rate_limit_per_minute": self.per_minute,
                "auth_failure_limit": self.auth_failure_limit,
                "auth_failure_window_seconds": self.auth_failure_window,
                "since_start": {"rate_limited_requests": self.limited_requests,
                                "locked_out_requests": self.locked_out_requests,
                                "auth_failures": self.auth_failures},
                "locked_out_clients": sorted(locked, key=lambda c: c["client_tag"]),
                "note": "In memory: a restart clears these counts and lock-outs.",
            }
