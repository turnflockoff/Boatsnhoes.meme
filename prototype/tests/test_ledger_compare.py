"""Comparing two ledger copies (threat model #10): a newer copy must keep the older copy's
history unchanged. Nothing edited, deleted or back-dated into the period already shared."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import ledger_copy
from app.ledger_compare import CompareError, compare_copies
from app.ledger_copy import _file_sha256, export_copy
from tests.test_audits import T0, make, pinned  # noqa: F401  (pytest fixture)
from tests.test_ledger_copy import UNTIL, busy_period, rows
from tests.test_rewards import sha_job

UNTIL2 = T0 + 400


def two_copies(tmp_path: Path) -> dict:
    """Period 1 (busy: exact job, disputed AI job, failed hidden test), copy 1; then period 2
    (one more job, wk-c revoked), copy 2."""
    client, clock, ledger = make(tmp_path)
    ids = busy_period(client)
    clock.t = T0 + 200
    old = tmp_path / "copy-1.sqlite3"
    export_copy(ledger.path, old, UNTIL, int(clock.t))
    clock.t = T0 + 250
    ids["later"] = sha_job(client, "wk-a", payload="period two")
    clock.t = T0 + 260
    assert ledger.revoke_worker("wk-c", int(clock.t))
    clock.t = T0 + 600
    new = tmp_path / "copy-2.sqlite3"
    export_copy(ledger.path, new, UNTIL2, int(clock.t))
    return {"client": client, "clock": clock, "ledger": ledger, "ids": ids,
            "old": old, "new": new}


def edit(path: Path, sql: str, args: tuple = ()) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql, args)
        db.commit()


@pytest.mark.usefixtures("pinned")
def test_honest_copies_match(tmp_path: Path) -> None:
    s = two_copies(tmp_path)
    before = (_file_sha256(s["old"]), _file_sha256(s["new"]))
    result = compare_copies(s["old"], s["new"])
    assert result["ok"], result["problems"]
    assert result["problem_count"] == 0
    assert result["compared"] == {"jobs": 5, "results": 5, "rigs": 3,
                                  "events": len(rows(s["old"], "SELECT id FROM events"))}
    assert result["older"]["until"] == UNTIL and result["newer"]["until"] == UNTIL2
    assert len(result["report_sha256_up_to_older_until"]) == 64
    # The newer copy really does show the later revocation; the older one can't.
    assert rows(s["old"], "SELECT revoked_at FROM workers WHERE worker_id='wk-c'"
                )[0]["revoked_at"] is None
    assert rows(s["new"], "SELECT revoked_at FROM workers WHERE worker_id='wk-c'"
                )[0]["revoked_at"] == T0 + 260
    # Read-only: neither file changed by a single byte.
    assert (_file_sha256(s["old"]), _file_sha256(s["new"])) == before


@pytest.mark.usefixtures("pinned")
def test_same_period_exported_twice_matches(tmp_path: Path) -> None:
    """Each export masks inputs with a fresh random key, so the hashes differ; the shape
    (which jobs had equal inputs) is what gets compared."""
    s = two_copies(tmp_path)
    again = tmp_path / "copy-1-again.sqlite3"
    export_copy(s["ledger"].path, again, UNTIL, int(s["clock"].t))
    assert (rows(again, "SELECT payload_hash FROM jobs ORDER BY job_id")
            != rows(s["old"], "SELECT payload_hash FROM jobs ORDER BY job_id"))
    assert compare_copies(s["old"], again)["ok"]


@pytest.mark.usefixtures("pinned")
def test_renamed_owner_labels_are_fine_but_merged_owners_are_not(tmp_path: Path) -> None:
    s = two_copies(tmp_path)
    labels = {r["worker_id"]: r["owner"]
              for r in rows(s["new"], "SELECT worker_id, owner FROM workers")}
    # Swap two labels: same grouping, different names. Copies may number owners differently.
    edit(s["new"], "UPDATE workers SET owner = CASE owner WHEN ? THEN ? WHEN ? THEN ? END "
         "WHERE owner IN (?, ?)", (labels["wk-a"], labels["wk-b"], labels["wk-b"],
                                   labels["wk-a"], labels["wk-a"], labels["wk-b"]))
    assert compare_copies(s["old"], s["new"])["ok"]
    # Now claim wk-b and wk-c were the same person all along. The grouping changed: flagged.
    edit(s["new"], "UPDATE workers SET owner = (SELECT owner FROM workers WHERE "
         "worker_id='wk-b') WHERE worker_id='wk-c'")
    result = compare_copies(s["old"], s["new"])
    assert not result["ok"]
    assert any(p.startswith("owner label of wk-") and "matches one it was different" in p
               for p in result["problems"]), result["problems"]


@pytest.mark.usefixtures("pinned")
def test_rewritten_settlement_is_caught(tmp_path: Path) -> None:
    """The operator later turns wk-b's outvoted (held) result into a confirmed one."""
    s = two_copies(tmp_path)
    edit(s["new"], "UPDATE submissions SET audit_status='confirmed' WHERE job_id=?",
         (s["ids"]["audit"],))
    result = compare_copies(s["old"], s["new"])
    assert not result["ok"]
    assert f"result for {s['ids']['audit']} changed: audit_status" in result["problems"]
    assert any("reward report for everything before" in p for p in result["problems"])
    assert result["report_sha256_up_to_older_until"] is None


@pytest.mark.usefixtures("pinned")
def test_moved_points_are_caught(tmp_path: Path) -> None:
    s = two_copies(tmp_path)
    edit(s["new"], "UPDATE submissions SET worker_id='wk-b' WHERE job_id=?",
         (s["ids"]["exact"],))
    edit(s["new"], "UPDATE jobs SET worker_id='wk-b' WHERE job_id=?", (s["ids"]["exact"],))
    problems = compare_copies(s["old"], s["new"])["problems"]
    assert f"result for {s['ids']['exact']} changed: worker_id" in problems
    assert f"job {s['ids']['exact']} changed: worker_id" in problems


@pytest.mark.usefixtures("pinned")
def test_deleted_hidden_test_is_caught(tmp_path: Path) -> None:
    """Quietly dropping wk-c's failed hidden test from history."""
    s = two_copies(tmp_path)
    job = s["ids"]["challenge"]
    for table in ("events", "job_exclusions", "submissions", "jobs"):
        edit(s["new"], f"DELETE FROM {table} WHERE job_id=?", (job,))
    problems = compare_copies(s["old"], s["new"])["problems"]
    assert f"job {job} was deleted" in problems
    assert f"result for {job} was deleted" in problems
    assert any(p.startswith("event ") and p.endswith("was deleted") for p in problems)


@pytest.mark.usefixtures("pinned")
def test_backdated_job_rig_and_event_are_caught(tmp_path: Path) -> None:
    s = two_copies(tmp_path)
    with closing(sqlite3.connect(s["new"])) as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(jobs)")]
        row = dict(zip(cols, db.execute("SELECT * FROM jobs WHERE job_id=?",
                                        (s["ids"]["later"],)).fetchone()))
        row.update(job_id="backdated-job", created_at=T0 + 1, updated_at=T0 + 1, status="expired",
                   worker_id=None)
        db.execute(f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                   [row[c] for c in cols])
        db.execute("INSERT INTO workers (worker_id, capabilities, registered_at) "
                   "VALUES ('wk-ghost', '[]', ?)", (T0 - 5,))
        db.execute("INSERT INTO events (job_id, worker_id, event, at) "
                   "VALUES (NULL, 'wk-a', 'worker_revoked', ?)", (T0 + 2,))
        db.commit()
    problems = compare_copies(s["old"], s["new"])["problems"]
    assert any(p.startswith("job backdated-job appears with a creation time before")
               for p in problems), problems
    assert any(p.startswith("rig wk-ghost appears registered before") for p in problems)
    assert any("(worker_revoked) appears before" in p for p in problems)


@pytest.mark.usefixtures("pinned")
def test_later_revocation_is_fine_but_backdated_revocation_is_not(tmp_path: Path) -> None:
    s = two_copies(tmp_path)
    assert compare_copies(s["old"], s["new"])["ok"]  # wk-c revoked at T0+260 > UNTIL
    edit(s["new"], "UPDATE workers SET revoked_at=? WHERE worker_id='wk-b'", (T0 + 5,))
    problems = compare_copies(s["old"], s["new"])["problems"]
    assert any(p.startswith("rig wk-b is now shown as revoked before") for p in problems)


@pytest.mark.usefixtures("pinned")
def test_split_or_joined_inputs_are_caught(tmp_path: Path) -> None:
    """The original, its re-run and its tie-breaker had the same input. A copy that says
    otherwise has rewritten which job re-checked which."""
    s = two_copies(tmp_path)
    edit(s["new"], "UPDATE jobs SET payload_hash=? WHERE job_id=?",
         ("f" * 64, s["ids"]["tiebreak"]))
    problems = compare_copies(s["old"], s["new"])["problems"]
    group = tuple(f"input hash of {s['ids'][k]}" for k in ("llm", "audit", "tiebreak"))
    assert any(p.startswith(group) for p in problems), problems


@pytest.mark.usefixtures("pinned")
def test_wrong_inputs_are_refused(tmp_path: Path) -> None:
    s = two_copies(tmp_path)
    with pytest.raises(CompareError, match="give the older copy first"):
        compare_copies(s["new"], s["old"])
    with pytest.raises(CompareError, match="full ledger, not a shared copy"):
        compare_copies(s["old"], s["ledger"].path)
    with pytest.raises(CompareError, match="file not found"):
        compare_copies(s["old"], tmp_path / "nope.sqlite3")
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"this is not a database at all, just some bytes" * 20)
    with pytest.raises(CompareError, match="not a readable ledger"):
        compare_copies(s["old"], junk)


@pytest.mark.usefixtures("pinned")
def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = two_copies(tmp_path)
    out = tmp_path / "compare.json"
    assert ledger_copy.main(["compare", str(s["old"]), str(s["new"]), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "OK: the newer copy keeps the older copy's history unchanged" in text
    assert json.loads(out.read_text())["ok"] is True
    # Never overwrites its output.
    assert ledger_copy.main(["compare", str(s["old"]), str(s["new"]), "--out", str(out)]) == 2
    assert "already exists" in capsys.readouterr().err
    # Wrong order: refused with a clear message.
    assert ledger_copy.main(["compare", str(s["new"]), str(s["old"])]) == 2
    assert "older copy first" in capsys.readouterr().err
    # A tampered newer copy: exit 1 and the problems listed.
    edit(s["new"], "UPDATE submissions SET reward_points=999 WHERE job_id=?",
         (s["ids"]["exact"],))
    assert ledger_copy.main(["compare", str(s["old"]), str(s["new"])]) == 1
    text = capsys.readouterr().out
    assert "PROBLEM:" in text and f"result for {s['ids']['exact']} changed: reward_points" in text
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.usefixtures("pinned")
def test_dropped_exclusion_and_unsound_copy_are_caught(tmp_path: Path) -> None:
    """Who was barred from re-checking a job is part of the history too; and each copy must
    also pass its own consistency check."""
    s = two_copies(tmp_path)
    edit(s["new"], "DELETE FROM job_exclusions WHERE job_id=? AND worker_id='wk-a'",
         (s["ids"]["audit"],))
    problems = compare_copies(s["old"], s["new"])["problems"]
    assert f"exclusion of wk-a from job {s['ids']['audit']} was deleted" in problems
    # Barring an extra rig from an old job after the fact is a rewrite too.
    edit(s["new"], "INSERT INTO job_exclusions (job_id, worker_id) VALUES (?, 'wk-c')",
         (s["ids"]["exact"],))
    problems = compare_copies(s["old"], s["new"])["problems"]
    assert f"exclusion of wk-c from job {s['ids']['exact']} was added later" in problems
    # wk-a "re-checked" its own result: the newer copy fails its own consistency check.
    edit(s["new"], "UPDATE submissions SET worker_id='wk-a' WHERE job_id=?", (s["ids"]["audit"],))
    problems = compare_copies(s["old"], s["new"])["problems"]
    assert any(p.startswith("copy-2.sqlite3 fails its own consistency check") for p in problems)


@pytest.mark.usefixtures("pinned")
def test_changed_revocation_time_is_caught(tmp_path: Path) -> None:
    s = two_copies(tmp_path)
    for path in (s["old"], s["new"]):  # wk-b was revoked inside period 1, in both copies
        edit(path, "UPDATE workers SET revoked_at=? WHERE worker_id='wk-b'", (T0 + 5,))
    assert compare_copies(s["old"], s["new"])["ok"]
    edit(s["new"], "UPDATE workers SET revoked_at=? WHERE worker_id='wk-b'", (T0 + 90,))
    assert "rig wk-b changed: revoked_at" in compare_copies(s["old"], s["new"])["problems"]
