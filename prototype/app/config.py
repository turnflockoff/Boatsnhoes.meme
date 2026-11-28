from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .client_address import parse_trusted_proxies
from .points import parse_points_table

load_dotenv()

log = logging.getLogger("bnh.config")


@dataclass(frozen=True)
class Settings:
    database_path: Path
    # Coordinator's Ed25519 private key for signing job manifests (threat model #9).
    # None = "coordinator-signing.key" next to the database. Created on first start.
    signing_key_path: Path | None = None
    lease_seconds: int = 60
    max_attempts: int = 3
    # Phase 4: share of "schema-only" results re-run by a different worker (0.0-1.0).
    audit_rate: float = 0.25
    # Threat model #1 (`audit-tier-v1`, app/audit_policy.py): rigs with too few
    # independently checked results, or with a strike (failed hidden test / outvoted),
    # are re-checked more often. Never below `audit_rate`.
    audit_rate_new: float = 0.5
    audit_rate_suspect: float = 1.0
    audit_new_until: int = 5
    # Required for operator endpoints (challenges, audits, trust). Unset = those are disabled.
    operator_token: str | None = None
    # Phase 4: when a re-run disagrees, ask a third worker to break the tie.
    tiebreak: bool = True
    # Threat model #4: audits go to a random active worker, not whoever polls first.
    # A worker counts as active this long after its last job request.
    auditor_active_seconds: int = 300
    # `auditor-pick-v1` (app/audit_policy.py): "uniform" = any eligible rig, same chance;
    # "trusted-first" = established rigs first, then new, a rig with a strike last.
    auditor_pick: str = "uniform"
    # `tiebreak-trust-v1` (app/audit_policy.py): "always" = the tie-breaker's majority
    # settles a dispute; "established-only" = only when both rigs on the winning side are
    # established, otherwise the case is held for a person (threat model #6).
    tiebreak_decides: str = "always"
    # How long the chosen worker has to pick up its audit before it is re-assigned.
    audit_assign_seconds: int = 120
    # Operator health warns about a worker that has dropped this many assigned audits or
    # tie-breakers (not picked up, lease ran out, or result refused). Lifetime count.
    audit_drop_warn: int = 3
    # Who may queue normal jobs (POST /v1/jobs): the operator token, or this separate
    # submitter token, which can only create jobs (no hidden tests, answers or reports).
    # With neither set, job creation is disabled.
    submitter_token: str | None = None
    # app/rate_limit.py: requests per minute per client address (0 = off), and a lock-out
    # after `auth_failure_limit` wrong credentials within `auth_failure_window_seconds`.
    rate_limit_per_minute: int = 240
    auth_failure_limit: int = 10
    auth_failure_window_seconds: int = 600
    # `client-prefix-v1`: IPv6 clients are counted per block of this many bits (one home or
    # server connection usually gets a /64; 128 = per full address), and the limiter keeps at
    # most this many clients in memory (more share one "overflow" bucket).
    ipv6_client_prefix: int = 64
    rate_limit_max_clients: int = 50_000
    # app/client_address.py: reverse proxies whose X-Forwarded-For header is believed, so
    # limits apply to the real client. Empty = the header is ignored (the safe default).
    trusted_proxies: tuple[str, ...] = ()
    # app/points.py (`points-table-v1`): fixed simulated points per task type, as sorted
    # (task_type, points) pairs. Empty = whoever queues a job picks its points (0-1000).
    points_table: tuple[tuple[str, int], ...] = ()
    # app/key_rotation.py (`handover-key-v1`): PUBLIC half of the offline key that must also
    # sign every key handover. None = handovers only need the old and new coordinator key.
    handover_public_key: str | None = None
    # app/rewards.py: scoring rule for reward reports. "sim-points-v1" (default) pays all
    # unverified work; "sim-points-v2-forfeit" (opt-in, threat model #15) forfeits a rig's
    # unverified points for a period in which it got a strike. Owner decision.
    reward_scoring: str = "sim-points-v1"
    # `public-delay-v1` (threat model #14): for this many seconds after a result is
    # accepted, the public GET /v1/ledger counts it as "pending" and withholds its
    # confidence, whatever kind of job it was. Shown live, a hidden test lands straight in
    # confirmed/held while normal AI work lands in pending/unverified, so a rig could spot
    # its hidden tests. 0 = live (old behaviour). The operator token always sees it live.
    public_settle_delay_seconds: int = 3600
    # `public-counts-v1` (threat model #14): the public job counts (/health, GET /v1/ledger)
    # are taken once per clock-aligned window of this many seconds and repeated until the
    # next window, for everyone. Live, `queued` rose the instant a rig's result was picked
    # for a re-check, so a rig could tell which of its results get re-checked. 0 = live
    # (old behaviour). The operator token always sees live counts.
    public_counts_interval_seconds: int = 900
    # `input-limits-v1` (app/input_limits.py): largest request body accepted, in bytes.
    # Bigger bodies get 413 before they are read. The largest honest body is ~150 KB.
    max_body_bytes: int = 256 * 1024
    # `ledger-clock-v1` (app/ledger_clock.py): refuse to start when the server clock is
    # more than this many seconds behind the newest time already in the ledger (a clock
    # reset to 1970/2000, or set back), instead of writing new work into closed periods.
    clock_behind_limit_seconds: int = 300
    # A server clock that moves this many seconds more (or less) than a steady clock between
    # two readings counts as a step: logged and shown on the operator health page.
    clock_step_warn_seconds: int = 60
    # `storage-full-v1` (app/storage.py): under this much free space (MB) on the ledger's
    # disk the coordinator stops taking NEW work (jobs, hidden tests, enrollment codes), so
    # the room left goes to results from rigs that already did the work. 0 = never refuse.
    disk_reserve_mb: int = 100
    # Under this much free space (MB) the operator health page warns. 0 = no early warning.
    disk_warn_mb: int = 1000
    # `recheck-release-v1` (threat model #5, app/ledger.py release_due): a new re-check
    # (audit or tie-breaker) is held for a random whole number of seconds between these two
    # before anyone can see or take it, so it isn't made in the second the result it
    # re-checks was accepted. Max 0 = handed out at once (the old behaviour). Settings built
    # in code (tests, the in-process rehearsal) start at 0; the coordinator started
    # normally (get_settings) holds re-checks 60-600 s unless RECHECK_DELAY_* say otherwise.
    recheck_delay_min_seconds: int = 0
    recheck_delay_max_seconds: int = 0
    # How often the coordinator's release timer looks for held re-checks that are due
    # (seconds). 0 = no timer (tests call app.state.release_held themselves).
    recheck_release_tick_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not 0 <= self.recheck_delay_min_seconds <= self.recheck_delay_max_seconds:
            raise ValueError("RECHECK_DELAY_MIN_SECONDS must be between 0 and "
                             "RECHECK_DELAY_MAX_SECONDS")
        if self.recheck_release_tick_seconds < 0:
            raise ValueError("RECHECK_RELEASE_TICK_SECONDS can't be negative")

    def resolved_signing_key_path(self) -> Path:
        return self.signing_key_path or self.database_path.parent / "coordinator-signing.key"


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


def _database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "./data/prototype.sqlite3"))


def default_signing_key_path() -> Path:
    raw = os.getenv("COORDINATOR_SIGNING_KEY_FILE")
    return Path(raw) if raw else _database_path().parent / "coordinator-signing.key"


def handover_public_key_env() -> str | None:
    """COORDINATOR_HANDOVER_PUBLIC_KEY: 64 hex characters, or unset."""
    from .signing import parse_public_key

    raw = (os.getenv("COORDINATOR_HANDOVER_PUBLIC_KEY") or "").strip().lower()
    if not raw:
        return None
    try:
        parse_public_key(raw)
    except ValueError as exc:
        raise ValueError(f"COORDINATOR_HANDOVER_PUBLIC_KEY: {exc}") from exc
    return raw


def _auditor_pick_env() -> str:
    from .audit_policy import AUDITOR_PICK_RULES

    raw = (os.getenv("AUDITOR_PICK") or "uniform").strip().lower()
    if raw not in AUDITOR_PICK_RULES:
        raise ValueError(f"AUDITOR_PICK must be one of {', '.join(AUDITOR_PICK_RULES)}")
    return raw


def _tiebreak_decides_env() -> str:
    from .audit_policy import TIEBREAK_DECIDES_RULES

    raw = (os.getenv("TIEBREAK_DECIDES") or "always").strip().lower()
    if raw not in TIEBREAK_DECIDES_RULES:
        raise ValueError(f"TIEBREAK_DECIDES must be one of {', '.join(TIEBREAK_DECIDES_RULES)}")
    return raw


def reward_scoring_env() -> str:
    from .rewards import CURRENT_SCORING, SCORING_RULES

    raw = (os.getenv("REWARD_SCORING") or CURRENT_SCORING).strip().lower()
    if raw not in SCORING_RULES:
        raise ValueError(f"REWARD_SCORING must be one of {', '.join(sorted(SCORING_RULES))}")
    return raw


def get_settings() -> Settings:
    from .tasks import EXECUTORS  # the task types a points table must cover

    db = _database_path()
    if os.getenv("PROTOTYPE_SECRET"):
        log.warning("PROTOTYPE_SECRET is no longer used: job manifests are now signed with the "
                    "coordinator's Ed25519 key. Remove it from .env and from worker machines.")
    operator_token = os.getenv("OPERATOR_TOKEN") or None
    if operator_token is not None and len(operator_token) < 24:
        raise ValueError("OPERATOR_TOKEN must be at least 24 characters")
    submitter_token = os.getenv("SUBMITTER_TOKEN") or None
    if submitter_token is not None:
        if len(submitter_token) < 24:
            raise ValueError("SUBMITTER_TOKEN must be at least 24 characters")
        if submitter_token == operator_token:
            raise ValueError("SUBMITTER_TOKEN must be different from OPERATOR_TOKEN")
    # `recheck-release-v1`: RECHECK_DELAY_MAX_SECONDS=0 alone turns the hold off.
    recheck_max = _int_env("RECHECK_DELAY_MAX_SECONDS", 600, 0, 86400)
    return Settings(
        database_path=db,
        signing_key_path=default_signing_key_path(),
        lease_seconds=_int_env("LEASE_SECONDS", 60, 5, 3600),
        max_attempts=_int_env("MAX_ATTEMPTS", 3, 1, 20),
        audit_rate=_rate_env("AUDIT_RATE", 0.25),
        audit_rate_new=_rate_env("AUDIT_RATE_NEW", 0.5),
        audit_rate_suspect=_rate_env("AUDIT_RATE_SUSPECT", 1.0),
        audit_new_until=_int_env("AUDIT_NEW_UNTIL", 5, 0, 100000),
        operator_token=operator_token,
        tiebreak=_bool_env("TIEBREAK", True),
        auditor_active_seconds=_int_env("AUDITOR_ACTIVE_SECONDS", 300, 10, 86400),
        audit_assign_seconds=_int_env("AUDIT_ASSIGN_SECONDS", 120, 5, 86400),
        auditor_pick=_auditor_pick_env(),
        tiebreak_decides=_tiebreak_decides_env(),
        audit_drop_warn=_int_env("AUDIT_DROP_WARN", 3, 1, 100000),
        submitter_token=submitter_token,
        rate_limit_per_minute=_int_env("RATE_LIMIT_PER_MINUTE", 240, 0, 100000),
        auth_failure_limit=_int_env("AUTH_FAILURE_LIMIT", 10, 0, 10000),
        auth_failure_window_seconds=_int_env("AUTH_FAILURE_WINDOW_SECONDS", 600, 10, 86400),
        ipv6_client_prefix=_int_env("IPV6_CLIENT_PREFIX", 64, 32, 128),
        rate_limit_max_clients=_int_env("RATE_LIMIT_MAX_CLIENTS", 50_000, 1000, 10_000_000),
        trusted_proxies=tuple(str(n) for n in
                              parse_trusted_proxies(os.getenv("TRUSTED_PROXIES"))),
        points_table=parse_points_table(os.getenv("POINTS_TABLE"), EXECUTORS),
        handover_public_key=handover_public_key_env(),
        reward_scoring=reward_scoring_env(),
        public_settle_delay_seconds=_int_env("PUBLIC_SETTLE_DELAY_SECONDS", 3600, 0,
                                             30 * 86400),
        public_counts_interval_seconds=_int_env("PUBLIC_COUNTS_INTERVAL_SECONDS", 900, 0,
                                                86400),
        max_body_bytes=_int_env("MAX_BODY_BYTES", 256 * 1024, 16 * 1024, 4 * 1024 * 1024),
        clock_behind_limit_seconds=_int_env("CLOCK_BEHIND_LIMIT_SECONDS", 300, 0, 7 * 86400),
        clock_step_warn_seconds=_int_env("CLOCK_STEP_WARN_SECONDS", 60, 1, 86400),
        disk_reserve_mb=_int_env("DISK_RESERVE_MB", 100, 0, 1024 * 1024),
        disk_warn_mb=_int_env("DISK_WARN_MB", 1000, 0, 1024 * 1024),
        recheck_delay_min_seconds=_int_env("RECHECK_DELAY_MIN_SECONDS",
                                           min(60, recheck_max), 0, 86400),
        recheck_delay_max_seconds=recheck_max,
        recheck_release_tick_seconds=float(_int_env("RECHECK_RELEASE_TICK_SECONDS", 1, 1, 60)),
    )
