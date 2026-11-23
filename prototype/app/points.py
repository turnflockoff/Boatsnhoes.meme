"""Fixed points per task type (threat model #16 follow-up, rule `points-table-v1`).

Without a table, whoever queues a job picks its simulated points (0-1000). Since 0.5.19
only the operator or a submitter token can queue jobs, but whoever holds the submitter
token can still queue a 1000-point job for a friend's rig to pick up.

`POINTS_TABLE` closes that: one fixed number per task type, e.g.

    POINTS_TABLE=demo:sha256=1,llm:generate=10

With a table set, every new job and hidden test gets its points from the table. A request
may leave `reward_points` out or send the table's number; any other number is refused
(422), for the operator as well, so hidden tests always pay what normal work of the same
type pays and can't be spotted by their points. To change the numbers, change the table
and restart. Jobs already queued keep the points they were signed with.

Simulated off-chain points only. This decides who picks the number, not what it is worth.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

RULE = "points-table-v1"
NO_TABLE_RULE = "creator-chooses"
MAX_POINTS = 1000
DEFAULT_POINTS = 1  # used when there is no table and the request leaves the number out

_ENTRY = re.compile(r"^([a-z0-9]+:[a-z0-9_-]+)=(\d{1,4})$")


class PointsError(ValueError):
    """A job asked for points the table doesn't allow."""


def parse_points_table(raw: str | None, task_types: Iterable[str]) -> tuple[tuple[str, int], ...]:
    """Parse `POINTS_TABLE` ("type=points" separated by commas or spaces).

    Empty or unset means no table. A table must list every task type the coordinator
    runs, exactly once, so no job type is silently left to the creator's choice."""
    if raw is None or not raw.strip():
        return ()
    known = set(task_types)
    table: dict[str, int] = {}
    for entry in re.split(r"[,\s]+", raw.strip()):
        if not entry:
            continue
        match = _ENTRY.match(entry)
        if not match:
            raise ValueError(f"POINTS_TABLE entry {entry!r} must look like task:type=points "
                             f"(whole number 0-{MAX_POINTS})")
        task_type, value = match.group(1), int(match.group(2))
        if task_type not in known:
            raise ValueError(f"POINTS_TABLE names an unknown task type {task_type!r}; "
                             f"known: {', '.join(sorted(known))}")
        if task_type in table:
            raise ValueError(f"POINTS_TABLE lists {task_type!r} twice")
        if value > MAX_POINTS:
            raise ValueError(f"POINTS_TABLE value for {task_type!r} must be 0-{MAX_POINTS}")
        table[task_type] = value
    missing = known - set(table)
    if missing:
        raise ValueError(f"POINTS_TABLE must list every task type; missing: "
                         f"{', '.join(sorted(missing))}")
    return tuple(sorted(table.items()))


def resolve(table: tuple[tuple[str, int], ...], task_type: str, requested: int | None) -> int:
    """Points for a new job. No table: the request's number (or 1). Table: the table's
    number; a request for anything else raises PointsError."""
    if not table:
        return DEFAULT_POINTS if requested is None else requested
    fixed = dict(table).get(task_type)
    if fixed is None:  # parse_points_table makes this impossible; never fall back silently
        raise PointsError(f"POINTS_TABLE has no entry for {task_type!r}")
    if requested is not None and requested != fixed:
        raise PointsError(f"points for {task_type} are fixed at {fixed} by POINTS_TABLE; "
                          "leave reward_points out (or send that number)")
    return fixed


def describe(table: tuple[tuple[str, int], ...]) -> dict[str, Any]:
    if not table:
        return {"rule": NO_TABLE_RULE, "table": None,
                "note": f"whoever queues a job picks its points (0-{MAX_POINTS}); "
                        "set POINTS_TABLE to fix them per task type"}
    return {"rule": RULE, "table": dict(table),
            "note": "every new job and hidden test gets the table's points; "
                    "jobs queued earlier keep theirs"}
