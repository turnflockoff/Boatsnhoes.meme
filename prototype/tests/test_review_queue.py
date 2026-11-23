"""Threat model #7: the operator review queue lists held results a person should look at
(stuck disputes, three-way disagreements, failed hidden tests) with every answer side by
side. Read-only and operator-only; it never changes points, statuses or the audit trail."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app import review
from app.config import Settings
from app.main import create_app
from tests.auth_helpers import FirstPickRng, WorkerClient, enroll
from tests.test_audits import (OP, OPERATOR, Clock, claim, make, make_dispute,  # noqa: F401
                               pinned, sha, submit)

pytestmark = pytest.mark.usefixtures("pinned")


def queue(client) -> dict:
    r = client.get("/v1/operator/review", headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


def roles(case: dict) -> dict[str, dict]:
    return {a["role"]: a for a in case["answers"]}


def test_review_queue_is_operator_only_and_empty_when_nothing_is_held(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    assert client.get("/v1/operator/review").status_code == 403
    assert client.get("/v1/operator/review",
                      headers={"X-Operator-Token": "wrong-token-that-is-long-enough"}
                      ).status_code == 403
    q = queue(client)
    assert q["cases"] == [] and q["points_held"] == 0
    assert q["counts"] == {"needs_review": 0, "waiting": 0, "decided_by_majority": 0}
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["review"]["needs_review"] == 0
    assert not any("need a person" in w for w in health["warnings"])


def test_dispute_with_tiebreaker_running_is_waiting_not_needing_a_person(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    original, audit_id = make_dispute(client)
    (case,) = queue(client)["cases"]
    assert case["case_id"] == original and case["kind"] == "dispute"
    assert case["stage"] == "waiting" and case["needs_person"] is False
    assert case["points_held"] == 2
    answers = roles(case)
    assert set(answers) == {"original", "audit"}
    assert answers["original"]["result"] == "The Salty Hoe"
    assert answers["audit"]["job_id"] == audit_id and answers["audit"]["worker_id"] == "wk-b"
    assert answers["original"]["agrees_with"] == [] and answers["audit"]["agrees_with"] == []
    assert json.loads(case["input_preview"])["prompt"] == "Name a boat."
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["review"]["waiting"] == 1
    assert not any("need a person" in w for w in health["warnings"])


def test_tiebreaker_that_never_ran_needs_a_person_and_warns(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    original, _ = make_dispute(client)
    clock.t += 301  # only the two disputing rigs ever show up
    ledger.sweep(int(clock()))
    q = queue(client)
    (case,) = q["cases"]
    assert case["stage"] == "needs_review" and case["needs_person"] is True
    assert "tie-breaker ended without a result (expired)" in case["why"]
    assert case["age_seconds"] == 301 and q["oldest_needs_review_age_seconds"] == 301
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["status"] == "attention" and health["review"]["needs_review"] == 1
    assert any("1 held case(s) need a person" in w for w in health["warnings"])


def test_dispute_without_any_tiebreaker_needs_a_person(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, audit_rate=1.0, operator_token=OPERATOR, tiebreak=False)
    client = WorkerClient(create_app(settings, clock=Clock(), rng=FirstPickRng(1)))
    for w in ("wk-a", "wk-b", "wk-c"):
        enroll(client, w)
    make_dispute(client)
    (case,) = queue(client)["cases"]
    assert case["stage"] == "needs_review"
    assert "no tie-breaker was scheduled" in case["why"]


def test_three_different_answers_need_a_person(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    original, _ = make_dispute(client)
    tb = claim(client, "wk-c").json()["job_id"]
    submit(client, tb, "wk-c", "Yet another answer")
    (case,) = queue(client)["cases"]
    assert case["case_id"] == original and case["stage"] == "needs_review"
    assert case["why"] == "three runs, three different answers"
    assert set(roles(case)) == {"original", "audit", "tie-breaker"}
    assert case["points_held"] == 3


def test_majority_decision_is_listed_for_spot_checks_only(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    original, _ = make_dispute(client)
    tb = claim(client, "wk-c").json()["job_id"]
    submit(client, tb, "wk-c", "The  Salty Hoe\n")  # agrees with the original, spacing aside
    q = queue(client)
    (case,) = q["cases"]
    assert case["stage"] == "decided_by_majority" and case["needs_person"] is False
    answers = roles(case)
    # The task's own tolerance rule decides who agrees with whom.
    assert answers["original"]["agrees_with"] == ["tie-breaker"]
    assert answers["tie-breaker"]["agrees_with"] == ["original"]
    assert answers["audit"]["agrees_with"] == [] and answers["audit"]["audit_status"] == "outvoted"
    assert answers["audit"]["worker_strikes"] == 1  # outvoted counts as a strike
    assert case["points_held"] == 1  # only the outvoted result is held
    # needs_person_only hides it.
    r = client.get("/v1/operator/review", params={"needs_person_only": True}, headers=OP)
    assert r.json()["cases"] == [] and r.json()["counts"]["decided_by_majority"] == 1


def test_wrong_answer_to_exactly_checked_hidden_test_is_refused_not_held(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    c = client.post("/v1/challenges", headers=OP,
                    json={"payload": "known", "expected_result": sha("known")}).json()
    assert claim(client, "wk-b").json()["job_id"] == c["job_id"]
    wrong = hashlib.sha256(b"something else").hexdigest()
    # demo:sha256 is checked exactly, so a wrong answer is refused outright; the only way to
    # fail a hidden test is an answer that passes the task's own check, like an AI one.
    assert submit(client, c["job_id"], "wk-b", wrong).status_code == 422
    assert queue(client)["cases"] == []


def test_failed_hidden_ai_test_is_a_review_case(tmp_path: Path) -> None:
    from tests.test_audits import llm_payload
    client, _, _ = make(tmp_path, audit_rate=0.0)
    c = client.post("/v1/challenges", headers=OP,
                    json={"task_type": "llm:generate", "payload": llm_payload("Hidden?"),
                          "expected_result": "The Salty Hoe"}).json()
    assert claim(client, "wk-b").json()["job_id"] == c["job_id"]
    assert submit(client, c["job_id"], "wk-b", "A made-up answer").status_code == 200
    (case,) = queue(client)["cases"]
    assert case["kind"] == "failed_hidden_test" and case["stage"] == "needs_review"
    answers = roles(case)
    assert answers["expected"]["result"] == "The Salty Hoe"
    assert answers["expected"]["worker_id"] is None
    assert answers["hidden-test answer"]["worker_id"] == "wk-b"
    assert answers["hidden-test answer"]["agrees_with"] == []
    assert case["points_held"] == 1


def test_confirmed_results_never_show_up(tmp_path: Path) -> None:
    from tests.test_audits import run_llm_job
    client, _, _ = make(tmp_path)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    audit = claim(client, "wk-b").json()["job_id"]
    submit(client, audit, "wk-b", "The Salty Hoe")
    assert queue(client)["cases"] == []
    assert original


def test_review_is_read_only(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    make_dispute(client)
    clock.t += 301
    ledger.sweep(int(clock()))
    before_events = len(ledger.events())
    before_points = client.get("/v1/ledger").json()["points_by_status"]
    files = [f for f in (ledger.path, Path(f"{ledger.path}-wal")) if f.exists()]
    db_bytes = [f.read_bytes() for f in files]
    for _ in range(2):
        queue(client)
        review.review_queue(ledger, int(clock()))
    assert len(ledger.events()) == before_events
    assert client.get("/v1/ledger").json()["points_by_status"] == before_points
    assert [f.read_bytes() for f in files] == db_bytes
    assert ledger.check_consistency() == []


def test_answers_stay_out_of_public_views(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    original, audit_id = make_dispute(client)
    for path in ("/v1/ledger", f"/v1/jobs/{original}", f"/v1/jobs/{audit_id}", "/health"):
        assert "Salty" not in client.get(path).text


def test_needs_review_cases_come_first_then_oldest(tmp_path: Path) -> None:
    groups = [
        {"root": "w1", "jobs": [
            {"job_id": "w1", "job_kind": "work", "task_type": "llm:generate", "payload": "p",
             "status": "accepted", "expected_result": None, "created_at": 1, "worker_id": "a",
             "result": "x", "audit_status": "outvoted", "reward_points": 1, "accepted_at": 1,
             "owner": "oa"}]},
        {"root": "c2", "jobs": [
            {"job_id": "c2", "job_kind": "challenge", "task_type": "llm:generate", "payload": "p",
             "status": "accepted", "expected_result": "y", "created_at": 5, "worker_id": "b",
             "result": "z", "audit_status": "failed-challenge", "reward_points": 2,
             "accepted_at": 50, "owner": "ob"}]},
        {"root": "c1", "jobs": [
            {"job_id": "c1", "job_kind": "challenge", "task_type": "llm:generate", "payload": "p",
             "status": "accepted", "expected_result": "y", "created_at": 4, "worker_id": "b",
             "result": "z", "audit_status": "failed-challenge", "reward_points": 2,
             "accepted_at": 10, "owner": "ob"}]},
    ]
    q = review.build_queue(groups, now=100, strikes={"b": 2})
    assert [c["case_id"] for c in q["cases"]] == ["c1", "c2", "w1"]
    assert q["oldest_needs_review_age_seconds"] == 90 and q["points_held"] == 5
    assert q["cases"][0]["answers"][1]["worker_strikes"] == 2


def test_cli_prints_cases_and_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    client, clock, ledger = make(tmp_path)
    make_dispute(client)
    clock.t += 301
    ledger.sweep(int(clock()))
    assert review.main(["--db", str(ledger.path)]) == 0
    out = capsys.readouterr().out
    assert "1 need a person" in out and "[needs_review]" in out
    assert "The Salty Hoe" in out and "A totally different boat" in out
    assert review.main(["--db", str(ledger.path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["needs_review"] == 1
    assert review.main(["--db", str(tmp_path / "missing.sqlite3")]) == 2
