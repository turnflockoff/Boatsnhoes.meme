"""Scoring v2 draft `sim-points-v2-forfeit` (threat model #15, opt-in).

A rig with a strike in a period (failed hidden test, or outvoted) forfeits its unverified
points for that period. Confirmed points are kept. v1 stays the default and unchanged.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import rewards
from app.config import Settings, reward_scoring_env
from app.main import create_app
from app.signing import ManifestSigner
from tests.auth_helpers import FirstPickRng, WorkerClient, enroll, flat_audit_rate
from tests.test_audits import OP, OPERATOR, T0, Clock, claim, llm_payload, pinned, submit  # noqa: F401
from tests.test_audits import run_llm_job
from tests.test_rewards import by_worker, report, sha_job

V1, V2 = "sim-points-v1", "sim-points-v2-forfeit"


def make(tmp_path: Path, scoring: str = V2, audit_rate: float = 0.0):
    clock = Clock()
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, audit_rate=audit_rate, **flat_audit_rate(audit_rate),
                        operator_token=OPERATOR, rate_limit_per_minute=0,
                        reward_scoring=scoring)
    app = create_app(settings, clock=clock, rng=FirstPickRng(1))
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b", "wk-c"):
        enroll(client, w)
    return client, clock, app.state.ledger


def fail_hidden_test(client, worker: str) -> str:
    r = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload("Secret test"),
        "expected_result": "Known answer"})
    assert r.status_code == 200, r.text
    job_id = claim(client, worker).json()["job_id"]
    assert submit(client, job_id, worker, "Made-up answer").status_code == 200
    return job_id


def set_status(ledger, job_id: str, status: str) -> None:
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE submissions SET audit_status=? WHERE job_id=?", (status, job_id))


@pytest.mark.usefixtures("pinned")
def test_failed_hidden_test_forfeits_unverified_but_keeps_confirmed(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    sha_job(client, "wk-a", points=3)              # exact check -> confirmed
    run_llm_job(client, "wk-a", "The Salty Hoe")   # not re-run -> unverified (1)
    run_llm_job(client, "wk-a", "The Lazy Oar")    # unverified (1)
    run_llm_job(client, "wk-c", "The Honest Hull")  # someone else, no strike
    fail_hidden_test(client, "wk-a")
    rep = report(client)
    assert rep["scoring"]["version"] == V2
    w = by_worker(rep)
    assert w["wk-a"]["points"] == {"confirmed": 3, "unverified": 2, "pending": 0, "held": 1}
    assert w["wk-a"]["struck"] is True and w["wk-a"]["forfeited_points"] == 2
    assert w["wk-a"]["payable_points"] == 3
    assert w["wk-c"]["struck"] is False and w["wk-c"]["forfeited_points"] == 0
    assert w["wk-c"]["payable_points"] == 1
    assert rep["totals"]["forfeited_points"] == 2 and rep["totals"]["payable_points"] == 4
    assert rep["checks"]["ok"] is True
    assert rewards.verify_report(rep, ledger) == []
    # The same ledger under v1 pays the unverified work and has no penalty fields.
    v1 = rewards.build_report(ledger, 0, T0 + 1, V1)
    a1 = by_worker(v1)["wk-a"]
    assert a1["payable_points"] == 5 and "struck" not in a1 and "forfeited_points" not in a1
    assert "forfeited_points" not in v1["totals"]


@pytest.mark.usefixtures("pinned")
def test_v2_report_never_says_which_result_was_the_strike(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    run_llm_job(client, "wk-a", "The Salty Hoe")
    fail_hidden_test(client, "wk-a")
    rep = report(client)
    text = json.dumps(rep).lower()
    for leak in ("challenge", "audit_status", "job_kind", "known answer",
                 "made-up answer", "secret test", "expected_result"):
        assert leak not in text, leak
    # Per rig: only a yes/no and an amount. Per result: the same fields as v1.
    assert set(by_worker(rep)["wk-a"]) == {"worker_id", "accepted_results", "points",
                                           "payable_points", "activity", "struck",
                                           "forfeited_points"}
    v1_keys = set(rewards.build_report(client.app.state.ledger, 0, T0 + 1, V1)["entries"][0])
    assert all(set(e) == v1_keys for e in rep["entries"])


@pytest.mark.usefixtures("pinned")
@pytest.mark.parametrize("status,struck", [
    ("outvoted", True), ("failed-challenge", True),
    ("disputed", False), ("unresolved", False)])
def test_only_strikes_forfeit(tmp_path: Path, status: str, struck: bool) -> None:
    client, _, ledger = make(tmp_path)
    run_llm_job(client, "wk-a", "The Salty Hoe")
    bad = run_llm_job(client, "wk-a", "The Lazy Oar")
    set_status(ledger, bad, status)  # held either way; only a strike costs the rest
    a = by_worker(rewards.build_report(ledger, 0, T0 + 1, V2))["wk-a"]
    assert a["points"]["held"] == 1 and a["points"]["unverified"] == 1
    assert a["struck"] is struck
    assert a["payable_points"] == (0 if struck else 1)


@pytest.mark.usefixtures("pinned")
def test_strike_only_counts_in_its_own_period(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    run_llm_job(client, "wk-a", "The Salty Hoe")   # period 1, unverified
    clock.t += 100
    bad = run_llm_job(client, "wk-a", "The Lazy Oar")  # period 2
    run_llm_job(client, "wk-a", "The Third Boat")      # period 2, unverified
    set_status(ledger, bad, "outvoted")
    first = by_worker(rewards.build_report(ledger, 0, T0 + 50, V2))["wk-a"]
    second = by_worker(rewards.build_report(ledger, T0 + 50, T0 + 200, V2))["wk-a"]
    assert first["struck"] is False and first["payable_points"] == 1
    assert second["struck"] is True and second["forfeited_points"] == 1
    assert second["payable_points"] == 0


@pytest.mark.usefixtures("pinned")
def test_pending_and_confirmed_are_not_forfeited(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, audit_rate=1.0)
    sha_job(client, "wk-a", points=4)                  # confirmed
    pending = run_llm_job(client, "wk-a", "The Salty Hoe")  # pending re-run
    bad = run_llm_job(client, "wk-a", "The Lazy Oar")
    set_status(ledger, bad, "failed-challenge")
    a = by_worker(rewards.build_report(ledger, 0, T0 + 1, V2))["wk-a"]
    assert ledger.submission(pending)["audit_status"] == "pending"
    assert a["points"] == {"confirmed": 4, "unverified": 0, "pending": 1, "held": 1}
    assert a["struck"] is True and a["forfeited_points"] == 0 and a["payable_points"] == 4


@pytest.mark.usefixtures("pinned")
def test_editing_a_forfeit_is_caught_and_v2_reports_sign(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    run_llm_job(client, "wk-a", "The Salty Hoe")
    fail_hidden_test(client, "wk-a")
    rep = rewards.build_report(ledger, 0, T0 + 1, V2)
    edited = json.loads(json.dumps(rep))
    edited["workers"][0]["forfeited_points"] = 0
    edited["workers"][0]["payable_points"] = 1
    edited["report_sha256"] = rewards.report_hash(edited)  # a careful forger re-hashes
    assert "ledger gives a different report for this period" in rewards.verify_report(
        edited, ledger)
    # Relabelling a v2 report as v1 changes the rebuild too.
    relabelled = json.loads(json.dumps(rep))
    relabelled["scoring"] = rewards.SCORING_RULES[V1]
    relabelled["report_sha256"] = rewards.report_hash(relabelled)
    assert rewards.verify_report(relabelled, ledger) != []
    signer = ManifestSigner.load_or_create(tmp_path / "k.key")
    signed = rewards.sign_report(rep, signer, now=T0 + 10)
    assert rewards.check_signature(signed, signer.public_key_hex) == []
    assert rewards.signed_fields(signed)["scoring_version"] == V2


def test_default_is_v1_and_unknown_rules_are_refused(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(database_path=tmp_path / "x.sqlite3").reward_scoring == V1
    assert rewards.CURRENT_SCORING == V1
    monkeypatch.delenv("REWARD_SCORING", raising=False)
    assert reward_scoring_env() == V1
    monkeypatch.setenv("REWARD_SCORING", " Sim-Points-V2-Forfeit ")
    assert reward_scoring_env() == V2
    monkeypatch.setenv("REWARD_SCORING", "sim-points-v3")
    with pytest.raises(ValueError, match="REWARD_SCORING"):
        reward_scoring_env()
    with pytest.raises(ValueError, match="scoring"):
        make(tmp_path / "bad", scoring="sim-points-v3")


@pytest.mark.usefixtures("pinned")
def test_api_and_health_follow_the_setting(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path / "v1", scoring=V1)
    assert report(client)["scoring"]["version"] == V1
    assert client.get("/v1/operator/health", headers=OP).json()["reward_scoring"] == V1
    client, _, _ = make(tmp_path / "v2", scoring=V2)
    assert report(client)["scoring"]["version"] == V2
    assert client.get("/v1/operator/health", headers=OP).json()["reward_scoring"] == V2


@pytest.mark.usefixtures("pinned")
def test_cli_scoring_flag_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  capsys) -> None:
    client, _, ledger = make(tmp_path, scoring=V1)
    run_llm_job(client, "wk-a", "The Salty Hoe")
    fail_hidden_test(client, "wk-a")
    monkeypatch.delenv("REWARD_SCORING", raising=False)
    out = tmp_path / "r.json"
    base = ["report", "--db", str(ledger.path), "--until", str(T0 + 100), "--out", str(out)]
    assert rewards.main(base) == 0
    assert json.loads(out.read_text())["scoring"]["version"] == V1
    assert rewards.main(base + ["--scoring", V2]) == 0
    data = json.loads(out.read_text())
    assert data["scoring"]["version"] == V2 and data["totals"]["forfeited_points"] == 1
    assert rewards.main(["verify", str(out), "--db", str(ledger.path)]) == 0
    monkeypatch.setenv("REWARD_SCORING", V2)
    assert rewards.main(base) == 0
    assert json.loads(out.read_text())["scoring"]["version"] == V2
    monkeypatch.setenv("REWARD_SCORING", "nope")
    assert rewards.main(base) == 2
    assert "REWARD_SCORING" in capsys.readouterr().err


@pytest.mark.usefixtures("pinned")
def test_v2_report_rebuilds_from_a_shared_ledger_copy(tmp_path: Path) -> None:
    from app.ledger import Ledger
    from app.ledger_copy import export_copy

    client, clock, ledger = make(tmp_path)
    sha_job(client, "wk-a", points=3)
    run_llm_job(client, "wk-a", "The Salty Hoe")
    run_llm_job(client, "wk-c", "The Honest Hull")
    fail_hidden_test(client, "wk-a")
    until = T0 + 10
    clock.t = until + 5
    rep = rewards.build_report(ledger, 0, until, V2)
    assert by_worker(rep)["wk-a"]["forfeited_points"] == 1
    copy = tmp_path / "copy.sqlite3"
    export_copy(ledger.path, copy, until, int(clock.t))
    # Outsiders get the same forfeits from the redacted copy as the coordinator did.
    assert rewards.verify_report(rep, Ledger(copy, read_only=True)) == []
