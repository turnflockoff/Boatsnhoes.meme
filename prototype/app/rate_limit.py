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
3. **Guesses at once** (`guess-slot-v1`, since 0.5.62). A request that carries an operator
   or submitter token is a possible guess until the coordinator has checked it. Failures are
   only counted when a check *finishes*, so before this rule a client could send hundreds of
   wrong tokens at the same moment and every one was checked before the first failure was
   counted (230 at once were all checked against a limit of 10). Now each such request takes
   a "guess slot" first: failures in the window plus slots in use may not go above
   `AUTH_FAILURE_LIMIT`. A request without a free slot gets `429` straight away and its token
   is never looked at. Worker keys and enrollment codes don't take slots: they are 256-bit
   random values nobody can guess, and rigs behind one home router may poll at the same
   moment.

Worker keys and enrollment codes are 256-bit random values, so nobody guesses those. The
lock-out matters for the operator and submitter tokens, which the owner types in and which
only need to be 24 characters.

`/health` is never limited, so a monitor can't be locked out by someone else's traffic.

4. **One home network, one client** (`client-prefix-v1`, since 0.5.72). An IPv6 home or
   server connection is handed a whole block of addresses (usually a /64: about 18 billion
   billion of them), and the machine can pick a new one for every request. Counted per full
   address, each request got a fresh bucket and a fresh set of 10 guesses (300 wrong operator
   tokens from one /64 through a trusted proxy were all checked, none refused). Now IPv6
   clients are counted per block of `IPV6_CLIENT_PREFIX` bits (default 64, the size one
   connection gets; 128 = per full address as before). IPv4 stays per address.
5. **A table that can't be blown up** (`client-prefix-v1`). Clients are kept in order of
   last use. Each new client first looks at the two oldest entries and forgets any that is
   exactly like a new one (bucket full again, no failures in the window, no guess in
   progress), so forgetting changes nothing. Before this, once 10,000 clients were known,
   every new address scanned the whole table while holding the lock that every request
   waits for: the second 10,000 addresses took 31 s instead of 0.1 s. The table never holds
   more than `RATE_LIMIT_MAX_CLIENTS` (default 50,000) entries; if it is full of clients that
   can't be forgotten yet, new clients share one "overflow" bucket and lock-out (fail closed:
   slower for newcomers, never extra guesses), and operator health warns.

Limits:
- "Client" means the address the connection came from, or, behind a reverse proxy listed
  in `TRUSTED_PROXIES`, the address that proxy reports (app/client_address.py), grouped as
  above. Behind a home router doing NAT (IPv4) or on one IPv6 /64, many machines share one
  client and share its limits. One rig with a bad key could lock out the other rigs on the
  same network for the window.
- Someone with many IPv6 blocks (a provider may hand out a /56 or /48: 256 or 65,536 /64s)
  still gets a bucket and 10 guesses per block. That is the same position as someone with
  that many IPv4 addresses; the operator token's length (at least 24 characters, random) is
  what makes guessing hopeless, the lock-out only slows it down.
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
import ipaddress
import math
import secrets
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

# Endpoints that are never limited (liveness checks for monitors / load balancers).
EXEMPT_PATHS = frozenset({"/health"})

# `client-prefix-v1`: how many of the oldest entries each new client looks at, to forget
# those that are exactly like a new entry. Two is enough to keep up: each new client adds one.
_SWEEP = 2
DEFAULT_IPV6_PREFIX = 64
DEFAULT_MAX_CLIENTS = 50_000
# The shared entry new clients use while the table is full of clients it can't forget yet.
OVERFLOW = "(overflow)"


def client_key(client: str, ipv6_prefix: int = DEFAULT_IPV6_PREFIX) -> str:
    """The name limits are counted under (`client-prefix-v1`). An IPv6 address becomes its
    block of `ipv6_prefix` bits (`2001:db8:1:2::/64`); IPv4 (also IPv4 written as IPv6)
    stays the address. Anything that isn't an address (a test client's name) is kept as is."""
    try:
        addr = ipaddress.ip_address(client.split("%", 1)[0])
    except ValueError:
        return client
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        if ipv6_prefix < 128:
            drop = 128 - ipv6_prefix
            return str(ipaddress.IPv6Network(((int(addr) >> drop) << drop, ipv6_prefix)))
    return str(addr)


@dataclass
class _Client:
    tokens: float
    updated: float
    failures: deque[float] = field(default_factory=deque)
    guessing: int = 0  # requests with a guessable token still being checked


class RateLimiter:
    def __init__(self, per_minute: int, auth_failure_limit: int, auth_failure_window: int,
                 clock: Callable[[], float], ipv6_prefix: int = DEFAULT_IPV6_PREFIX,
                 max_clients: int = DEFAULT_MAX_CLIENTS) -> None:
        if not 32 <= ipv6_prefix <= 128:
            raise ValueError("ipv6_prefix must be between 32 and 128")
        if max_clients < 1:
            raise ValueError("max_clients must be at least 1")
        self.per_minute = per_minute
        self.auth_failure_limit = auth_failure_limit
        self.auth_failure_window = auth_failure_window
        self.clock = clock
        self.ipv6_prefix = ipv6_prefix
        self.max_clients = max_clients
        # Oldest use first (`client-prefix-v1`), so the sweep looks at the stalest entries.
        self._clients: OrderedDict[str, _Client] = OrderedDict()
        self._overflow: _Client | None = None
        # Guess slots taken on the overflow entry, per client, so they're given back there.
        self._overflow_guesses: dict[str, int] = {}
        self.overflow_requests = 0
        self._lock = threading.Lock()
        self._tag_key = secrets.token_bytes(32)
        self.limited_requests = 0
        self.locked_out_requests = 0
        self.auth_failures = 0
        self.guesses_refused = 0

    def key(self, client: str) -> str:
        """The name this client's limits are counted under (see `client_key`)."""
        return client_key(client, self.ipv6_prefix)

    def tag(self, client: str) -> str:
        """A short, stable-until-restart label for a client that doesn't reveal its address.
        Clients counted together (one IPv6 block) share a tag."""
        return hmac.new(self._tag_key, self.key(client).encode(),
                        hashlib.sha256).hexdigest()[:12]

    def _get(self, key: str, t: float) -> _Client:
        """The entry for an already-grouped key, made if new. Returns the shared overflow
        entry if the table is full of clients that can't be forgotten yet."""
        entry = self._clients.get(key)
        if entry is not None:
            self._clients.move_to_end(key)
            return entry
        self._sweep(t)
        if len(self._clients) >= self.max_clients:
            if self._overflow is None:
                self._overflow = _Client(tokens=float(self.per_minute), updated=t)
            return self._overflow
        entry = _Client(tokens=float(self.per_minute), updated=t)
        self._clients[key] = entry
        return entry

    def _is_like_new(self, entry: _Client, t: float) -> bool:
        """True if forgetting this entry changes nothing: a new one would be the same."""
        if entry.guessing:
            return False
        self._drop_old_failures(entry, t)
        if entry.failures:
            return False
        if not self.per_minute:
            return True
        refilled = entry.tokens + max(0.0, t - entry.updated) * self.per_minute / 60.0
        return refilled >= float(self.per_minute)

    def _sweep(self, t: float) -> None:
        """Look at the `_SWEEP` oldest entries: forget those that are like new, move the
        others to the back so the next sweep looks at different ones. Constant work."""
        for _ in range(min(_SWEEP, len(self._clients))):
            name, entry = next(iter(self._clients.items()))
            if self._is_like_new(entry, t):
                del self._clients[name]
            else:
                self._clients.move_to_end(name)

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
        key = self.key(client)
        with self._lock:
            t = self.clock()
            entry = self._get(key, t)
            if entry is self._overflow:
                self.overflow_requests += 1
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

    def start_guess(self, client: str) -> tuple[str, int] | None:
        """Take a guess slot before a guessable token is checked (`guess-slot-v1`).
        None = go ahead, and `finish_guess` MUST be called afterwards (after the failure, if
        any, was recorded). Otherwise ("too_many_at_once", retry_after_seconds): the request
        must be refused without checking its token."""
        if not self.auth_failure_limit:
            return None
        key = self.key(client)
        with self._lock:
            t = self.clock()
            entry = self._get(key, t)
            self._drop_old_failures(entry, t)
            if len(entry.failures) + entry.guessing >= self.auth_failure_limit:
                self.guesses_refused += 1
                return "too_many_at_once", max(1, math.ceil(self._locked_for(entry, t)))
            entry.guessing += 1
            if entry is self._overflow:
                self._overflow_guesses[key] = self._overflow_guesses.get(key, 0) + 1
            else:
                entry.updated = t
            return None

    def finish_guess(self, client: str) -> None:
        """Give back a slot taken by `start_guess`."""
        if not self.auth_failure_limit:
            return
        key = self.key(client)
        with self._lock:
            held = self._overflow_guesses.get(key, 0)
            if held and self._overflow is not None:
                # The slot was taken on the overflow entry; give it back there.
                if held == 1:
                    del self._overflow_guesses[key]
                else:
                    self._overflow_guesses[key] = held - 1
                if self._overflow.guessing > 0:
                    self._overflow.guessing -= 1
                return
            entry = self._clients.get(key)
            if entry is not None and entry.guessing > 0:
                entry.guessing -= 1

    def record_auth_failure(self, client: str) -> bool:
        """Count one wrong credential. Returns True if this failure locked the client out."""
        key = self.key(client)
        with self._lock:
            t = self.clock()
            entry = self._get(key, t)
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
            entries = list(self._clients.items())
            if self._overflow is not None:
                entries.append((OVERFLOW, self._overflow))
            for name, entry in entries:
                seconds = self._locked_for(entry, t)
                if seconds > 0:
                    locked.append({"client": name, "client_tag": self.tag(name),
                                   "failures_in_window": len(entry.failures),
                                   "retry_after_seconds": max(1, math.ceil(seconds))})
            return {
                "rate_limit_per_minute": self.per_minute,
                "auth_failure_limit": self.auth_failure_limit,
                "auth_failure_window_seconds": self.auth_failure_window,
                "ipv6_client_prefix": self.ipv6_prefix,
                "tracked_clients": len(self._clients),
                "max_tracked_clients": self.max_clients,
                "since_start": {"rate_limited_requests": self.limited_requests,
                                "locked_out_requests": self.locked_out_requests,
                                "auth_failures": self.auth_failures,
                                "guesses_refused_at_once": self.guesses_refused,
                                "overflow_requests": self.overflow_requests},
                "locked_out_clients": sorted(locked, key=lambda c: c["client_tag"]),
                "note": "In memory: a restart clears these counts and lock-outs.",
            }
