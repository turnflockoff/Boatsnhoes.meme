"""Threat model #4 and #6: who gets picked to audit or break a tie (`auditor-pick-v1`).

With `AUDITOR_PICK=uniform` (the default) every eligible rig has the same chance, including
a rig that was just caught (`suspect`). With `AUDITOR_PICK=trusted-first` the coordinator
picks at random among the most trusted eligible rigs: `established` first, then `new`, and
a `suspect` rig only when nobody else is eligible.

Tiers come from `audit-tier-v1` (app/audit_policy.py). In these tests, with
`audit_new_until=1`, a rig becomes `established` by passing one hidden test and `suspect`
by failing one.
"""
from __future__ import annotations

import random
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import audit_policy
from app.config import Settings, get_settings
from app.ledger_copy import export_copy
from app.main import create_app
from tests.auth_helpers import WorkerClient, enroll
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

EXPECTED = "The Flagship"
WORKERS = ("wk-a", "wk-b", "wk-c", "wk-d")


# -- the rule itself ----------------------------------------------------------------------
def test_pick_pool_prefers_the_most_trusted_tier() -> None:
    pool = audit_policy.auditor_pool
    tiers = {"a": "new", "b": "established", "c": "suspect", "d": "established"}
    assert pool(["a", "b", "c", "d"], tiers.__getitem__) == (["b", "d"], "established")
    assert pool(["a", "c"], tiers.__getitem__) == (["a"], "new")
    assert pool(["c"], tiers.__getitem__) == (["c"], "suspect")
    assert pool([], tiers.__getitem__) == ([], None)
    # Order is kept, so a seeded rng picks the same rig every run.
    assert pool(["d", "b"], tiers.__getitem__) == (["d", "b"], "established")


def test_setting_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    assert get_settings().auditor_pick == "uniform"
    monkeypatch.setenv("AUDITOR_PICK", "trusted-first")
    assert get_settings().auditor_pick == "trusted-first"
    monkeypatch.setenv("AUDITOR_PICK", " Trusted-First ")
    assert get_settings().auditor_pick == "trusted-first"
    for bad in ("trusted", "random", "established-only"):
        monkeypatch.setenv("AUDITOR_PICK", bad)
        with pytest.raises(ValueError, match="AUDITOR_PICK"):
            get_settings()


# -- through the API ------------------------------------------------------------------------
def make(tmp_path: Path, pick: str, seed: int = 7):
    """wk-a does normal work. wk-b stays `new`, wk-c becomes `established` (passed a hidden
    test) and wk-d becomes `suspect` (failed one). Every result is re-checked."""
    clock = Clock()
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, operator_token=OP["X-Operator-Token"],
                        audit_rate=1.0, audit_rate_new=1.0, audit_rate_suspect=1.0,
                        audit_new_until=1, rate_limit_per_minute=0, auditor_pick=pick)
    app = create_app(settings, clock=clock, rng=random.Random(seed))
    client = WorkerClient(app)
    for w in WORKERS:
        enroll(client, w)
    hidden_test(client, "wk-c", EXPECTED)
    hidden_test(client, "wk-d", "Made up answer")
    tiers = {w: trust(client, w)["audit_tier"] for w in WORKERS}
    assert tiers == {"wk-a": "new", "wk-b": "new", "wk-c": "established", "wk-d": "suspect"}
    return client, clock, app.state.ledger


def hidden_test(client: WorkerClient, worker: str, answer: str) -> None:
    job_id = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload("Name a ship."),
        "expected_result": EXPECTED}).json()["job_id"]
    assert claim(client, worker).json()["job_id"] == job_id
    assert submit(client, job_id, worker, answer).status_code == 200


def trust(client: WorkerClient, worker: str) -> dict:
    r = client.get(f"/v1/workers/{worker}/trust", headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


def assigned(ledger, original: str) -> str | None:
    return next(a for a in ledger.audits() if a["audit_of"] == original)["assigned_worker"]


def picks(client: WorkerClient, ledger, rounds: int) -> set[str]:
    """Who gets wk-a's audits over `rounds` results. Each audit is picked when queued."""
    return {assigned(ledger, run_llm_job(client, "wk-a", f"boat {i}")) for i in range(rounds)}


@pytest.mark.usefixtures("pinned")
def test_uniform_pick_can_hand_audits_to_a_caught_rig(tmp_path: Path) -> None:
    """Today's behaviour, kept as the default: a rig with a strike still audits others."""
    client, _, ledger = make(tmp_path, "uniform")
    assert picks(client, ledger, 30) == {"wk-b", "wk-c", "wk-d"}
    assert client.get("/v1/audits", headers=OP).json()["audit_policy"]["auditor_pick"] == {
        "version": "auditor-pick-v1", "rule": "uniform",
        "order": ["established", "new", "suspect"]}


@pytest.mark.usefixtures("pinned")
def test_trusted_first_sends_every_audit_to_an_established_rig(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, "trusted-first")
    assert picks(client, ledger, 30) == {"wk-c"}
    assert client.get("/v1/audits", headers=OP).json()["audit_policy"]["auditor_pick"][
        "rule"] == "trusted-first"
    assert ledger.check_consistency() == []


@pytest.mark.usefixtures("pinned")
def test_without_an_established_rig_a_new_one_goes_before_a_suspect(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, "trusted-first")
    assert client.post("/v1/workers/wk-c/revoke", headers=OP).status_code == 200
    assert picks(client, ledger, 20) == {"wk-b"}


@pytest.mark.usefixtures("pinned")
def test_a_suspect_rig_audits_only_when_nobody_else_is_eligible(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, "trusted-first")
    for w in ("wk-b", "wk-c"):
        assert client.post(f"/v1/workers/{w}/revoke", headers=OP).status_code == 200
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert assigned(ledger, original) == "wk-d"  # last resort: better than no check at all
    audit_id = claim(client, "wk-d").json()["job_id"]
    assert submit(client, audit_id, "wk-d", "The Salty Hoe").status_code == 200
    assert audit_status(ledger, original) == "confirmed"


@pytest.mark.usefixtures("pinned")
def test_silent_established_pick_moves_down_a_tier_not_to_a_suspect(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, "trusted-first")
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert assigned(ledger, original) == "wk-c"
    clock.t += 121  # wk-c never picks it up; the previous pick is avoided when possible
    assert claim(client, "wk-b").status_code == 200
    assert assigned(ledger, original) == "wk-b"


@pytest.mark.usefixtures("pinned")
def test_tiebreaker_is_picked_trusted_first_too(tmp_path: Path) -> None:
    """wk-c (established) disagrees with wk-a. The tie-breaker can't be either of them, so
    it goes to wk-b (new) and never to wk-d (suspect)."""
    client, _, ledger = make(tmp_path, "trusted-first")
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    audit_id = claim(client, "wk-c").json()["job_id"]
    assert submit(client, audit_id, "wk-c", "The Rusty Oar").status_code == 200
    assert audit_status(ledger, original) == "disputed"
    tiebreak = next(a for a in ledger.audits()
                    if a["audit_of"] == original and a["job_kind"] == "tiebreak")
    assert tiebreak["assigned_worker"] == "wk-b"


@pytest.mark.usefixtures("pinned")
def test_pick_detail_stays_private_and_out_of_ledger_copies(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, "trusted-first")
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    with closing(sqlite3.connect(ledger.path)) as db:  # the private audit trail
        detail = db.execute("SELECT detail FROM events WHERE event='auditor_assigned' "
                            "ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert detail == "candidates=1 pool=established"
    public = client.get(f"/v1/jobs/{original}").json()["events"]
    assert all(e["event"] != "auditor_assigned" for e in public)
    assert all("pool=" not in str(e.get("detail") or "") for e in public)
    audit_id = claim(client, "wk-c").json()["job_id"]
    assert submit(client, audit_id, "wk-c", "The Salty Hoe").status_code == 200
    until = int(clock.t) + 1
    clock.t += 10
    out = tmp_path / "copy.sqlite3"
    export_copy(ledger.path, out, until, int(clock.t))
    assert b"pool=" not in out.read_bytes()


@pytest.mark.usefixtures("pinned")
def test_health_warns_when_a_caught_rig_can_still_audit(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path, "uniform")
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["auditor_pick"] == "uniform"
    assert any("AUDITOR_PICK=trusted-first" in w for w in health["warnings"])
    client2, _, _ = make(tmp_path / "second", "trusted-first")
    health2 = client2.get("/v1/operator/health", headers=OP).json()
    assert health2["auditor_pick"] == "trusted-first"
    assert not any("AUDITOR_PICK" in w for w in health2["warnings"])


def test_ledger_refuses_an_unknown_rule_or_trusted_first_without_tiers(tmp_path: Path) -> None:
    from app.ledger import Ledger

    with pytest.raises(ValueError, match="auditor_pick"):
        Ledger(tmp_path / "a.sqlite3", auditor_pick="random")
    with pytest.raises(ValueError, match="tier_of"):
        Ledger(tmp_path / "b.sqlite3", auditor_pick="trusted-first")
    assert Ledger(tmp_path / "c.sqlite3").auditor_pick == "uniform"
