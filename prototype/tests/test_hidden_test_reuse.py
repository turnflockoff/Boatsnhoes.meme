"""Retired hidden tests (`test-reuse-v1`, threat model #11 and #14).

A ledger copy shows which jobs before its `until` were hidden tests, and each rig remembers
the inputs of the jobs it ran. So after a copy is made, reusing one of those inputs as a new
hidden test would let the rig recognise the test and answer honestly only on it. Every
export is recorded in the live ledger, and a hidden test with the exact input of one from a
period a copy covers is refused. Near copies (`test-reuse-v2`) are tested in
tests/test_hidden_test_near_copies.py.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import ledger_copy
from app.ledger import COPY_MADE_EVENT, Ledger
from app.ledger_compare import compare_copies
from app.ledger_copy import CopyError, export_copy
from tests.test_audits import OP, claim, llm_payload, make, pinned, submit  # noqa: F401
from tests.test_ledger_copy import (
    CHALLENGE_PROMPT,
    EXPECTED,
    PROMPT,
    UNTIL,
    busy_period,
    rows,
)

pytestmark = pytest.mark.usefixtures("pinned")


def hidden_test(client, prompt: str = CHALLENGE_PROMPT, **extra):
    return client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload(prompt),
        "expected_result": EXPECTED, **extra})


def job_count(ledger: Ledger) -> int:
    return rows(ledger.path, "SELECT COUNT(*) AS n FROM jobs")[0]["n"]


def operator_health(client) -> dict:
    r = client.get("/v1/operator/health", headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


def exported(tmp_path: Path, name: str = "copy.sqlite3"):
    client, clock, ledger = make(tmp_path)
    busy_period(client)
    clock.t += 200
    out = export_copy(ledger.path, tmp_path / name, UNTIL, int(clock.t))
    return client, clock, ledger, out


def test_revealed_input_is_refused_as_a_new_hidden_test(tmp_path: Path) -> None:
    client, _clock, ledger, _ = exported(tmp_path)
    before = job_count(ledger)
    r = hidden_test(client)
    assert r.status_code == 409
    assert "ledger copy" in r.json()["detail"] and str(UNTIL) in r.json()["detail"]
    assert EXPECTED not in r.text and CHALLENGE_PROMPT not in r.text
    assert job_count(ledger) == before  # nothing queued, nothing half-written
    # Aimed tests go through the same check.
    assert hidden_test(client, target_worker="wk-a").status_code == 409
    assert hidden_test(client, target_tier="new").status_code == 409
    assert job_count(ledger) == before


def test_fresh_inputs_and_normal_jobs_are_still_fine(tmp_path: Path) -> None:
    client, *_ = exported(tmp_path)
    assert hidden_test(client, "A brand new question").status_code == 200
    # The period's normal AI job had PROMPT as input; it wasn't a test, so no tell.
    assert hidden_test(client, PROMPT).status_code == 200
    # Only hidden tests are refused: a normal job with that input reveals nothing.
    r = client.post("/v1/jobs", json={"task_type": "llm:generate",
                                      "payload": llm_payload(CHALLENGE_PROMPT)})
    assert r.status_code == 200


def test_tests_made_after_the_copy_period_can_still_repeat(tmp_path: Path) -> None:
    """A copy only reveals hidden tests created before its `until`."""
    client, *_ = exported(tmp_path)
    assert hidden_test(client, "Made after the period").status_code == 200
    assert hidden_test(client, "Made after the period").status_code == 200


def test_reuse_is_allowed_before_any_copy_but_shows_a_warning(tmp_path: Path) -> None:
    client, _clock, _ledger = make(tmp_path)
    health = operator_health(client)
    assert health["hidden_test_reuse"] == {"rule": "test-reuse-v2",
                                           "fingerprint": "input-fingerprint-v1",
                                           "revealed_until": None, "retired_inputs": 0,
                                           "open_sharing_input": 0}
    assert hidden_test(client).status_code == 200
    assert not any("same input" in w for w in operator_health(client)["warnings"])
    assert hidden_test(client).status_code == 200
    health = operator_health(client)
    assert health["hidden_test_reuse"]["open_sharing_input"] == 2
    assert any("2 open hidden test(s) use the same input" in w for w in health["warnings"])
    # The warning never names the input or the expected answer.
    assert CHALLENGE_PROMPT not in str(health) and EXPECTED not in str(health)
    # Once both are answered they're no longer open, so the warning goes away.
    for _ in range(2):
        job = claim(client, "wk-a").json()
        assert submit(client, job["job_id"], "wk-a", EXPECTED).status_code == 200
    health = operator_health(client)
    assert health["hidden_test_reuse"]["open_sharing_input"] == 0
    assert not any("same input" in w for w in health["warnings"])


def test_export_is_recorded_in_the_live_ledger(tmp_path: Path) -> None:
    client, clock, ledger, out = exported(tmp_path)
    recorded = rows(ledger.path, "SELECT detail, at, job_id, worker_id FROM events "
                                 "WHERE event=?", (COPY_MADE_EVENT,))
    assert len(recorded) == 1 and recorded[0]["at"] == clock.t
    assert recorded[0]["job_id"] is None and recorded[0]["worker_id"] is None
    assert f'"until": {UNTIL}' in recorded[0]["detail"]
    assert out["file_sha256"] in recorded[0]["detail"]
    assert ledger.revealed_until() == UNTIL
    health = operator_health(client)["hidden_test_reuse"]
    assert health["revealed_until"] == UNTIL and health["retired_inputs"] == 1
    # An older period exported later doesn't move the line back.
    export_copy(ledger.path, tmp_path / "older.sqlite3", UNTIL - 50, int(clock.t))
    assert ledger.revealed_until() == UNTIL
    # The copy itself doesn't contain its own record.
    assert rows(tmp_path / "copy.sqlite3", "SELECT COUNT(*) AS n FROM events WHERE event=?",
                (COPY_MADE_EVENT,))[0]["n"] == 0


def test_later_copies_carry_the_record_and_still_compare_cleanly(tmp_path: Path) -> None:
    _client, clock, ledger, _ = exported(tmp_path, "older.sqlite3")
    clock.t += 200
    export_copy(ledger.path, tmp_path / "newer.sqlite3", int(clock.t) - 10, int(clock.t))
    kept = rows(tmp_path / "newer.sqlite3", "SELECT detail FROM events WHERE event=?",
                (COPY_MADE_EVENT,))
    assert kept == [{"detail": None}]  # the fact survives, the details don't
    assert compare_copies(tmp_path / "older.sqlite3", tmp_path / "newer.sqlite3")["ok"]
    # Inside a copy the record has no `until`, so nothing is read as revealed there.
    assert Ledger(tmp_path / "newer.sqlite3", read_only=True).revealed_until() is None


def test_exporting_from_a_copy_is_refused_and_leaves_it_unchanged(tmp_path: Path) -> None:
    _client, clock, _ledger, out = exported(tmp_path)
    with pytest.raises(CopyError, match="already a ledger copy"):
        export_copy(tmp_path / "copy.sqlite3", tmp_path / "again.sqlite3", UNTIL, int(clock.t))
    assert ledger_copy._file_sha256(tmp_path / "copy.sqlite3") == out["file_sha256"]
    assert not (tmp_path / "again.sqlite3").exists()


def test_unrecorded_copy_is_deleted(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    busy_period(client)
    clock.t += 200

    def broken(self, now, until, file_sha256):
        raise sqlite3.OperationalError("database is locked")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Ledger, "record_copy", broken)
        with pytest.raises(CopyError, match="could not record") as err:
            export_copy(ledger.path, tmp_path / "copy.sqlite3", UNTIL, int(clock.t))
    assert "locked" not in str(err.value)  # error type only, no details
    assert list(tmp_path.glob("copy.sqlite3*")) == []
    assert hidden_test(client).status_code == 200  # nothing recorded, nothing refused


def test_export_is_recorded_while_paused(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    busy_period(client)
    clock.t += 200
    ledger.pause(int(clock.t), "checking something", "test")
    export_copy(ledger.path, tmp_path / "copy.sqlite3", UNTIL, int(clock.t))
    assert ledger.revealed_until() == UNTIL


def test_cli_export_records_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, clock, ledger = make(tmp_path)
    busy_period(client)
    clock.t += 200
    monkeypatch.setattr(ledger_copy.time, "time", lambda: float(clock.t))
    code = ledger_copy.main(["export", "--db", str(ledger.path), "--until", str(UNTIL),
                             "--out", str(tmp_path / "copy.sqlite3")])
    assert code == 0
    assert ledger.revealed_until() == UNTIL
    assert hidden_test(client).status_code == 409


def test_refusal_checks_only_hidden_tests_from_before_until(tmp_path: Path) -> None:
    """Direct ledger check, independent of the HTTP layer."""
    client, clock, ledger = make(tmp_path)
    busy_period(client)
    with closing(sqlite3.connect(ledger.path)) as db:
        created = db.execute("SELECT created_at FROM jobs WHERE job_kind='challenge'"
                             ).fetchone()[0]
    clock.t += 200
    ledger.record_copy(int(clock.t), created, "0" * 64)  # copy ends right at that test
    assert hidden_test(client).status_code == 200
    ledger.record_copy(int(clock.t), created + 1, "1" * 64)
    assert hidden_test(client).status_code == 409


def test_a_garbled_record_is_ignored_not_fatal(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    for detail in ('{"until": "soon"}', "not json", '{"until": null}', "[]"):
        ledger.note(int(clock.t), COPY_MADE_EVENT, detail=detail)
    assert ledger.revealed_until() is None
    assert hidden_test(client).status_code == 200
    assert operator_health(client)["hidden_test_reuse"]["revealed_until"] is None
