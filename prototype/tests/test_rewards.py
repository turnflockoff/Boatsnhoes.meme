"""Phase 5: simulated reward reports with a versioned, deterministic scoring rule."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import rewards
from app.security import sha256_text
from tests.test_audits import (
    OP,
    T0,
    claim,
    llm_payload,
    make,
    pinned,  # noqa: F401  (pytest fixture)
    run_llm_job,
    submit,
)


def sha_job(client: TestClient, worker: str, payload: str = "hello boats",
            points: int = 3) -> str:
    assert client.post("/v1/jobs", json={"payload": payload, "reward_points": points}
                       ).status_code == 200
    job_id = claim(client, worker).json()["job_id"]
    assert submit(client, job_id, worker, sha256_text(payload)).status_code == 200
    return job_id


def report(client: TestClient, **params) -> dict:
    r = client.get("/v1/rewards/report", params=params, headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


def by_worker(rep: dict) -> dict[str, dict]:
    return {w["worker_id"]: w for w in rep["workers"]}


@pytest.mark.usefixtures("pinned")
def test_buckets_and_payable_points(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path, audit_rate=1.0)
    sha_job(client, "wk-a", points=3)                      # exact -> confirmed
    original = run_llm_job(client, "wk-a", "The Salty Hoe")  # schema-only -> pending
    rep = report(client)
    a = by_worker(rep)["wk-a"]
    assert a["points"] == {"confirmed": 3, "unverified": 0, "pending": 1, "held": 0}
    assert a["payable_points"] == 3
    # The hidden re-run disagrees: both sides are held, nothing extra becomes payable.
    audit = claim(client, "wk-b").json()["job_id"]
    submit(client, audit, "wk-b", "Some other boat")
    rep = report(client)
    w = by_worker(rep)
    assert w["wk-a"]["points"]["held"] == 1 and w["wk-a"]["payable_points"] == 3
    assert w["wk-b"]["points"]["held"] == 1 and w["wk-b"]["payable_points"] == 0
    assert rep["totals"]["payable_points"] == 3 and rep["totals"]["points"]["held"] == 2
    assert rep["checks"] == {"consistency_problems": [], "worker_sums_match_totals": True,
                             "ok": True}
    assert rep["scoring"]["version"] == "sim-points-v1"
    assert "not money" in rep["notice"]
    assert original in {e["job_id"] for e in rep["entries"]}


@pytest.mark.usefixtures("pinned")
def test_unsampled_schema_only_work_is_unverified_but_payable(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path, audit_rate=0.0)
    run_llm_job(client, "wk-c", "The Salty Hoe")
    c = by_worker(report(client))["wk-c"]
    assert c["points"]["unverified"] == 1 and c["payable_points"] == 1


@pytest.mark.usefixtures("pinned")
def test_failed_hidden_challenge_is_held_and_not_named(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    r = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload("Secret test"),
        "expected_result": "Known answer"})
    assert r.status_code == 200
    job_id = claim(client, "wk-a").json()["job_id"]
    submit(client, job_id, "wk-a", "Made-up answer")
    rep = report(client)
    assert by_worker(rep)["wk-a"]["points"]["held"] == 1
    text = json.dumps(rep).lower()
    # The report never names job kinds, results, payloads, expected answers or secrets.
    for leak in ("challenge", "audit_of", "job_kind", "known answer", "made-up answer",
                 "secret test", "private key", "expected_result"):
        assert leak not in text, leak


def test_same_ledger_same_period_gives_identical_report(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    sha_job(client, "wk-a")
    clock.t += 10
    sha_job(client, "wk-b", payload="second")
    first = report(client, until=T0 + 100)
    clock.t += 500  # time passing alone changes nothing for a fixed period
    second = report(client, until=T0 + 100)
    assert first == second
    assert first["report_sha256"] == rewards.report_hash(first)
    assert rewards.verify_report(first, ledger) == []


def test_period_filter(tmp_path: Path) -> None:
    client, clock, _ = make(tmp_path)
    early = sha_job(client, "wk-a", payload="early")
    clock.t += 100
    late = sha_job(client, "wk-a", payload="late")
    only_early = report(client, since=0, until=T0 + 50)
    only_late = report(client, since=T0 + 50, until=T0 + 1000)
    assert [e["job_id"] for e in only_early["entries"]] == [early]
    assert [e["job_id"] for e in only_late["entries"]] == [late]
    assert client.get("/v1/rewards/report", params={"since": 10, "until": 10},
                      headers=OP).status_code == 422


def test_worker_activity_counts(tmp_path: Path) -> None:
    client, clock, _ = make(tmp_path)
    client.post("/v1/jobs", json={"payload": "x"})
    job_id = claim(client, "wk-a").json()["job_id"]
    assert submit(client, job_id, "wk-a", "0" * 64).status_code == 422  # wrong answer
    claim(client, "wk-b")
    clock.t += 31  # wk-b goes silent; lease times out on the next sweep
    ledger = client.app.state.ledger
    ledger.sweep(int(clock.t))
    w = by_worker(report(client))
    assert w["wk-a"]["activity"]["leases"] == 1 and w["wk-a"]["activity"]["rejected"] == 1
    assert w["wk-b"]["activity"]["lease_timeouts"] == 1
    assert w["wk-a"]["accepted_results"] == 0 and w["wk-a"]["payable_points"] == 0


def test_report_is_operator_only(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    assert client.get("/v1/rewards/report").status_code == 403
    assert client.get("/v1/rewards/report", headers={"X-Operator-Token": "x" * 30}
                      ).status_code == 403
    off, _, _ = make(tmp_path / "off", operator_token=None)
    assert off.get("/v1/rewards/report", headers=OP).status_code == 403


def test_verify_catches_edited_report_and_changed_ledger(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    job_id = sha_job(client, "wk-a", points=5)
    rep = report(client, until=T0 + 100)
    edited = json.loads(json.dumps(rep))
    edited["workers"][0]["payable_points"] = 500
    assert any("was edited" in p for p in rewards.verify_report(edited, ledger))
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE submissions SET worker_id='wk-b' WHERE job_id=?", (job_id,))
    problems = rewards.verify_report(rep, ledger)
    assert "ledger gives a different report for this period" in problems
    assert f"entry differs: {job_id}" in problems


def test_unknown_scoring_version_is_refused(tmp_path: Path) -> None:
    _, _, ledger = make(tmp_path)
    try:
        rewards.build_report(ledger, 0, T0, scoring="sim-points-v999")
    except rewards.ReportError:
        pass
    else:
        raise AssertionError("unknown scoring version accepted")


def test_cli_report_and_verify(tmp_path: Path, capsys) -> None:
    client, _, ledger = make(tmp_path)
    sha_job(client, "wk-a")
    out = tmp_path / "report.json"
    assert rewards.main(["report", "--db", str(ledger.path), "--until", str(T0 + 100),
                         "--out", str(out)]) == 0
    assert rewards.main(["verify", str(out), "--db", str(ledger.path)]) == 0
    assert "OK: report matches the ledger" in capsys.readouterr().out
    data = json.loads(out.read_text())
    data["totals"]["payable_points"] += 1
    out.write_text(json.dumps(data))
    assert rewards.main(["verify", str(out), "--db", str(ledger.path)]) == 1
    assert rewards.main(["report", "--db", str(tmp_path / "missing.sqlite3")]) == 2
