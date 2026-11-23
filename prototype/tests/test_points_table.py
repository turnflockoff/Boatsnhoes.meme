"""Fixed points per task type (`points-table-v1`, threat model #16 follow-up).

Without POINTS_TABLE whoever queues a job picks its points (unchanged since 0.5.19). With
it, every new job and hidden test gets the table's number, and asking for any other
number is refused, so a submitter can't queue a 1000-point job for a friend and hidden
tests can't be spotted by their points.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import points, tasks
from app.config import Settings, get_settings
from app.main import create_app
from tests.auth_helpers import OP, OPERATOR, receipt
from tests.test_rate_limits import SUB, SUBMITTER, T0, join

TABLE = (("demo:sha256", 2), ("llm:generate", 10))


def sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def make(tmp_path: Path, table: tuple[tuple[str, int], ...] = TABLE,
         submitter_token: str | None = SUBMITTER) -> TestClient:
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        max_attempts=2, audit_rate=0.0, audit_rate_new=0.0,
                        audit_rate_suspect=0.0, operator_token=OPERATOR,
                        submitter_token=submitter_token, points_table=table)
    return TestClient(create_app(settings, clock=lambda: T0))


def job_count(client: TestClient) -> int:
    db = client.app.state.ledger._connect()
    try:
        return db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        db.close()


# -- reading the setting ----------------------------------------------------------------------

def test_parse_reads_every_task_type_with_commas_or_spaces() -> None:
    assert points.parse_points_table("demo:sha256=2, llm:generate=10", tasks.EXECUTORS) == TABLE
    assert points.parse_points_table(" llm:generate=10  demo:sha256=2 ", tasks.EXECUTORS) == TABLE
    assert points.parse_points_table("demo:sha256=0,llm:generate=1000",
                                     tasks.EXECUTORS) == (("demo:sha256", 0),
                                                          ("llm:generate", 1000))


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_or_unset_means_no_table(raw: str | None) -> None:
    assert points.parse_points_table(raw, tasks.EXECUTORS) == ()


@pytest.mark.parametrize("raw, message", [
    ("demo:sha256=2", "missing: llm:generate"),
    ("demo:sha256=2,llm:generate=10,img:classify=3", "unknown task type"),
    ("demo:sha256=2,demo:sha256=3,llm:generate=10", "twice"),
    ("demo:sha256=2,llm:generate=1001", "0-1000"),
    ("demo:sha256=-1,llm:generate=10", "must look like"),
    ("demo:sha256=2.5,llm:generate=10", "must look like"),
    ("demo:sha256,llm:generate=10", "must look like"),
    ("demo:sha256=lots,llm:generate=10", "must look like"),
])
def test_bad_tables_are_refused(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        points.parse_points_table(raw, tasks.EXECUTORS)


def test_settings_read_points_table_and_refuse_bad_ones(monkeypatch: pytest.MonkeyPatch,
                                                        tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.delenv("POINTS_TABLE", raising=False)
    assert get_settings().points_table == ()
    monkeypatch.setenv("POINTS_TABLE", "llm:generate=10,demo:sha256=2")
    assert get_settings().points_table == TABLE
    monkeypatch.setenv("POINTS_TABLE", "llm:generate=10")
    with pytest.raises(ValueError, match="missing"):
        get_settings()


def test_coordinator_refuses_a_table_that_skips_a_task_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="every task type"):
        make(tmp_path, table=(("demo:sha256", 2),))


# -- no table: unchanged ----------------------------------------------------------------------

def test_without_a_table_the_creator_still_picks_points(tmp_path: Path) -> None:
    client = make(tmp_path, table=())
    r = client.post("/v1/jobs", json={"payload": "a", "reward_points": 1000}, headers=SUB)
    assert r.status_code == 200 and r.json()["reward_points"] == 1000
    r = client.post("/v1/jobs", json={"payload": "b"}, headers=SUB)
    assert r.status_code == 200 and r.json()["reward_points"] == 1


# -- with a table -----------------------------------------------------------------------------

def test_with_a_table_jobs_get_the_tables_points(tmp_path: Path) -> None:
    client = make(tmp_path)
    r = client.post("/v1/jobs", json={"payload": "a"}, headers=SUB)
    assert r.status_code == 200 and r.json()["reward_points"] == 2
    # Sending the table's own number is fine too.
    r = client.post("/v1/jobs", json={"payload": "b", "reward_points": 2}, headers=SUB)
    assert r.status_code == 200 and r.json()["reward_points"] == 2


@pytest.mark.parametrize("headers", [SUB, OP], ids=["submitter", "operator"])
def test_with_a_table_any_other_number_is_refused_and_nothing_is_queued(
        tmp_path: Path, headers: dict[str, str]) -> None:
    client = make(tmp_path)
    for asked in (1000, 0, 3):
        r = client.post("/v1/jobs", json={"payload": "a", "reward_points": asked},
                        headers=headers)
        assert r.status_code == 422
        assert "fixed at 2" in r.json()["detail"]
    assert job_count(client) == 0


def test_the_points_are_inside_the_signed_job_the_worker_gets(tmp_path: Path) -> None:
    client = make(tmp_path)
    client.post("/v1/jobs", json={"payload": "a"}, headers=SUB)
    auth = join(client, "wk-a")
    r = client.post("/v1/jobs/next", params={"worker_id": "wk-a"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["reward_points"] == 2


def test_hidden_tests_pay_the_same_as_normal_work(tmp_path: Path) -> None:
    client = make(tmp_path)
    r = client.post("/v1/challenges", headers=OP,
                    json={"payload": "test", "expected_result": sha("test")})
    assert r.status_code == 200, r.text
    assert client.app.state.ledger.get_job(r.json()["job_id"])["reward_points"] == 2
    # A hidden test with different points would be a tell, so it's refused.
    r = client.post("/v1/challenges", headers=OP,
                    json={"payload": "test2", "expected_result": sha("test2"),
                          "reward_points": 50})
    assert r.status_code == 422 and "fixed at 2" in r.json()["detail"]
    assert job_count(client) == 1


def test_points_are_paid_from_the_table_in_the_reward_report(tmp_path: Path) -> None:
    client = make(tmp_path)
    client.post("/v1/jobs", json={"payload": "a"}, headers=SUB)
    auth = join(client, "wk-a")
    job = client.post("/v1/jobs/next", params={"worker_id": "wk-a"}, headers=auth).json()
    r = client.post(f"/v1/jobs/{job['job_id']}/submit", headers=auth, json={
        "worker_id": "wk-a", "result": sha("a"), "result_hash": sha(sha("a")),
        "started_at": T0, "completed_at": T0, **receipt(client, job["job_id"])})
    assert r.status_code == 200, r.text
    assert r.json()["reward_points"] == 2
    assert client.get("/v1/ledger").json()["total_simulated_points"] == 2


# -- operator health --------------------------------------------------------------------------

def test_health_warns_when_a_submitter_can_pick_points(tmp_path: Path) -> None:
    body = make(tmp_path / "a", table=()).get("/v1/operator/health", headers=OP).json()
    assert body["points"]["rule"] == "creator-chooses" and body["points"]["table"] is None
    assert any("POINTS_TABLE" in w for w in body["warnings"])


def test_health_shows_the_table_and_does_not_warn_when_one_is_set(tmp_path: Path) -> None:
    body = make(tmp_path).get("/v1/operator/health", headers=OP).json()
    assert body["points"]["rule"] == "points-table-v1"
    assert body["points"]["table"] == {"demo:sha256": 2, "llm:generate": 10}
    assert not any("POINTS_TABLE" in w for w in body["warnings"])


def test_health_does_not_warn_without_a_submitter_token(tmp_path: Path) -> None:
    body = make(tmp_path, table=(), submitter_token=None).get(
        "/v1/operator/health", headers=OP).json()
    assert not any("POINTS_TABLE" in w for w in body["warnings"])


def test_jobs_queued_before_a_table_change_keep_their_signed_points(tmp_path: Path) -> None:
    before = make(tmp_path)  # demo:sha256 = 2
    before.post("/v1/jobs", json={"payload": "queued earlier"}, headers=SUB)
    after = make(tmp_path, table=(("demo:sha256", 7), ("llm:generate", 10)))  # same database
    auth = join(after, "wk-a")
    job = after.post("/v1/jobs/next", params={"worker_id": "wk-a"}, headers=auth).json()
    assert job["reward_points"] == 2
    assert after.post("/v1/jobs", json={"payload": "new"}, headers=SUB).json()["reward_points"] == 7
