"""A private pool of ready-made hidden tests, handed out a few at a time (`test-pool-v1`).

Threat model #1 and #8 leave one gap: aimed hidden tests (0.5.21) only exist if the operator
writes them, one HTTP request at a time, and #11 says each one should be fresh. This tool
keeps a stock of prepared tests (input + known answer) in one private file and issues them
to the coordinator in small batches, each one used once and then crossed off.

    python -m app.challenge_pool init   --pool ~/bnh-private/tests.json
    python -m app.challenge_pool add    --pool ~/bnh-private/tests.json --file new-tests.jsonl
    python -m app.challenge_pool make-demo --pool ~/bnh-private/tests.json --count 20
    python -m app.challenge_pool status --pool ~/bnh-private/tests.json
    OPERATOR_TOKEN=... python -m app.challenge_pool issue --pool ~/bnh-private/tests.json \\
        --url http://127.0.0.1:8000 --count 3 --tier new

Rules it keeps:
- The pool file holds the answers, so it is created owner-only (0600), never inside a git
  folder, and refused if anyone else can read it. Nothing it prints contains an input or
  an answer.
- `add` checks every test the way the coordinator will: known task type, same size limits,
  the task's own payload check, and the answer passes the task's own verifier. An input
  that is already in the pool, or was used before, is skipped.
- `issue` uses each test at most once. A test is crossed off *before* it is sent, so a crash
  or a lost connection can never send it twice; it is put back only when the coordinator
  clearly refused it without creating anything. A test the coordinator refuses because a
  ledger copy already revealed that input (`409`, `test-reuse-v1`) is retired, not retried.
- It stops at the first sign of trouble (paused coordinator, wrong token, unknown rig,
  network error) instead of burning through the pool.

What it does not do: write good tests for you. For `llm:generate` the known answer has to
come from running the pinned model yourself (on the rig, Phase 3); nothing here has been run
with real model output yet. `make-demo` makes SHA-256 practice tests, which are only useful
for rehearsing the flow: the coordinator already recomputes every SHA-256 answer.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app import tasks
from app.security import sha256_text

POOL_FORMAT = "bnh-test-pool/1"
POOL_POLICY = "test-pool-v1"
TIERS = ("new", "suspect", "established")
# Same limits as ChallengeCreate in app/models.py, so `add` refuses what the coordinator would.
MAX_PAYLOAD = 4096
MAX_EXPECTED = 8192
MAX_ISSUE = 50


class PoolError(Exception):
    """The pool file can't be used (missing, readable by others, damaged, in a git folder)."""


def _now_iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def input_hash(task_type: str, payload: str) -> str:
    """Fingerprint of one test input, kept after the test is used so it is never re-added."""
    return sha256_text(f"{task_type}\n{payload}")


def inside_git_folder(path: Path) -> Path | None:
    for parent in path.resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None


# ---- the file ---------------------------------------------------------------------------

def load_pool(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PoolError(f"{path} doesn't exist; make one with `init` first")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise PoolError(f"{path} can be read by other users; it holds hidden-test answers. "
                        f"Run `chmod 600 {path}` and check nobody copied it")
    try:
        pool = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PoolError(f"{path} isn't a readable pool file ({exc.__class__.__name__})") from None
    if not isinstance(pool, dict) or pool.get("format") != POOL_FORMAT \
            or not isinstance(pool.get("entries"), list) or not isinstance(pool.get("used"), list):
        raise PoolError(f"{path} isn't a {POOL_FORMAT} pool file")
    return pool


def save_pool(path: Path, pool: dict[str, Any]) -> None:
    """Write a new copy owner-only, then swap it in, so a crash leaves the old or new file
    whole, never half of one."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(pool, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def init_pool(path: Path, now: float) -> None:
    if path.exists():
        raise PoolError(f"{path} already exists; refusing to overwrite it")
    repo = inside_git_folder(path)
    if repo is not None:
        raise PoolError(f"{path} is inside the git folder {repo}; hidden-test answers must "
                        "never be committed. Keep the pool somewhere private outside the repo")
    if not path.parent.is_dir():
        raise PoolError(f"folder {path.parent} doesn't exist")
    save_pool(path, {"format": POOL_FORMAT, "policy": POOL_POLICY,
                     "created_at": _now_iso(now), "entries": [], "used": []})


# ---- adding tests -----------------------------------------------------------------------

def check_test(task_type: Any, payload: Any, expected: Any) -> str | None:
    """None if the coordinator would accept this hidden test, else the reason it wouldn't."""
    if task_type not in tasks.EXECUTORS:
        return f"unknown task_type (use one of {', '.join(sorted(tasks.EXECUTORS))})"
    if not isinstance(payload, str) or not 1 <= len(payload) <= MAX_PAYLOAD:
        return f"payload must be text, 1-{MAX_PAYLOAD} characters"
    if not isinstance(expected, str) or not 1 <= len(expected) <= MAX_EXPECTED:
        return f"expected_result must be text, 1-{MAX_EXPECTED} characters"
    executor = tasks.get(task_type)
    try:
        executor.validate(payload)
    except tasks.TaskError as exc:
        return f"payload refused by {task_type}: {exc}"
    if not executor.verify(payload, expected).ok:
        return "expected_result fails this task's own verifier"
    return None


def known_inputs(pool: dict[str, Any]) -> set[str]:
    hashes = {input_hash(e["task_type"], e["payload"]) for e in pool["entries"]}
    return hashes | {u["input_sha256"] for u in pool["used"]}


def add_tests(pool: dict[str, Any], items: Iterable[tuple[str, Any]], source: str,
              now: float) -> tuple[int, list[str]]:
    """Add (label, item) pairs, each item a dict with task_type, payload, expected_result.
    Returns how many were added and one line per skipped item (never the input itself)."""
    seen = known_inputs(pool)
    added, skipped = 0, []
    for label, item in items:
        if not isinstance(item, dict):
            skipped.append(f"{label}: not a JSON object")
            continue
        task_type = item.get("task_type", "llm:generate")
        payload, expected = item.get("payload"), item.get("expected_result")
        reason = check_test(task_type, payload, expected)
        if reason is None and input_hash(task_type, payload) in seen:
            reason = "this input is already in the pool or was used before"
        if reason is not None:
            skipped.append(f"{label}: {reason}")
            continue
        seen.add(input_hash(task_type, payload))
        pool["entries"].append({"id": secrets.token_hex(6), "task_type": task_type,
                                "payload": payload, "expected_result": expected,
                                "added_at": _now_iso(now), "source": source})
        added += 1
    return added, skipped


def read_jsonl(path: Path) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            items.append((f"line {number}", json.loads(line)))
        except json.JSONDecodeError:
            items.append((f"line {number}", None))
    return items


def make_demo_tests(count: int) -> list[tuple[str, dict[str, str]]]:
    """Practice tests for `demo:sha256`. The answer is computed here, so they're free."""
    out = []
    for i in range(count):
        payload = secrets.token_hex(16)
        out.append((f"demo {i + 1}", {"task_type": "demo:sha256", "payload": payload,
                                      "expected_result": sha256_text(payload)}))
    return out


# ---- status -----------------------------------------------------------------------------

def pool_status(pool: dict[str, Any]) -> dict[str, Any]:
    """Counts only: no inputs, answers or input hashes."""
    ready = Counter(e["task_type"] for e in pool["entries"])
    used = Counter(u["outcome"] for u in pool["used"])
    return {"format": POOL_FORMAT, "policy": POOL_POLICY, "ready": dict(sorted(ready.items())),
            "ready_total": sum(ready.values()), "used": dict(sorted(used.items())),
            "used_total": len(pool["used"])}


def status_text(status: dict[str, Any]) -> str:
    lines = [f"Hidden-test pool ({POOL_POLICY}): {status['ready_total']} ready to use."]
    for task_type, n in status["ready"].items():
        lines.append(f"  {task_type}: {n}")
    if status["used_total"]:
        parts = ", ".join(f"{n} {k}" for k, n in status["used"].items())
        lines.append(f"Used so far: {status['used_total']} ({parts}).")
    if status["ready_total"] < 5:
        lines.append("Running low: add fresh tests before the next batch.")
    return "\n".join(lines)


# ---- issuing ----------------------------------------------------------------------------

def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("detail", ""))[:200] if isinstance(body, dict) else ""


def issue_tests(path: Path, client: httpx.Client, operator_token: str, count: int, *,
                task_type: str | None = None, target_worker: str | None = None,
                target_tier: str | None = None, ttl_seconds: int = 600,
                now: Callable[[], float] = time.time,
                wait: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Send up to `count` tests from the pool to the coordinator as hidden tests.

    The pool file is re-saved after every step, so whatever happens it always says which
    tests were used. The result lists job ids and outcomes only."""
    if not 1 <= count <= MAX_ISSUE:
        raise PoolError(f"--count must be 1-{MAX_ISSUE}")
    if target_worker is not None and target_tier is not None:
        raise PoolError("aim at one rig (--worker) or one tier (--tier), not both")
    if target_tier is not None and target_tier not in TIERS:
        raise PoolError(f"--tier must be one of {', '.join(TIERS)}")
    pool = load_pool(path)
    candidates = [e for e in pool["entries"] if task_type is None or e["task_type"] == task_type]
    if not candidates:
        raise PoolError("no unused tests in the pool" + (f" for {task_type}" if task_type else ""))
    picker = secrets.SystemRandom()
    chosen = picker.sample(candidates, min(count, len(candidates)))
    target = {"target_worker": target_worker} if target_worker else (
        {"target_tier": target_tier} if target_tier else {})
    target_label = (f"worker:{target_worker}" if target_worker else
                    f"tier:{target_tier}" if target_tier else None)
    headers = {"X-Operator-Token": operator_token}
    result: dict[str, Any] = {"policy": POOL_POLICY, "asked": count, "target": target_label,
                              "issued": [], "retired": 0, "unknown": 0, "stopped": None}

    for entry in chosen:
        # Cross it off first: if anything below goes wrong, the test is never sent twice.
        pool["entries"] = [e for e in pool["entries"] if e["id"] != entry["id"]]
        record = {"id": entry["id"], "task_type": entry["task_type"],
                  "input_sha256": input_hash(entry["task_type"], entry["payload"]),
                  "outcome": "sending", "job_id": None, "target": target_label,
                  "at": _now_iso(now())}
        pool["used"].append(record)
        save_pool(path, pool)

        body = {"task_type": entry["task_type"], "payload": entry["payload"],
                "expected_result": entry["expected_result"], "ttl_seconds": ttl_seconds,
                **target}
        response = None
        try:
            for _ in range(5):
                response = client.post("/v1/challenges", headers=headers, json=body)
                if response.status_code != 429:
                    break
                try:
                    pause = int(response.headers.get("Retry-After", "5"))
                except ValueError:
                    pause = 5
                wait(max(1, min(pause, 60)))
        except httpx.HTTPError as exc:
            response = None
            result["stopped"] = (f"lost contact with the coordinator ({exc.__class__.__name__}); "
                                 "this test may or may not have been queued, so it stays used")
        status = response.status_code if response is not None else None
        detail = _detail(response) if response is not None else ""

        if status == 200:
            try:
                job_id = response.json().get("job_id")
            except (ValueError, AttributeError):
                job_id = None
            record["outcome"], record["job_id"] = "issued", job_id
            result["issued"].append({"job_id": record["job_id"], "task_type": entry["task_type"]})
        elif status == 409 and "already a hidden test" in detail:
            record["outcome"] = "retired"  # revealed by a ledger copy (test-reuse-v1)
            result["retired"] += 1
        elif status is not None and (status in (401, 403, 404, 409, 422, 429, 503)):
            # Clearly refused and nothing was created: put the test back for later.
            pool["used"].remove(record)
            pool["entries"].append(entry)
            reasons = {401: "operator token missing", 403: "wrong operator token",
                       404: "no such rig", 409: "refused", 422: "the coordinator refused this test",
                       429: "still rate-limited after 5 tries",
                       503: "the coordinator is paused or down"}
            result["stopped"] = f"{status}: {reasons[status]}" + (f" ({detail})" if detail else "")
        else:
            record["outcome"] = "unknown"
            result["unknown"] += 1
            if result["stopped"] is None:
                result["stopped"] = (f"unexpected answer {status}; this test may or may not "
                                     "have been queued, so it stays used")
        save_pool(path, pool)
        if result["stopped"]:
            break

    result["ready_left"] = len(pool["entries"])
    return result


def issue_text(result: dict[str, Any]) -> str:
    aim = f" aimed at {result['target']}" if result["target"] else " (first-come)"
    lines = [f"Issued {len(result['issued'])} of {result['asked']} hidden tests{aim}."]
    for item in result["issued"]:
        lines.append(f"  {item['job_id']}  {item['task_type']}")
    if result["retired"]:
        lines.append(f"Retired {result['retired']} test(s) a ledger copy had already revealed.")
    if result["unknown"]:
        lines.append(f"{result['unknown']} test(s) may or may not have been queued; they are "
                     "crossed off so they can't be sent twice.")
    if result["stopped"]:
        lines.append(f"Stopped: {result['stopped']}")
    if len(result["issued"]) < result["asked"] and not result["stopped"]:
        lines.append("The pool ran out; add fresh tests.")
    lines.append(f"{result['ready_left']} test(s) left in the pool.")
    return "\n".join(lines)


# ---- command line -----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Private pool of hidden tests, issued a few at a time (test-pool-v1)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("init", "create an empty pool file (owner-only, outside git)"),
                       ("add", "add tests from a JSON-lines file"),
                       ("make-demo", "add SHA-256 practice tests"),
                       ("status", "how many tests are ready and used (no answers shown)"),
                       ("issue", "send tests to a coordinator as hidden tests")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--pool", type=Path, required=True, help="the private pool file")
        if name == "add":
            p.add_argument("--file", type=Path, required=True,
                           help='one JSON object per line: {"task_type", "payload", '
                                '"expected_result"}')
        if name == "make-demo":
            p.add_argument("--count", type=int, required=True)
        if name == "status":
            p.add_argument("--json", action="store_true")
        if name == "issue":
            p.add_argument("--url", required=True, help="coordinator, e.g. http://127.0.0.1:8000")
            p.add_argument("--count", type=int, required=True, help=f"1-{MAX_ISSUE}")
            p.add_argument("--task", choices=sorted(tasks.EXECUTORS))
            aim = p.add_mutually_exclusive_group()
            aim.add_argument("--worker", help="only this rig can take them")
            aim.add_argument("--tier", choices=TIERS, help="a random active rig in this tier")
            p.add_argument("--ttl", type=int, default=600, help="seconds, 10-3600")
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            init_pool(args.pool, time.time())
            print(f"Created {args.pool} (readable by you only). Keep it out of git and backups "
                  "other people can open: it holds the answers.")
            return 0
        if args.command == "status":
            status = pool_status(load_pool(args.pool))
            print(json.dumps(status, indent=2) if args.json else status_text(status))
            return 0
        if args.command in ("add", "make-demo"):
            pool = load_pool(args.pool)
            if args.command == "add":
                if not args.file.is_file():
                    raise PoolError(f"{args.file} doesn't exist")
                items, source = read_jsonl(args.file), f"file:{args.file.name}"
            else:
                if not 1 <= args.count <= 1000:
                    raise PoolError("--count must be 1-1000")
                items, source = make_demo_tests(args.count), "make-demo"
            added, skipped = add_tests(pool, items, source, time.time())
            save_pool(args.pool, pool)
            print(f"Added {added} test(s); {len(pool['entries'])} ready in the pool.")
            for line in skipped:
                print(f"  skipped {line}")
            if args.command == "add" and added:
                print(f"The pool has its own copy now; delete {args.file} if nobody else "
                      "should see those answers.")
            return 0 if not skipped else 1
        # issue
        if not 10 <= args.ttl <= 3600:
            raise PoolError("--ttl must be 10-3600 seconds")
        token = os.getenv("OPERATOR_TOKEN") or ""
        if not token:
            print("Refusing: set OPERATOR_TOKEN in the environment (not on the command line).",
                  file=sys.stderr)
            return 2
        with httpx.Client(base_url=args.url, timeout=15) as client:
            result = issue_tests(args.pool, client, token, args.count, task_type=args.task,
                                 target_worker=args.worker, target_tier=args.tier,
                                 ttl_seconds=args.ttl)
        print(issue_text(result))
        return 0 if not result["stopped"] and not result["unknown"] else 1
    except PoolError as exc:
        print(f"Refusing: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
