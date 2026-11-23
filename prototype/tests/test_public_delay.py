"""Threat model #14 (`public-delay-v1`): the public ledger shows settlement with a delay.

Before this, `GET /v1/ledger` updated `points_by_status` the instant a result was accepted.
Normal AI work lands in pending/unverified, but a hidden known-answer test lands straight
in confirmed (passed) or held (failed). So a rig could read the public totals just before
and just after submitting and know, right away, whether that job was a hidden test and
whether it was caught. With the delay, every result counts as `pending` (and its
`confidence` is withheld) for PUBLIC_SETTLE_DELAY_SECONDS after it was accepted, whatever
kind of job it was. The operator still sees the live view with the operator token.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config
from app.config import Settings
from app.main import create_app
from tests.auth_helpers import OP, FirstPickRng, WorkerClient, enroll, flat_audit_rate
from tests.test_audits import (MODEL_ID, T0, Clock, claim, llm_payload, pinned, run_llm_job,  # noqa: F401
                               sha, submit)

DELAY = Settings(database_path=Path("x")).public_settle_delay_seconds
EMPTY = {"confirmed": 0, "pending": 0, "unverified": 0, "held": 0}


def make(tmp_path: Path, **overrides):
    clock = Clock()
    fields = dict(database_path=tmp_path / "db.sqlite3", lease_seconds=30, max_attempts=2,
                  audit_rate=1.0, **flat_audit_rate(1.0), operator_token=OP["X-Operator-Token"])
    app = create_app(Settings(**(fields | overrides)), clock=clock, rng=FirstPickRng(1))
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b", "wk-c"):
        enroll(client, w)
    return client, clock, app.state.ledger


def hidden_test(client: WorkerClient, worker: str, answer: str) -> str:
    c = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload("Name a ship."),
        "expected_result": "The Flagship"}).json()
    assert claim(client, worker).json()["job_id"] == c["job_id"]
    assert submit(client, c["job_id"], worker, answer).status_code == 200
    return c["job_id"]


def public(client: WorkerClient) -> dict:
    r = client.get("/v1/ledger")
    assert r.status_code == 200
    return r.json()


def test_delay_is_on_by_default() -> None:
    assert DELAY == 3600


@pytest.mark.parametrize("kind", ["normal", "passed", "failed"])
def test_public_totals_look_the_same_for_work_and_hidden_tests(pinned, tmp_path: Path,
                                                                kind: str) -> None:
    client, _, ledger = make(tmp_path)
    if kind == "normal":
        job_id = run_llm_job(client, "wk-a", "The Salty Hoe")
    else:
        job_id = hidden_test(client, "wk-a", "The Flagship" if kind == "passed" else "Made up")
    body = public(client)
    # Before the fix: normal -> pending 1, passed -> confirmed 1, failed -> held 1.
    assert body["points_by_status"] == EMPTY | {"pending": 1}
    entry = body["entries"][0]
    assert entry["job_id"] == job_id and entry["confidence"] is None
    assert entry["settlement_visible"] is False
    assert body["public_settle_delay_seconds"] == DELAY
    # The operator's live view still tells them apart.
    live = client.get("/v1/ledger", headers=OP).json()["points_by_status"]
    assert live == EMPTY | {{"normal": "pending", "passed": "confirmed",
                             "failed": "held"}[kind]: 1}
    assert ledger.check_consistency() == []


def test_settlement_shows_once_the_delay_has_passed(pinned, tmp_path: Path) -> None:
    client, clock, _ = make(tmp_path)
    passed = hidden_test(client, "wk-a", "The Flagship")
    clock.t += DELAY - 1
    assert public(client)["points_by_status"] == EMPTY | {"pending": 1}
    clock.t += 1
    body = public(client)
    assert body["points_by_status"] == EMPTY | {"confirmed": 1}
    entry = body["entries"][0]
    assert entry["job_id"] == passed and entry["settlement_visible"] is True
    assert entry["confidence"] == "schema-only"


def test_an_audit_match_does_not_show_early_as_replicated(pinned, tmp_path: Path) -> None:
    """A confirmed re-run flips `confidence` to "replicated". Shown live, that would tell
    the auditor its job was a re-check of someone else's."""
    client, clock, ledger = make(tmp_path)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    audit = claim(client, "wk-b").json()["job_id"]
    assert submit(client, audit, "wk-b", "The Salty Hoe").status_code == 200
    assert ledger.submission(audit)["confidence"] == "replicated"
    body = public(client)
    assert {e["confidence"] for e in body["entries"]} == {None}
    assert body["points_by_status"] == EMPTY | {"pending": 2}
    assert "replicated" not in json.dumps(body)
    clock.t += DELAY
    assert {e["confidence"] for e in public(client)["entries"]} == {"replicated"}


def test_totals_and_job_counts_are_not_delayed(pinned, tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    run_llm_job(client, "wk-a", "The Salty Hoe")
    body = public(client)
    assert body["total_simulated_points"] == 1
    assert body["jobs"]["accepted"] == 1


def test_operator_view_needs_the_right_token(pinned, tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    hidden_test(client, "wk-a", "Made up")
    r = client.get("/v1/ledger", headers={"X-Operator-Token": "wrong-token-xxxxxxxxxxxxxxxxxx"})
    assert r.status_code == 403
    live = client.get("/v1/ledger", headers=OP).json()
    assert live["public_settle_delay_seconds"] == 0 and live["view"] == "operator-live"
    assert public(client)["view"] == "public-delayed"


def test_zero_delay_keeps_the_old_live_view_and_warns(pinned, tmp_path: Path) -> None:
    client, _, _ = make(tmp_path, public_settle_delay_seconds=0)
    hidden_test(client, "wk-a", "The Flagship")
    body = public(client)
    assert body["points_by_status"] == EMPTY | {"confirmed": 1}
    assert body["entries"][0]["settlement_visible"] is True
    warnings = client.get("/v1/operator/health", headers=OP).json()["warnings"]
    assert any("PUBLIC_SETTLE_DELAY_SECONDS" in w for w in warnings)


def test_default_delay_gives_no_health_warning(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    health = client.get("/v1/operator/health", headers=OP).json()
    assert not any("PUBLIC_SETTLE_DELAY_SECONDS" in w for w in health["warnings"])
    assert health["public_settle_delay_seconds"] == DELAY


def test_delay_setting_from_environment(monkeypatch: pytest.MonkeyPatch,
                                        tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("PUBLIC_SETTLE_DELAY_SECONDS", "7200")
    assert config.get_settings().public_settle_delay_seconds == 7200
    for bad in ("-1", "soon", str(31 * 86400)):
        monkeypatch.setenv("PUBLIC_SETTLE_DELAY_SECONDS", bad)
        with pytest.raises(ValueError, match="PUBLIC_SETTLE_DELAY_SECONDS"):
            config.get_settings()
