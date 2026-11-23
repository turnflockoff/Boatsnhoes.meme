"""Threat model #14, last open hint (`public-counts-v1`): public job counts are batched.

`GET /health` and `GET /v1/ledger` showed live job counts (`queued`, `leased`, ...). When a
rig submits AI work that gets picked for a re-check, the coordinator queues that re-check
straight away, so `queued` went up by one the instant the rig submitted. A rig (or a ring
member watching the counts) could read them just before and just after each submit and
learn which of its results were being re-checked, and from that roughly how often it is
checked (its trust tier). Leaving re-checks out of the counts doesn't work: the auditor
would then see the counts *not* move when it claimed one, which is its own tell.

So the public counts are now taken once per clock-aligned window of
PUBLIC_COUNTS_INTERVAL_SECONDS (default 900) and repeated until the next window starts,
for everyone and every job kind. A rig sees at most one change per window, the sum of
everything that happened in it. The operator token still sees live counts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app
from tests.auth_helpers import OP, FirstPickRng, WorkerClient, enroll, flat_audit_rate
from tests.test_audits import Clock, claim, llm_payload, pinned, submit  # noqa: F401

INTERVAL = Settings(database_path=Path("x")).public_counts_interval_seconds


def make(tmp_path: Path, audit_rate: float = 1.0, **overrides):
    clock = Clock()
    # Start one second into a window so the tests spell out where the boundary is.
    clock.t = (int(clock.t) // INTERVAL) * INTERVAL + 1
    fields = dict(database_path=tmp_path / "db.sqlite3", lease_seconds=30, max_attempts=2,
                  audit_rate=audit_rate, **flat_audit_rate(audit_rate),
                  operator_token=OP["X-Operator-Token"])
    app = create_app(Settings(**(fields | overrides)), clock=clock, rng=FirstPickRng(1))
    client = WorkerClient(app)
    for w in ("wk-a", "wk-b"):
        enroll(client, w)
    return client, clock, app.state.ledger


def public_jobs(client: WorkerClient) -> dict:
    ledger_view = client.get("/v1/ledger").json()["jobs"]
    health_view = client.get("/health").json()["jobs"]
    assert ledger_view == health_view  # one snapshot, shared by both public views
    return ledger_view


def submit_ai_work(client: WorkerClient) -> str:
    """wk-a takes a job, the public counts are read, then wk-a submits."""
    job = client.post("/v1/jobs", json={"task_type": "llm:generate",
                                        "payload": llm_payload()}).json()
    assert claim(client, "wk-a").json()["job_id"] == job["job_id"]
    return job["job_id"]


def test_counts_are_batched_by_default() -> None:
    assert INTERVAL == 900


@pytest.mark.parametrize("audit_rate", [1.0, 0.0])
def test_a_submit_does_not_show_whether_a_recheck_was_queued(pinned, tmp_path: Path,
                                                             audit_rate: float) -> None:
    """Before the fix, right after the submit: re-checked -> queued 1, not -> queued 0."""
    client, _, ledger = make(tmp_path, audit_rate=audit_rate)
    job_id = submit_ai_work(client)
    before = public_jobs(client)
    assert before == {"leased": 1}
    assert submit(client, job_id, "wk-a", "The Salty Hoe").status_code == 200
    assert public_jobs(client) == before  # same window: same counts, whichever case
    # The operator still sees the truth live.
    live = client.get("/v1/ledger", headers=OP).json()["jobs"]
    assert live == ledger.status_counts()
    assert live == ({"accepted": 1, "queued": 1} if audit_rate else {"accepted": 1})


def test_counts_refresh_when_the_next_window_starts(pinned, tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    job_id = submit_ai_work(client)
    public_jobs(client)
    submit(client, job_id, "wk-a", "The Salty Hoe")
    clock.t += INTERVAL - 2  # last second of the window
    assert public_jobs(client) == {"leased": 1}
    clock.t += 1  # first second of the next window
    assert public_jobs(client) == {"accepted": 1, "queued": 1} == ledger.status_counts()


def test_windows_follow_the_clock_not_the_last_request(pinned, tmp_path: Path) -> None:
    """A rig can't stretch or restart a window by asking at the right moment."""
    client, clock, _ = make(tmp_path)
    clock.t += INTERVAL - 2
    first = client.get("/health").json()
    assert first["jobs"] == {}
    window_start = int(clock.t) // INTERVAL * INTERVAL
    assert first["jobs_next_count_at"] == window_start + INTERVAL
    assert first["jobs_counted_at"] == int(clock.t)
    client.post("/v1/jobs", json={"payload": "x"})
    clock.t += 1
    assert client.get("/health").json()["jobs"] == {"queued": 1}


def test_the_auditor_cannot_see_its_claim_in_the_counts(pinned, tmp_path: Path) -> None:
    """The other side of the leak: claiming a re-check must not stand out either."""
    client, clock, _ = make(tmp_path)
    clock.t += INTERVAL - 12  # near the end of a window, so the re-check is still fresh
    job_id = submit_ai_work(client)
    submit(client, job_id, "wk-a", "The Salty Hoe")
    clock.t += 11  # first second of the next window
    before = public_jobs(client)
    assert before == {"accepted": 1, "queued": 1}
    assert claim(client, "wk-b").status_code == 200
    assert public_jobs(client) == before


def test_responses_say_how_counts_are_taken(pinned, tmp_path: Path) -> None:
    client, clock, _ = make(tmp_path)
    body = client.get("/v1/ledger").json()
    assert "public-counts-v1" in body["public_rules"]
    assert body["public_counts_interval_seconds"] == INTERVAL
    assert body["jobs_counted_at"] == int(clock.t)
    health = client.get("/health").json()
    assert health["public_counts_interval_seconds"] == INTERVAL
    op = client.get("/v1/ledger", headers=OP).json()
    assert op["public_counts_interval_seconds"] == 0 and op["jobs_next_count_at"] is None


def test_operator_counts_need_the_right_token(pinned, tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    assert client.get("/v1/ledger", headers={"X-Operator-Token": "x" * 40}).status_code == 403


def test_zero_means_live_counts_and_operator_health_warns(pinned, tmp_path: Path) -> None:
    client, _, _ = make(tmp_path, public_counts_interval_seconds=0)
    job_id = submit_ai_work(client)
    assert public_jobs(client) == {"leased": 1}
    submit(client, job_id, "wk-a", "The Salty Hoe")
    assert public_jobs(client) == {"accepted": 1, "queued": 1}
    body = client.get("/v1/ledger").json()
    assert "public-counts-v1" not in body["public_rules"]
    warnings = client.get("/v1/operator/health", headers=OP).json()["warnings"]
    assert any("PUBLIC_COUNTS_INTERVAL_SECONDS is 0" in w for w in warnings)


def test_no_warning_with_the_default(pinned, tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    health = client.get("/v1/operator/health", headers=OP).json()
    assert not any("PUBLIC_COUNTS_INTERVAL_SECONDS" in w for w in health["warnings"])
    assert health["public_counts_interval_seconds"] == INTERVAL


def test_the_setting_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    from app import config
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("PUBLIC_COUNTS_INTERVAL_SECONDS", "60")
    assert config.get_settings().public_counts_interval_seconds == 60
    monkeypatch.setenv("PUBLIC_COUNTS_INTERVAL_SECONDS", "-5")
    with pytest.raises(ValueError):
        config.get_settings()


def test_a_database_failure_still_reports_down(pinned, tmp_path: Path) -> None:
    """The snapshot must not hide an outage: /health still checks the database each time."""
    client, _, ledger = make(tmp_path)
    assert client.get("/health").status_code == 200

    def broken() -> None:
        raise OSError("disk gone")
    ledger.ping = broken
    assert client.get("/health").status_code == 503
