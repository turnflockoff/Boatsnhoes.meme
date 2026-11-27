"""The coordinator's clock (`ledger-clock-v1`): never write a time earlier than one already
written, and refuse to start on a clock that is clearly wrong.

Everything in the ledger is ordered by the server's clock: when a job was created, when a
result was accepted, which reward period it belongs to. Server clocks do move: NTP steps
them, a virtual machine is restored from a snapshot, a small board (or a server with a flat
clock battery) boots in 1970 or 2000 or at the time it last shut down. Before this rule the
coordinator simply wrote whatever the clock said. Shown on a real coordinator:

  * a clock set back 3 minutes after period 1 was closed, signed and shared put a new
    result *inside* period 1: the rebuilt period-1 report no longer matched the signed one,
    and `ledger_copy compare` said the operator had back-dated a job and three events. An
    honest operator looked exactly like a cheating one.
  * a server that rebooted with its clock at 2000-01-01 started anyway; the job a crashed
    rig was holding stayed "leased" for 27 years, so nobody else could ever take it.

The rule:

  1. **Time never goes backwards in the ledger.** The coordinator's time is the server
     clock, or the newest time already written, whichever is later. If the clock is set
     back, the coordinator holds its time at the last recorded moment until the clock
     catches up. It still follows the clock forwards (a board that booted with an old
     clock and is then corrected by NTP must catch up, or every honest rig's result would
     look like it came from the future).
  2. **Refuse to start on a clock that is far behind the ledger.** More than
     `CLOCK_BEHIND_LIMIT_SECONDS` (default 300) behind the newest thing written means the
     clock is wrong, not that time passed; holding time for longer would freeze leases and
     deadlines. The message says how far behind and what to do.
  3. **Notice steps while running.** Every reading compares how far the server clock moved
     with how far a steady clock (which no one can set) moved. A difference of
     `CLOCK_STEP_WARN_SECONDS` (default 60) or more is a step: logged, counted, and shown on
     the operator health page with a plain-language warning.
  4. **The operator's own tools follow the same rule** (`app.incident`, `app.workers`,
     `app.ledger_copy export`, `app.rewards report`), and signing a reward report is written
     into the ledger (`report_signed`), so the end of a signed period is a recorded time
     too and nothing can later be written before it.

What it can't do: it can't tell a wrong clock from a right one in the forward direction.
If the clock jumps ahead by a day for a moment, leases and deadlines inside that day expire
(the health page says so), and once the clock is set back the coordinator holds its time
until real time catches up. Keep NTP running on the server.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable

RULE = "ledger-clock-v1"
DEFAULT_BEHIND_LIMIT = 300
DEFAULT_STEP_WARN = 60
MAX_BEHIND_LIMIT = 7 * 86400
KEPT_STEPS = 10

log = logging.getLogger("bnh.clock")


class ClockBehindError(RuntimeError):
    """The server clock is further behind the ledger than the allowed limit."""


def behind_limit_env() -> int:
    """CLOCK_BEHIND_LIMIT_SECONDS for the command-line tools (the server reads Settings)."""
    from .config import _int_env
    return _int_env("CLOCK_BEHIND_LIMIT_SECONDS", DEFAULT_BEHIND_LIMIT, 0, MAX_BEHIND_LIMIT)


def check_behind(wall: int, latest: int | None, limit: int) -> None:
    """Raise ClockBehindError if `wall` is more than `limit` seconds before `latest`."""
    if latest is None or latest - wall <= limit:
        return
    behind = latest - wall
    raise ClockBehindError(
        f"the server clock ({wall}, {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(wall))} "
        f"UTC) is {behind} s behind the newest time already in the ledger ({latest}, "
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(latest))} UTC). Writing with this "
        "clock would put new work into periods that are already closed. Fix the server clock "
        "first (check `timedatectl` and that NTP is on), then start again. If the clock is "
        "right and the ledger came from a machine whose clock was ahead, wait until real "
        f"time passes {latest}, or raise CLOCK_BEHIND_LIMIT_SECONDS (now {limit}) on purpose")


def tool_now(latest: int | None, wall: Callable[[], float] | None = None,
             limit: int | None = None) -> int:
    """The time an operator tool writes with: the server clock, never earlier than the
    newest time already in the ledger. Refuses (ClockBehindError) if the clock is too far
    behind, exactly like the coordinator does at start."""
    w = int((wall or time.time)())  # looked up now, so a patched time.time is seen
    check_behind(w, latest, behind_limit_env() if limit is None else limit)
    return max(w, latest or 0)


class LedgerClock:
    """The coordinator's time: `max(server clock, newest time written)`, never decreasing.

    `wall` is the server clock (time.time). `steady` is a clock nobody can set
    (time.monotonic), used only to notice steps. Tests inject one fake clock as both, so
    a test moving its clock forward is never seen as a step."""

    def __init__(self, wall: Callable[[], float], steady: Callable[[], float],
                 latest: int | None, behind_limit: int = DEFAULT_BEHIND_LIMIT,
                 step_warn: int = DEFAULT_STEP_WARN) -> None:
        self._wall = wall
        self._steady = steady
        self.step_warn = step_warn
        w = wall()
        check_behind(int(w), latest, behind_limit)
        self._high = max(int(w), latest or 0)
        self.held_at_start = max(0, (latest or 0) - int(w))
        self._last_pair = (w, steady())
        self._lock = threading.Lock()
        self._steps: deque[dict[str, int]] = deque(maxlen=KEPT_STEPS)
        self.step_count = 0
        if self.held_at_start:
            log.warning("server clock is behind the ledger; holding time until it catches up",
                        extra={"behind_seconds": self.held_at_start})

    def __call__(self) -> int:
        with self._lock:
            w, s = self._wall(), self._steady()
            w_prev, s_prev = self._last_pair
            step = int((w - w_prev) - (s - s_prev))
            if abs(step) >= self.step_warn:
                self.step_count += 1
                self._steps.append({"at": max(int(w), self._high), "seconds": step})
                log.warning("server clock stepped", extra={"step_seconds": step})
            self._last_pair = (w, s)
            self._high = max(self._high, int(w))
            return self._high

    def status(self) -> dict[str, Any]:
        """Operator view: the coordinator's time, the server clock, how long it is held."""
        t = self()
        wall = int(self._wall())
        return {"rule": RULE, "coordinator_time": t, "server_clock": wall,
                "held_seconds": max(0, t - wall), "held_at_start_seconds": self.held_at_start,
                "steps_seen": self.step_count, "recent_steps": list(self._steps),
                "step_warn_seconds": self.step_warn}

    def warnings(self) -> list[str]:
        st = self.status()
        out: list[str] = []
        if st["held_seconds"]:
            out.append(f"the server clock is {st['held_seconds']} s behind the coordinator's "
                       "time (it was set back, or the server started behind the ledger), so "
                       "the coordinator is holding its time at the last recorded moment until "
                       "the clock catches up; leases and deadlines wait meanwhile. Check "
                       "`timedatectl` and that NTP is on")
        ahead = [s for s in st["recent_steps"] if s["seconds"] > 0]
        back = [s for s in st["recent_steps"] if s["seconds"] < 0]
        if ahead:
            out.append(f"the server clock jumped ahead {len(ahead)} time(s) while running "
                       f"(largest {max(s['seconds'] for s in ahead)} s): leases and deadlines "
                       "inside a jump expire at once. Fine if it was a correction; if the "
                       "clock was wrong, rigs lost that work. Keep NTP on")
        if back:
            out.append(f"the server clock was set back {len(back)} time(s) while running "
                       f"(largest {-min(s['seconds'] for s in back)} s); nothing was written "
                       "with an earlier time (`ledger-clock-v1`)")
        return out


def default_steady(clock: Callable[[], float]) -> Callable[[], float]:
    """The real steady clock for the real server clock; the injected clock itself in tests
    (so moving a fake clock is never counted as a step)."""
    return time.monotonic if clock is time.time else clock

