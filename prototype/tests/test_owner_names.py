"""Owner labels and worker ids that look the same (`owner-name-v1`).

Code audit, twenty-seventh pass (0.5.79). The rule "rigs with the same owner never audit or
break ties on each other's results" (threat model #2, #3, #6) compared owner labels exactly.
Found on the old tree, with an in-process coordinator:
  * Codes made for owner `Alice` and for owner `alice` (the operator typing the same person
    two ways on two days) gave two rigs that the coordinator treated as different people:
    the second rig was handed the hidden re-run of the first rig's AI result (200) and its
    agreeing answer marked it "confirmed". The consistency check saw nothing wrong.
  * `bob-smith`, `bob_smith` and `bob.smith` were also three different owners.
  * A published ledger copy gave `Alice` and `alice` two different labels (`owner-1`,
    `owner-2`), so outsiders checking it saw two people too.
  * A new rig could register as `WK-A` or `wk_a` next to an existing `wk-a`: a name that reads
    as the same rig on the operator's screen, in the review queue and in reports.

Now owner labels are saved in lower case, a new label that differs from an existing one only
by `.`, `_` or `-` is refused with the existing name, and every owner comparison ignores
letter case (so ledgers written before this change are covered without rewriting them). A
worker id that matches an existing one after ignoring letter case and `.`, `_`, `-`, `:` is
refused at registration (409).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import owners, workers
from app.ledger_copy import export_copy
from tests.test_worker_auth import (OP, claim, code_for, join, llm_job, make, pinned,  # noqa: F401
                                    register, submit)


def test_same_person_typed_two_ways_never_audits_itself(pinned, tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, audit_rate=1.0)
    a1 = join(client, "alice-1", "Alice")
    a2 = join(client, "alice-2", "alice")
    b1 = join(client, "bob-1", "bob")
    original = llm_job(client)
    assert claim(client, a1).json()["job_id"] == original
    assert submit(client, a1, original, "The Salty Hoe").status_code == 200
    assert ledger.submission(original)["audit_status"] == "pending"
    assert claim(client, a2).status_code == 404       # old tree: 200, alice re-ran her own job
    audit = claim(client, b1).json()["job_id"]
    assert submit(client, b1, audit, "The Salty Hoe").status_code == 200
    assert ledger.submission(original)["audit_status"] == "confirmed"
    assert ledger.check_consistency() == []


def test_owner_labels_are_saved_in_lower_case(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    r = client.post("/v1/enrollments", json={"owner": "Alice"}, headers=OP)
    assert r.status_code == 200 and r.json()["owner"] == "alice"
    reg = register(client, "rig-1", r.json()["enrollment_code"])
    assert reg.status_code == 200 and reg.json()["owner"] == "alice"
    assert ledger.get_worker("rig-1")["owner"] == "alice"


@pytest.mark.parametrize("second", ["bob_smith", "bob.smith", "Bob.Smith", "bobsmith", "bob--smith"])
def test_owner_that_differs_only_by_dot_dash_underscore_is_refused(tmp_path: Path,
                                                                   second: str) -> None:
    client, _, _ = make(tmp_path)
    code_for(client, "bob-smith")
    r = client.post("/v1/enrollments", json={"owner": second}, headers=OP)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "looks like the existing owner 'bob-smith'" in detail
    # The exact existing name, in any letter case, is the same person and is fine.
    assert client.post("/v1/enrollments", json={"owner": "BOB-SMITH"},
                       headers=OP).status_code == 200


def test_lookalike_check_sees_owners_of_registered_rigs_and_unused_codes(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    join(client, "rig-1", "carol.jones")                  # a registered rig
    code_for(client, "dave-lee")                          # a code nobody used yet
    for owner in ("carol_jones", "dave.lee"):
        assert client.post("/v1/enrollments", json={"owner": owner},
                           headers=OP).status_code == 422
    with pytest.raises(owners.OwnerLookalike):
        workers.issue_code(ledger, "Carol-Jones", 3600, 1_800_000_000)
    assert client.post("/v1/enrollments", json={"owner": "carol-jonas"},
                       headers=OP).status_code == 200      # a different name is fine


def test_cli_refuses_a_lookalike_owner_and_prints_the_saved_label(
        tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    client, clock, ledger = make(tmp_path)
    monkeypatch.setattr(workers.time, "time", lambda: float(clock.t))
    code_for(client, "erin-o")
    assert workers.main(["--db", str(ledger.path), "enroll", "--owner", "erin_o"]) == 2
    assert "looks like the existing owner 'erin-o'" in capsys.readouterr().err
    assert workers.main(["--db", str(ledger.path), "enroll", "--owner", "Frank"]) == 0
    assert "owner=frank " in capsys.readouterr().err  # the code alone goes to stdout


@pytest.mark.parametrize("twin", ["WK-A", "wk_a", "wk.a", "Wk:A", "wka"])
def test_worker_id_that_reads_as_an_existing_one_is_refused(tmp_path: Path, twin: str) -> None:
    client, _, ledger = make(tmp_path)
    join(client, "wk-a")
    code = code_for(client, "someone-else")
    r = register(client, twin, code)
    assert r.status_code == 409, r.text
    assert "looks like the existing worker 'wk-a'" in r.json()["detail"]
    assert ledger.get_worker(twin) is None
    # The refusal didn't use the code up: a clearly different id still registers with it.
    assert register(client, "wk-b", code).status_code == 200


def test_lookalike_message_is_plain_and_short() -> None:
    hostile = "\x1b[2J" + "x" * 300
    message = owners.lookalike_message("worker", hostile)
    assert all(32 <= ord(ch) < 127 for ch in message), message
    assert len(message) < 200


def test_older_ledger_with_mixed_case_owners_is_covered_without_rewriting(
        pinned, tmp_path: Path) -> None:
    """A ledger written before this change can hold `Alice` and `alice`. The comparisons
    ignore letter case, so they are one owner without touching the stored rows."""
    client, _, ledger = make(tmp_path, audit_rate=1.0)
    a1 = join(client, "alice-1", "alice")
    a2 = join(client, "alice-2", "someone")
    b1 = join(client, "bob-1", "bob")
    db = sqlite3.connect(ledger.path)
    db.execute("UPDATE workers SET owner='Alice' WHERE worker_id='alice-2'")  # as old code wrote
    db.commit()
    db.close()
    original = llm_job(client)
    claim(client, a1)
    submit(client, a1, original, "The Salty Hoe")
    assert claim(client, a2).status_code == 404
    audit = claim(client, b1).json()["job_id"]
    assert ledger.check_consistency() == []
    db = sqlite3.connect(ledger.path)
    db.execute("UPDATE jobs SET worker_id='alice-2' WHERE job_id=?", (audit,))  # tamper
    db.commit()
    db.close()
    assert (f"job given to a worker with the same owner as an excluded worker {audit}"
            in ledger.check_consistency())


def test_published_copy_gives_one_label_per_owner_whatever_the_case(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    join(client, "alice-1", "alice")
    join(client, "alice-2", "someone")
    join(client, "bob-1", "bob")
    db = sqlite3.connect(ledger.path)
    db.execute("UPDATE workers SET owner='Alice' WHERE worker_id='alice-2'")  # as old code wrote
    db.commit()
    db.close()
    clock.t += 10
    out = tmp_path / "copy.sqlite3"
    export_copy(ledger.path, out, int(clock.t) - 1, int(clock.t))
    rows = dict(sqlite3.connect(out).execute("SELECT worker_id, owner FROM workers"))
    assert rows["alice-1"] == rows["alice-2"] != rows["bob-1"]


def test_keys() -> None:
    assert owners.canonical("Bob-Smith") == "bob-smith"
    assert owners.owner_key("Bob.Smith") == owners.owner_key("bob_smith") == "bobsmith"
    assert owners.worker_key("Wk:A-1") == owners.worker_key("wk_a.1") == "wka1"
    assert owners.owner_key("bob-smith") != owners.owner_key("bob-smyth")


def test_audit_already_given_to_a_same_owner_rig_by_older_code_is_not_handed_out(
        pinned, tmp_path: Path) -> None:
    """Older code compared labels exactly, so a ledger written by it can already have the
    re-run of `alice`'s job assigned to her `Alice` rig. The claim itself also ignores letter
    case, so that rig still can't take it; it waits for a fresh pick instead."""
    client, _, ledger = make(tmp_path, audit_rate=1.0)
    a1 = join(client, "alice-1", "alice")
    a2 = join(client, "alice-2", "someone")
    join(client, "bob-1", "bob")
    original = llm_job(client)
    claim(client, a1)
    submit(client, a1, original, "The Salty Hoe")
    db = sqlite3.connect(ledger.path)
    db.execute("UPDATE workers SET owner='Alice' WHERE worker_id='alice-2'")  # as old code wrote
    db.execute("UPDATE jobs SET assigned_worker='alice-2' WHERE audit_of=?", (original,))
    db.commit()
    db.close()
    assert claim(client, a2).status_code == 404


def test_published_copy_keeps_a_real_owner_called_owner_1_apart(tmp_path: Path) -> None:
    """Found while fixing the above: labels were swapped in with one UPDATE per owner, so a
    real owner literally called `owner-1` was merged with whoever got that label first."""
    client, clock, ledger = make(tmp_path)
    join(client, "rig-bob", "bob")
    clock.t += 1
    join(client, "rig-x", "owner-1")
    clock.t += 10
    out = tmp_path / "copy.sqlite3"
    export_copy(ledger.path, out, int(clock.t) - 1, int(clock.t))
    rows = dict(sqlite3.connect(out).execute("SELECT worker_id, owner FROM workers"))
    assert rows == {"rig-bob": "owner-1", "rig-x": "owner-2"}
