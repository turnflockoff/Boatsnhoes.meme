"""Threat model #6: who may settle a dispute on their own (`tiebreak-trust-v1`).

When an audit disagrees with a result, a third rig (the tie-breaker) runs the job and the
majority of three wins. If the tie-breaker and the rig it sides with are in the same ring,
two colluders outvote an honest rig. With `TIEBREAK_DECIDES=established-only` a majority
only settles the dispute automatically when both rigs on the winning side are
`established` (see app/audit_policy.py). Otherwise all three results stay held and the
case goes to the operator review queue. `always` (the default) keeps today's behaviour.

Tiers come from `audit-tier-v1`. In these tests, with `audit_new_until=1`, a rig becomes
`established` by passing one hidden test. wk-a does the original work, wk-b audits it and
wk-c breaks the tie (FirstPickRng picks alphabetically).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import audit_policy, review
from app.config import Settings, get_settings
from app.ledger import Ledger
from app.ledger_copy import export_copy
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

EXPECTED = "The Flagship"
ORIGINAL = "The Salty Hoe"
OTHER = "A totally different boat"


# -- the rule itself ----------------------------------------------------------------------
def test_rule_settles_only_when_every_deciding_rig_is_established() -> None:
    settles = audit_policy.tiebreak_settles
    assert settles("always", ["new", "suspect"]) is True
    assert settles("established-only", ["established", "established"]) is True
    assert settles("established-only", ["established", "new"]) is False
    assert settles("established-only", ["suspect", "established"]) is False
    assert settles("established-only", []) is False  # nothing known: a person decides
    with pytest.raises(ValueError):
        settles("sometimes", ["established"])


def test_setting_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    assert get_settings().tiebreak_decides == "always"
    monkeypatch.setenv("TIEBREAK_DECIDES", " Established-Only ")
    assert get_settings().tiebreak_decides == "established-only"
    for bad in ("established", "never", "trusted-first"):
        monkeypatch.setenv("TIEBREAK_DECIDES", bad)
        with pytest.raises(ValueError, match="TIEBREAK_DECIDES"):
            get_settings()


def test_ledger_refuses_an_unknown_rule_or_established_only_without_tiers(
        tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tiebreak_decides"):
        Ledger(tmp_path / "a.sqlite3", tiebreak_decides="sometimes")
    with pytest.raises(ValueError, match="tier_of"):
        Ledger(tmp_path / "b.sqlite3", tiebreak_decides="established-only")
    assert Ledger(tmp_path / "c.sqlite3").tiebreak_decides == "always"


# -- through the API ------------------------------------------------------------------------
def make(tmp_path: Path, rule: str, established: tuple[str, ...]):
    clock = Clock()
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, operator_token=OP["X-Operator-Token"],
                        audit_rate=1.0, audit_rate_new=1.0, audit_rate_suspect=1.0,
                        audit_new_until=1, rate_limit_per_minute=0, tiebreak_decides=rule,
                        public_settle_delay_seconds=0)  # see test_public_delay.py
    app = create_app(settings, clock=clock, rng=FirstPickRng(1))
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b", "wk-c"):
        enroll(client, w)
    for w in established:
        hidden_test(client, w)
    tiers = {w: tier(client, w) for w in ("wk-a", "wk-b", "wk-c")}
    assert tiers == {w: "established" if w in established else "new"
                     for w in ("wk-a", "wk-b", "wk-c")}
    return client, clock, app.state.ledger


def hidden_test(client: WorkerClient, worker: str) -> None:
    job_id = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload("Name a ship."),
        "expected_result": EXPECTED}).json()["job_id"]
    assert claim(client, worker).json()["job_id"] == job_id
    assert submit(client, job_id, worker, EXPECTED).status_code == 200


def tier(client: WorkerClient, worker: str) -> str:
    r = client.get(f"/v1/workers/{worker}/trust", headers=OP)
    assert r.status_code == 200, r.text
    return r.json()["audit_tier"]


def dispute(client: WorkerClient, tiebreak_answer: str) -> tuple[str, str, str]:
    """wk-a answers, wk-b disagrees, wk-c breaks the tie with `tiebreak_answer`."""
    original = run_llm_job(client, "wk-a", ORIGINAL)
    audit_id = claim(client, "wk-b").json()["job_id"]
    assert submit(client, audit_id, "wk-b", OTHER).status_code == 200
    tb = claim(client, "wk-c").json()["job_id"]
    assert submit(client, tb, "wk-c", tiebreak_answer).status_code == 200
    return original, audit_id, tb


def statuses(ledger: Ledger, *job_ids: str) -> list[str]:
    return [audit_status(ledger, j) for j in job_ids]


def events(ledger: Ledger, job_id: str) -> list[str]:
    return [e["event"] for e in ledger.events(job_id)]


@pytest.mark.usefixtures("pinned")
def test_default_lets_a_new_tiebreaker_decide(tmp_path: Path) -> None:
    """Today's behaviour, kept as the default."""
    client, _, ledger = make(tmp_path, "always", established=())
    original, audit_id, tb = dispute(client, ORIGINAL)
    assert statuses(ledger, original, audit_id, tb) == ["confirmed", "outvoted", "confirmed"]
    assert client.get("/v1/audits", headers=OP).json()["audit_policy"]["tiebreak_decides"] == {
        "version": "tiebreak-trust-v1", "rule": "always"}


@pytest.mark.usefixtures("pinned")
def test_established_majority_still_settles(tmp_path: Path) -> None:
    """wk-a and wk-c are established and agree: wk-b (new) is outvoted as before."""
    client, _, ledger = make(tmp_path, "established-only", established=("wk-a", "wk-c"))
    original, audit_id, tb = dispute(client, ORIGINAL)
    assert statuses(ledger, original, audit_id, tb) == ["confirmed", "outvoted", "confirmed"]
    assert "tiebreak_majority" in events(ledger, original)
    assert "tiebreak_held_for_review" not in events(ledger, original)


@pytest.mark.usefixtures("pinned")
def test_new_tiebreaker_cannot_settle_on_its_own(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, "established-only", established=("wk-a", "wk-b"))
    original, audit_id, tb = dispute(client, ORIGINAL)
    assert statuses(ledger, original, audit_id, tb) == ["disputed"] * 3
    for job_id in (original, audit_id, tb):
        assert "tiebreak_held_for_review" in events(ledger, job_id)
    assert "tiebreak_majority" not in events(ledger, original)
    # Nobody gets a strike: a person hasn't decided yet.
    assert ledger.audit_history("wk-b")["strikes"] == 0
    assert client.get("/v1/workers/wk-b/trust", headers=OP).json()["audits_outvoted"] == 0
    body = client.get("/v1/ledger").json()
    assert body["points_by_status"]["held"] == 3
    assert body["consistency_problems"] == []


@pytest.mark.usefixtures("pinned")
def test_new_winner_cannot_settle_even_with_an_established_tiebreaker(tmp_path: Path) -> None:
    """wk-c (established) sides with wk-a (new): two rigs agree, but one of them hasn't
    earned trust yet, so a person decides."""
    client, _, ledger = make(tmp_path, "established-only", established=("wk-b", "wk-c"))
    original, audit_id, tb = dispute(client, ORIGINAL)
    assert statuses(ledger, original, audit_id, tb) == ["disputed"] * 3
    # The other way round both deciding rigs are established, so it settles.
    client2, _, ledger2 = make(tmp_path / "second", "established-only",
                               established=("wk-b", "wk-c"))
    original2, audit2, tb2 = dispute(client2, OTHER)
    assert statuses(ledger2, original2, audit2, tb2) == ["outvoted", "confirmed", "confirmed"]
    assert ledger2.audit_history("wk-a")["strikes"] == 1


@pytest.mark.usefixtures("pinned")
def test_three_way_disagreement_is_unresolved_under_either_rule(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, "established-only", established=())
    original, audit_id, tb = dispute(client, "A third boat entirely")
    assert statuses(ledger, original, audit_id, tb) == ["unresolved"] * 3
    assert "tiebreak_held_for_review" not in events(ledger, original)


@pytest.mark.usefixtures("pinned")
def test_held_case_goes_to_the_review_queue(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, "established-only", established=("wk-a", "wk-b"))
    original, _, _ = dispute(client, ORIGINAL)
    queue = review.review_queue(ledger, int(clock()))
    case = next(c for c in queue["cases"] if c["case_id"] == original)
    assert case["stage"] == "needs_review" and case["needs_person"]
    assert "TIEBREAK_DECIDES=established-only" in case["why"]
    agree = {a["role"]: a["agrees_with"] for a in case["answers"]}
    assert agree["tie-breaker"] == ["original"]
    assert case["points_held"] == 3
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["tiebreak_decides"] == "established-only"
    assert any("need a person" in w for w in health["warnings"])


@pytest.mark.usefixtures("pinned")
def test_hold_stays_private_and_out_of_ledger_copies(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, "established-only", established=("wk-a", "wk-b"))
    original, audit_id, tb = dispute(client, ORIGINAL)
    for job_id in (original, audit_id, tb):
        public = json.dumps(client.get(f"/v1/jobs/{job_id}").json())
        assert "held_for_review" not in public and "established" not in public
    with closing(sqlite3.connect(ledger.path)) as db:  # the private audit trail
        detail = db.execute("SELECT detail FROM events WHERE event='tiebreak_held_for_review' "
                            "AND job_id=?", (original,)).fetchone()[0]
    assert "sided with original" in detail and "tie-breaker new" in detail
    # A held dispute doesn't block sharing the period, and the copy drops the detail.
    until = int(clock()) + 1
    clock.t += 10
    out = tmp_path / "copy.sqlite3"
    export_copy(ledger.path, out, until, int(clock()))
    assert b"tie-breaker new" not in out.read_bytes()
    assert Ledger(out, read_only=True).check_consistency() == []


@pytest.mark.usefixtures("pinned")
def test_tier_is_judged_before_the_vote_counts(tmp_path: Path) -> None:
    """The vote itself would give wk-c its first passed check (and make it established);
    the rule looks at each rig's record from before this dispute."""
    client, _, ledger = make(tmp_path, "established-only", established=("wk-a",))
    original, audit_id, tb = dispute(client, ORIGINAL)
    assert statuses(ledger, original, audit_id, tb) == ["disputed"] * 3
    assert tier(client, "wk-c") == "new"
