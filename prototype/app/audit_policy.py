"""Phase 4: how often a worker's unproven results are re-checked (threat model #1).

A flat audit rate treats a brand-new rig and one with a long clean record the same, and
keeps auditing a rig that was just caught at the same low rate. `audit-tier-v1` sorts each
worker into one of three tiers from its own record in the ledger:

    suspect      at least one strike: a failed hidden test job (`failed-challenge`) or a
                 result the tie-breaker voted against (`outvoted`). Re-checked at
                 `audit_rate_suspect` (default: every result).
    new          fewer than `audit_new_until` results that passed an independent check
                 (`confirmed` re-run or `passed-challenge`). Re-checked at `audit_rate_new`.
    established  everyone else. Re-checked at the base `audit_rate`.

The rate actually used is never below the base rate, so `AUDIT_RATE=1` still checks
everything. The tier is an operator-only fact: it is never put in an event, an envelope or a
receipt, so a worker can't read its own tier (it can only guess from how many duplicate
prompts other rigs see).

What this is not:
  - Not a penalty. Strikes change how often a worker is checked, never its points. A points
    penalty is scoring v2, still an owner decision (threat model #15); a candidate,
    `sim-points-v2-forfeit` in app/rewards.py, is built but off by default.
  - Not proof of honesty. Two members of a ring that audit each other can "confirm" fake
    answers and so graduate out of `new` (threat model #3). Passed hidden test jobs can't be
    faked that way, but they only exist if the operator creates them.
  - Not forgiving. Strikes don't expire in v1; the operator can revoke a rig instead.

The same tiers can also decide who is picked to audit others (`auditor-pick-v1`, setting
`AUDITOR_PICK`). With `trusted-first`, audits and tie-breakers go to a random `established`
rig when one is eligible, else a `new` one, and a `suspect` rig only as a last resort. It is
off by default because audit work pays the same points as normal work, so it shifts who
earns that share; switching it on is the owner's call.

The tiers also decide whether a tie-breaker may settle a dispute on its own
(`tiebreak-trust-v1`, setting `TIEBREAK_DECIDES`, threat model #6). A tie-breaker that sides
with one of the two disputed results normally decides the case: two rigs agree, the odd one
out is `outvoted`. If those two rigs are in the same ring, they outvote an honest one. With
`established-only`, the majority settles the dispute only when both rigs on the winning side
(the tie-breaker and the rig it agreed with) are `established`, judged from their records
just before the vote. Otherwise all three results stay held (`disputed`) and the case goes
to the operator review queue (app/review.py), with no strike for anyone until a person
decides. Off by default (`always`): with only new rigs, every dispute would wait for a
person, and held points stay held because there is no override yet (scoring v2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

AUDIT_POLICY_VERSION = "audit-tier-v1"

# Who gets picked to audit or break a tie (`auditor-pick-v1`, threat model #4 and #6).
#   uniform        every eligible rig has the same chance (the default; today's behaviour).
#   trusted-first  random among the most trusted eligible rigs, in PICK_ORDER: a `suspect`
#                  rig only audits when nobody else is eligible.
AUDITOR_PICK_VERSION = "auditor-pick-v1"
AUDITOR_PICK_RULES = ("uniform", "trusted-first")
PICK_ORDER = ("established", "new", "suspect")

# Who may settle a dispute without a person (`tiebreak-trust-v1`, threat model #6).
#   always            the tie-breaker's majority always settles it (the default; today).
#   established-only  only when the tie-breaker and the rig it sided with are both
#                     `established`; otherwise the case is held for the review queue.
TIEBREAK_DECIDES_VERSION = "tiebreak-trust-v1"
TIEBREAK_DECIDES_RULES = ("always", "established-only")

# Audit statuses that count for or against a worker.
PASSED_STATUSES = ("confirmed", "passed-challenge")
STRIKE_STATUSES = ("failed-challenge", "outvoted")


@dataclass(frozen=True)
class AuditDecision:
    tier: str  # "suspect" | "new" | "established"
    rate: float
    checks_passed: int
    strikes: int


def decide(checks_passed: int, strikes: int, base_rate: float, new_rate: float,
           suspect_rate: float, new_until: int) -> AuditDecision:
    """Pure function, so the rule is easy to test and to re-run by hand."""
    if strikes > 0:
        tier, rate = "suspect", suspect_rate
    elif checks_passed < new_until:
        tier, rate = "new", new_rate
    else:
        tier, rate = "established", base_rate
    return AuditDecision(tier, max(base_rate, rate), checks_passed, strikes)


def decide_for(history: dict[str, int], settings: Any) -> AuditDecision:
    """`history` is Ledger.audit_history(worker_id); `settings` is a config.Settings."""
    return decide(history["checks_passed"], history["strikes"], settings.audit_rate,
                  settings.audit_rate_new, settings.audit_rate_suspect,
                  settings.audit_new_until)


def describe(settings: Any) -> dict[str, Any]:
    """The policy in force, for the operator's /v1/audits view."""
    return {"version": AUDIT_POLICY_VERSION,
            "base_rate": settings.audit_rate,
            "new_rate": max(settings.audit_rate, settings.audit_rate_new),
            "suspect_rate": max(settings.audit_rate, settings.audit_rate_suspect),
            "new_until_checks_passed": settings.audit_new_until,
            "passed_statuses": list(PASSED_STATUSES),
            "strike_statuses": list(STRIKE_STATUSES),
            "auditor_pick": describe_pick(getattr(settings, "auditor_pick", "uniform")),
            "tiebreak_decides": describe_tiebreak(getattr(settings, "tiebreak_decides",
                                                          "always"))}


def auditor_pool(candidates: Sequence[str],
                 tier_of_worker: Callable[[str], str]) -> tuple[list[str], str | None]:
    """`trusted-first`: the eligible rigs in the most trusted tier that has anyone, and that
    tier's name. Pure, and keeps the order it was given, so a seeded rng stays repeatable.
    The pick inside the pool is still random (app/ledger.py)."""
    by_tier: dict[str, list[str]] = {}
    for worker in candidates:
        by_tier.setdefault(tier_of_worker(worker), []).append(worker)
    for tier in PICK_ORDER:
        if by_tier.get(tier):
            return by_tier[tier], tier
    return [], None


def describe_pick(rule: str) -> dict[str, Any]:
    """The auditor-pick rule in force, for the operator's views."""
    return {"version": AUDITOR_PICK_VERSION, "rule": rule, "order": list(PICK_ORDER)}


def tiebreak_settles(rule: str, deciding_tiers: Sequence[str]) -> bool:
    """`tiebreak-trust-v1`: may a majority settle a dispute without a person? Pure.
    `deciding_tiers` are the tiers of the rigs on the winning side (the tie-breaker and the
    rig it agreed with), each from its record before this vote. No tiers known means no."""
    if rule not in TIEBREAK_DECIDES_RULES:
        raise ValueError(f"unknown tiebreak rule {rule!r}")
    if rule == "always":
        return True
    return bool(deciding_tiers) and all(t == "established" for t in deciding_tiers)


def describe_tiebreak(rule: str) -> dict[str, Any]:
    """The tie-breaker rule in force, for the operator's views."""
    return {"version": TIEBREAK_DECIDES_VERSION, "rule": rule}
