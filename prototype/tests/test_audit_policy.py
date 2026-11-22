"""Threat model #1: new rigs and rigs with a strike are re-checked more often (`audit-tier-v1`)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import audit_policy
from app.config import Settings, get_settings
from app.main import create_app
from tests.auth_helpers import FirstPickRng, WorkerClient, enroll
from tests.test_audits import (
    OP,
    Clock,
    audit_status,
    claim,
    llm_payload,
    pinned,  # noqa: F401  (pytest fixture)
    run_llm_job,
    submit,
)


class FixedDrawRng(FirstPickRng):
    """Every audit-sampling draw returns `draw`, so the rate alone decides: a result is
    re-checked when draw < rate. Auditor picks stay alphabetical (wk-b before wk-c)."""

    def __init__(self, draw: float) -> None:
        super().__init__(1)
        self.draw = draw

    def random(self) -> float:
        return self.draw


def make(tmp_path: Path, draw: float, new_until: int = 1, **overrides: float):
    rates = {"audit_rate": 0.25, "audit_rate_new": 0.5, "audit_rate_suspect": 1.0} | overrides
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, operator_token=OP["X-Operator-Token"],
                        audit_new_until=new_until, **rates)
    app = create_app(settings, clock=Clock(), rng=FixedDrawRng(draw))
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b", "wk-c"):
        enroll(client, w)
    return client, app.state.ledger


def trust(client: WorkerClient, worker: str) -> dict:
    r = client.get(f"/v1/workers/{worker}/trust", headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


def fail_a_challenge(client: WorkerClient, worker: str) -> str:
    job_id = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload("Name a ship."),
        "expected_result": "The Flagship"}).json()["job_id"]
    assert claim(client, worker).json()["job_id"] == job_id
    assert submit(client, job_id, worker, "Made up answer").status_code == 200
    return job_id


# -- the rule itself ----------------------------------------------------------------------
def test_tier_rule() -> None:
    d = audit_policy.decide
    assert d(0, 0, 0.25, 0.5, 1.0, 5) == audit_policy.AuditDecision("new", 0.5, 0, 0)
    assert d(4, 0, 0.25, 0.5, 1.0, 5).tier == "new"
    assert d(5, 0, 0.25, 0.5, 1.0, 5) == audit_policy.AuditDecision("established", 0.25, 5, 0)
    # One strike outweighs any number of passed checks.
    assert d(500, 1, 0.25, 0.5, 1.0, 5) == audit_policy.AuditDecision("suspect", 1.0, 500, 1)
    # A tier never lowers the base rate: AUDIT_RATE=1 still checks everything.
    assert d(0, 0, 1.0, 0.0, 0.0, 5).rate == 1.0
    assert d(0, 3, 0.25, 0.5, 0.0, 5).rate == 0.25
    # AUDIT_NEW_UNTIL=0 switches the "new" tier off.
    assert d(0, 0, 0.25, 0.5, 1.0, 0).tier == "established"


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    s = get_settings()
    assert (s.audit_rate_new, s.audit_rate_suspect, s.audit_new_until) == (0.5, 1.0, 5)
    monkeypatch.setenv("AUDIT_RATE_NEW", "0.9")
    monkeypatch.setenv("AUDIT_NEW_UNTIL", "20")
    assert get_settings().audit_rate_new == 0.9 and get_settings().audit_new_until == 20
    for name, bad in (("AUDIT_RATE_NEW", "1.5"), ("AUDIT_RATE_SUSPECT", "-0.1"),
                      ("AUDIT_RATE_SUSPECT", "lots"), ("AUDIT_NEW_UNTIL", "-1")):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValueError, match=name):
            get_settings()
        monkeypatch.delenv(name)


# -- through the API ------------------------------------------------------------------------
@pytest.mark.usefixtures("pinned")
def test_new_worker_is_checked_more_until_it_has_a_clean_record(tmp_path: Path) -> None:
    # draw 0.4: re-checked at the "new" rate (0.5), not at the base rate (0.25).
    client, ledger = make(tmp_path, draw=0.4, new_until=1)
    assert trust(client, "wk-a")["audit_tier"] == "new"
    first = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert audit_status(ledger, first) == "pending"
    audit_id = claim(client, "wk-b").json()["job_id"]
    assert submit(client, audit_id, "wk-b", "The Salty Hoe").status_code == 200
    assert audit_status(ledger, first) == "confirmed"
    t = trust(client, "wk-a")
    assert (t["audit_tier"], t["audit_rate"], t["checks_passed"], t["strikes"]) == \
        ("established", 0.25, 1, 0)
    assert t["audit_policy"] == "audit-tier-v1"
    # Now established: the same draw is above the base rate, so no re-run.
    second = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert audit_status(ledger, second) == "unaudited"
    assert ledger.check_consistency() == []


@pytest.mark.usefixtures("pinned")
def test_failed_hidden_test_makes_every_result_rechecked(tmp_path: Path) -> None:
    # draw 0.6: above the "new" rate (0.5), below the "suspect" rate (1.0).
    client, ledger = make(tmp_path, draw=0.6, new_until=1)
    before = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert audit_status(ledger, before) == "unaudited"
    fail_a_challenge(client, "wk-a")
    t = trust(client, "wk-a")
    assert (t["audit_tier"], t["audit_rate"], t["strikes"]) == ("suspect", 1.0, 1)
    after = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert audit_status(ledger, after) == "pending"
    # Other rigs are unaffected by wk-a's strike.
    assert trust(client, "wk-c")["audit_tier"] == "new"


@pytest.mark.usefixtures("pinned")
def test_outvoted_result_is_a_strike(tmp_path: Path) -> None:
    client, ledger = make(tmp_path, draw=0.4)
    original = run_llm_job(client, "wk-a", "A fake boat")
    audit_id = claim(client, "wk-b").json()["job_id"]
    submit(client, audit_id, "wk-b", "The Salty Hoe")
    tiebreak_id = claim(client, "wk-c").json()["job_id"]
    submit(client, tiebreak_id, "wk-c", "The Salty Hoe")
    assert audit_status(ledger, original) == "outvoted"
    assert ledger.audit_history("wk-a") == {"checks_passed": 0, "strikes": 1}
    # The two rigs that agreed each passed a check.
    assert ledger.audit_history("wk-b") == {"checks_passed": 1, "strikes": 0}
    assert ledger.audit_history("wk-c") == {"checks_passed": 1, "strikes": 0}
    assert trust(client, "wk-a")["audit_tier"] == "suspect"


@pytest.mark.usefixtures("pinned")
def test_strike_changes_checks_not_points_and_stays_hidden(tmp_path: Path) -> None:
    client, ledger = make(tmp_path, draw=0.6)
    fail_a_challenge(client, "wk-a")
    job = client.post("/v1/jobs", json={"task_type": "llm:generate", "payload": llm_payload()})
    job_id = job.json()["job_id"]
    claim(client, "wk-a")
    r = submit(client, job_id, "wk-a", "The Salty Hoe")
    # Same reply a clean rig gets: no tier, no hint it's being watched.
    assert r.status_code == 200 and r.json()["reward_points"] == 1
    public = json.dumps([r.json(), client.get(f"/v1/jobs/{job_id}").json(),
                         client.get("/v1/ledger").json()])
    for word in ("tier", "suspect", "strike"):
        assert word not in public
    assert all(e["event"] not in ("audit_tier", "strike") for e in ledger.events())
    # The operator is told, by name, on the health page.
    health = client.get("/v1/operator/health", headers=OP).json()
    assert any("wk-a" in w and "strike" in w for w in health["warnings"])


def test_policy_is_listed_for_the_operator(tmp_path: Path) -> None:
    client, ledger = make(tmp_path, draw=0.0, new_until=7, audit_rate_new=0.1)
    policy = client.get("/v1/audits", headers=OP).json()["audit_policy"]
    assert policy["version"] == "audit-tier-v1"
    assert (policy["base_rate"], policy["new_rate"], policy["suspect_rate"]) == (0.25, 0.25, 1.0)
    assert policy["new_until_checks_passed"] == 7
    assert client.get("/v1/audits").status_code == 403
    # Workers with no results and unknown ids both start from zero.
    assert ledger.audit_history()["wk-a"] == {"checks_passed": 0, "strikes": 0}
    assert ledger.audit_history("nobody") == {"checks_passed": 0, "strikes": 0}
