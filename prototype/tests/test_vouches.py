"""Threat model #3 (`vouch-check-v1`): who agreed with a rig that was later caught.

A colluding auditor confirms a friend's fake answer and both look "confirmed". When the
friend is later caught (failed hidden test or outvoted), every rig whose re-run *agreed*
with it becomes a lead worth checking, and every result whose only independent agreement
came from a caught rig is a weak confirmation. Read-only and operator-only: it changes no
points, statuses or picks."""
from __future__ import annotations

import json
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app import vouches
from tests.auth_helpers import enroll
from tests.test_audits import (  # noqa: F401
    OP,
    claim,
    llm_payload,
    make,
    pinned,
    run_llm_job,
    sha,
    submit,
)

pytestmark = pytest.mark.usefixtures("pinned")


class PreferRng(random.Random):
    """Auditor pick: the rig named in `prefer` when it is eligible, else the first by name."""

    prefer: str | None = None

    def choice(self, seq: Sequence[Any]) -> Any:
        return self.prefer if self.prefer in seq else min(seq)


def make_vouch(tmp_path: Path):
    client, clock, ledger = make(tmp_path)
    ledger.rng = PreferRng(1)
    return client, clock, ledger


def report(client) -> dict:
    r = client.get("/v1/operator/vouches", headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


def warnings(client) -> list[str]:
    return client.get("/v1/operator/health", headers=OP).json()["warnings"]


def confirmed_pair(client, original_worker: str, auditor: str, text: str) -> tuple[str, str]:
    """original_worker runs an AI job, auditor's re-run agrees (both 'confirmed')."""
    client.app.state.ledger.rng.prefer = auditor
    job = client.post("/v1/jobs", json={"task_type": "llm:generate",
                                        "payload": llm_payload(text)})
    assert job.status_code == 200, job.text
    job_id = job.json()["job_id"]
    assert claim(client, original_worker).json()["job_id"] == job_id
    assert submit(client, job_id, original_worker, f"answer to {text}").status_code == 200
    audit = claim(client, auditor).json()
    assert audit["payload"] == llm_payload(text)
    assert submit(client, audit["job_id"], auditor, f"answer to {text}").status_code == 200
    return job_id, audit["job_id"]


def fail_hidden_test(client, worker: str, prompt: str = "Name a secret ship.") -> str:
    c = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload(prompt),
        "expected_result": "The Flagship", "target_worker": worker})
    assert c.status_code == 200, c.text
    assert claim(client, worker).json()["job_id"] == c.json()["job_id"]
    assert submit(client, c.json()["job_id"], worker, "Made up").status_code == 200
    return c.json()["job_id"]


def pass_hidden_test(client, worker: str) -> None:
    c = client.post("/v1/challenges", headers=OP, json={
        "payload": f"known-{worker}", "expected_result": sha(f"known-{worker}"),
        "target_worker": worker})
    assert c.status_code == 200, c.text
    assert claim(client, worker).json()["job_id"] == c.json()["job_id"]
    assert submit(client, c.json()["job_id"], worker, sha(f"known-{worker}")).status_code == 200


def test_operator_only_and_empty_without_caught_rigs(tmp_path: Path) -> None:
    client, _, _ = make_vouch(tmp_path)
    assert client.get("/v1/operator/vouches").status_code == 403
    assert client.get("/v1/operator/vouches",
                      headers={"X-Operator-Token": "wrong-token-that-is-long-enough"}
                      ).status_code == 403
    confirmed_pair(client, "wk-a", "wk-b", "Name a boat.")  # agreement, but nobody caught
    r = report(client)
    assert r["rule"] == "vouch-check-v1"
    assert r["caught_rigs"] == [] and r["cases"] == [] and r["leads"] == []
    assert r["weak_confirmations"] == {"results": 0, "points": 0}
    assert not any("vouch" in w for w in warnings(client))


def test_rig_that_confirmed_a_caught_rigs_work_is_a_lead(tmp_path: Path) -> None:
    client, _, _ = make_vouch(tmp_path)
    original, audit = confirmed_pair(client, "wk-a", "wk-b", "Name a boat.")
    fail_hidden_test(client, "wk-a")
    r = report(client)
    assert [c["worker_id"] for c in r["caught_rigs"]] == ["wk-a"]
    assert r["caught_rigs"][0]["strikes"] == 1
    (case,) = r["cases"]
    assert case["case_id"] == original and case["task_type"] == "llm:generate"
    roles = {m["role"]: m for m in case["agreed"]}
    assert roles["original"]["worker_id"] == "wk-a" and roles["original"]["caught"] is True
    assert roles["audit"]["worker_id"] == "wk-b" and roles["audit"]["caught"] is False
    assert roles["audit"]["job_id"] == audit and roles["audit"]["owner"] == "owner-wk-b"
    (lead,) = r["leads"]
    assert lead["worker_id"] == "wk-b" and lead["owner"] == "owner-wk-b"
    assert lead["agreed_with_caught"] == 1 and lead["caught_partners"] == {"wk-a": 1}
    assert lead["hidden_tests_passed"] == 0 and lead["also_caught"] is False
    assert lead["revoked"] is False
    assert "target_worker" in lead["next_step"] and "wk-b" in lead["next_step"]
    # wk-b's own confirmed result rests only on the caught rig's agreement.
    assert r["weak_confirmations"] == {"results": 1, "points": 1}
    assert case["weak_confirmations"] == [audit]
    # Health names the unchecked lead and points at the report.
    (warning,) = [w for w in warnings(client) if "vouches" in w]
    assert "wk-b" in warning and "never passed a hidden test" in warning


def test_caught_rig_as_auditor_makes_its_confirmations_weak(tmp_path: Path) -> None:
    client, _, _ = make_vouch(tmp_path)
    original, _ = confirmed_pair(client, "wk-b", "wk-a", "Name a boat.")
    fail_hidden_test(client, "wk-a")
    r = report(client)
    (case,) = r["cases"]
    assert case["weak_confirmations"] == [original]  # wk-b's result, vouched for by wk-a
    (lead,) = r["leads"]
    assert lead["worker_id"] == "wk-b" and lead["caught_partners"] == {"wk-a": 1}


def test_passing_an_aimed_hidden_test_clears_the_warning_not_the_lead(tmp_path: Path) -> None:
    client, _, _ = make_vouch(tmp_path)
    confirmed_pair(client, "wk-a", "wk-b", "Name a boat.")
    fail_hidden_test(client, "wk-a")
    assert any("vouches" in w for w in warnings(client))
    pass_hidden_test(client, "wk-b")
    (lead,) = report(client)["leads"]
    assert lead["worker_id"] == "wk-b" and lead["hidden_tests_passed"] == 1
    assert not any("vouches" in w for w in warnings(client))


def test_revoked_lead_is_listed_but_not_warned_about(tmp_path: Path) -> None:
    client, _, _ = make_vouch(tmp_path)
    confirmed_pair(client, "wk-a", "wk-b", "Name a boat.")
    fail_hidden_test(client, "wk-a")
    assert client.post("/v1/workers/wk-b/revoke", headers=OP).status_code == 200
    (lead,) = report(client)["leads"]
    assert lead["revoked"] is True
    assert not any("vouches" in w for w in warnings(client))


def test_outvoted_rigs_opponents_are_not_leads(tmp_path: Path) -> None:
    """wk-b and wk-c agreed with each other *against* the caught rig: not a lead."""
    client, _, _ = make_vouch(tmp_path)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    audit = claim(client, "wk-b").json()["job_id"]
    submit(client, audit, "wk-b", "A totally different boat")
    tb = claim(client, "wk-c").json()["job_id"]
    submit(client, tb, "wk-c", "A totally different boat")
    assert client.app.state.ledger.submission(original)["audit_status"] == "outvoted"
    r = report(client)
    assert [c["worker_id"] for c in r["caught_rigs"]] == ["wk-a"]
    assert r["cases"] == [] and r["leads"] == []


def test_tiebreaker_that_sided_with_a_later_caught_rig_is_a_lead(tmp_path: Path) -> None:
    client, _, _ = make_vouch(tmp_path)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    audit = claim(client, "wk-b").json()["job_id"]
    submit(client, audit, "wk-b", "A totally different boat")
    tb = claim(client, "wk-c").json()["job_id"]
    submit(client, tb, "wk-c", "The Salty Hoe")  # sides with wk-a; wk-b outvoted
    fail_hidden_test(client, "wk-a")
    r = report(client)
    assert {c["worker_id"] for c in r["caught_rigs"]} == {"wk-a", "wk-b"}
    (case,) = r["cases"]
    assert case["case_id"] == original
    assert {m["role"] for m in case["agreed"]} == {"original", "tie-breaker"}
    assert case["weak_confirmations"] == [tb]
    (lead,) = r["leads"]
    assert lead["worker_id"] == "wk-c" and lead["caught_partners"] == {"wk-a": 1}


def test_two_caught_rigs_that_agreed_are_flagged_first(tmp_path: Path) -> None:
    client, _, _ = make_vouch(tmp_path)
    enroll(client, "wk-d")
    confirmed_pair(client, "wk-a", "wk-b", "Name a boat.")
    confirmed_pair(client, "wk-c", "wk-d", "Name a ship.")
    confirmed_pair(client, "wk-c", "wk-b", "Name a dock.")  # b agreed with a and c
    fail_hidden_test(client, "wk-a", "Secret one.")
    fail_hidden_test(client, "wk-b", "Secret two.")
    fail_hidden_test(client, "wk-c", "Secret three.")
    r = report(client)
    leads = {lead["worker_id"]: lead for lead in r["leads"]}
    assert set(leads) == {"wk-a", "wk-b", "wk-c", "wk-d"}
    assert leads["wk-b"]["also_caught"] is True
    assert leads["wk-b"]["caught_partners"] == {"wk-a": 1, "wk-c": 1}
    assert leads["wk-d"]["also_caught"] is False
    # Caught rigs that agreed with each other come first, then by agreements.
    assert r["leads"][0]["worker_id"] == "wk-b"
    # a+b and c+b: neither side is independent (4); c+d: only d's rests on c (1).
    assert r["weak_confirmations"]["results"] == 5


def test_report_is_pure_and_ignores_same_worker_and_single_confirmations() -> None:
    groups = [
        {"root": "j1", "task_type": "demo:sha256", "agreed": [
            {"job_id": "j1", "job_kind": "work", "worker_id": "x", "reward_points": 3,
             "accepted_at": 10}]},  # only one confirmed answer: no agreement
        {"root": "j2", "task_type": "demo:sha256", "agreed": [
            {"job_id": "j2", "job_kind": "work", "worker_id": "x", "reward_points": 3,
             "accepted_at": 10},
            {"job_id": "j3", "job_kind": "audit", "worker_id": "x", "reward_points": 3,
             "accepted_at": 11}]},  # can't happen (self-audit blocked), but don't count it
    ]
    record = {"x": {"owner": "o", "revoked": False, "strikes": 1, "hidden_tests_passed": 0,
                    "first_struck_result_at": 5}}
    r = vouches.build_report(groups, record, now=100)
    assert r["cases"] == [] and r["leads"] == []
    assert r["caught_rigs"] == [{"worker_id": "x", "owner": "o", "strikes": 1,
                                 "first_struck_result_at": 5, "revoked": False}]


def test_cli_reads_the_database_read_only(tmp_path: Path, capsys) -> None:
    client, _, ledger = make_vouch(tmp_path)
    confirmed_pair(client, "wk-a", "wk-b", "Name a boat.")
    fail_hidden_test(client, "wk-a")
    db = client.app.state.settings.database_path
    before = db.stat().st_mtime_ns, ledger.events()
    assert vouches.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "wk-b" in out and "1 caught rig" in out and "target_worker" in out
    assert "answer to" not in out  # no answers or prompts in the report
    assert vouches.main(["--db", str(db), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["leads"][0]["worker_id"] == "wk-b"
    assert (db.stat().st_mtime_ns, ledger.events()) == before
    assert vouches.main(["--db", str(tmp_path / "missing.sqlite3")]) == 2


def test_report_never_contains_answers_or_prompts(tmp_path: Path) -> None:
    client, _, _ = make_vouch(tmp_path)
    confirmed_pair(client, "wk-a", "wk-b", "Name a boat.")
    fail_hidden_test(client, "wk-a")
    text = json.dumps(report(client))
    assert "answer to" not in text and "Name a" not in text and "Made up" not in text


def _row(job_id: str, kind: str, worker: str) -> dict:
    return {"job_id": job_id, "job_kind": kind, "worker_id": worker, "reward_points": 2,
            "accepted_at": 10}


def _rec(strikes: int = 0, passed: int = 0) -> dict:
    return {"owner": "o", "revoked": False, "strikes": strikes, "hidden_tests_passed": passed,
            "first_struck_result_at": 5 if strikes else None}


def test_weak_only_when_every_partner_is_caught_and_caught_leads_rank_first() -> None:
    groups = [
        # Three agreeing answers: y agreed with caught x AND with honest z, so y's
        # confirmation still has one independent partner and isn't weak; z's neither.
        {"root": "g1", "task_type": "llm:generate", "agreed": [
            _row("g1", "work", "x"), _row("g1a", "audit", "y"), _row("g1t", "tiebreak", "z")]},
        {"root": "g2", "task_type": "llm:generate", "agreed": [
            _row("g2", "work", "x"), _row("g2a", "audit", "y")]},
        {"root": "g3", "task_type": "llm:generate", "agreed": [
            _row("g3", "work", "q"), _row("g3a", "audit", "c")]},
    ]
    record = {"x": _rec(1), "y": _rec(), "z": _rec(), "q": _rec(1), "c": _rec(2)}
    r = vouches.build_report(groups, record, now=100)
    weak = {c["case_id"]: c["weak_confirmations"] for c in r["cases"]}
    assert weak == {"g1": [], "g2": ["g2a"], "g3": ["g3", "g3a"]}
    # y agreed with a caught rig twice, but c and q (caught, agreeing with each other)
    # rank first, even with fewer agreements.
    assert [lead["worker_id"] for lead in r["leads"]] == ["c", "q", "y", "z"]
    assert r["unchecked_leads"] == ["y", "z"]
