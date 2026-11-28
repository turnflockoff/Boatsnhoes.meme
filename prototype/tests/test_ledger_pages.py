"""Code audit, twelfth pass (`ledger-pages-v1`): what a big ledger costs strangers to read.

`GET /v1/ledger` is public. Before this pass every reply held *every* result ever accepted
and re-ran the whole consistency check, and `GET /v1/jobs/{id}` read every event ever
written to find one job's few. Measured on a real server with 100,000 results (about two
months of two rigs finishing a job a minute): one public ledger reply took 0.86 s of the
coordinator's time and was 20 MB. One address asking four times a second, inside the
240-a-minute request limit, left a rig waiting up to 7.8 s for its next job; the replies
piled up faster than they could be sent.

Now the entries come in pages (default 100, at most 1000) read through an index, the
point totals and the consistency check are taken once per public counts window (like the
job counts), and events are found by job through an index. Same ledger: 9 ms per public
reply, 20 KB, and the rig's waits didn't change during the same burst.

These tests check the behaviour (pages, cursors, limits, what each view shows) and, so a
later change can't quietly bring the whole-ledger reads back, that the queries behind the
public routes use their indexes (SQLite's own query plan, no timing).
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from app import copy_guard
from app.config import Settings
from app.ledger import MAX_PAGE, SCALE_INDEXES, Ledger
from app.main import create_app
from tests.auth_helpers import OP, FirstPickRng, WorkerClient, enroll
from tests.test_audits import T0, Clock, claim, sha, submit
from tests.test_copy_guard import honest, variant  # noqa: F401  (fixture)
from tests.test_audits import pinned  # noqa: F401  (fixture used by honest)

INTERVAL = Settings(database_path=Path("x")).public_counts_interval_seconds
DELAY = Settings(database_path=Path("x")).public_settle_delay_seconds


def make(tmp_path: Path, **overrides):
    clock = Clock()
    fields = dict(database_path=tmp_path / "db.sqlite3", audit_rate=0.0, audit_rate_new=0.0,
                  audit_rate_suspect=0.0, operator_token=OP["X-Operator-Token"])
    app = create_app(Settings(**(fields | overrides)), clock=clock, rng=FirstPickRng(1))
    client = WorkerClient(app)
    enroll(client, "wk-a")
    return client, clock, app.state.ledger


def run_demo_job(client: WorkerClient, payload: str) -> str:
    r = client.post("/v1/jobs", json={"task_type": "demo:sha256", "payload": payload})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert claim(client, "wk-a").json()["job_id"] == job_id
    assert submit(client, job_id, "wk-a", sha(payload)).status_code == 200
    return job_id


def add_results(ledger: Ledger, n: int, accepted_at: int = T0 - 10_000,
                same_time: bool = False) -> list[str]:
    """`n` accepted demo results written straight into the ledger (far faster than the API;
    the API path is covered by run_demo_job). Returns their job ids."""
    ids = [str(uuid.uuid4()) for _ in range(n)]
    with sqlite3.connect(ledger.path) as db:
        for i, job_id in enumerate(ids):
            at = accepted_at if same_time else accepted_at + i
            db.execute("INSERT INTO jobs(job_id, task_type, payload, payload_hash, "
                       "reward_points, expires_at, signature, status, worker_id, attempts, "
                       "max_attempts, created_at, updated_at, schema_version) VALUES "
                       "(?, 'demo:sha256', 'p', ?, 1, ?, 's', 'accepted', 'wk-a', 1, 3, ?, ?, "
                       "'bnh-job/1')", (job_id, sha("p"), at + 600, at, at))
            db.execute("INSERT INTO submissions(job_id, worker_id, result_hash, reward_points, "
                       "accepted_at) VALUES (?, 'wk-a', ?, 1, ?)", (job_id, sha("p"), at))
            db.execute("INSERT INTO events(job_id, worker_id, event, at) VALUES "
                       "(?, 'wk-a', 'job_accepted', ?)", (job_id, at))
    return ids


def walk(client: WorkerClient, order: str = "oldest", limit: int = 7,
         headers: dict | None = None) -> list[dict]:
    """Every entry, page by page, following next_after."""
    seen, after = [], None
    for _ in range(1000):
        params = {"limit": limit, "order": order} | ({"after": after} if after else {})
        body = client.get("/v1/ledger", params=params, headers=headers or {}).json()
        assert len(body["entries"]) <= limit
        seen += body["entries"]
        after = body["next_after"]
        if after is None:
            return seen
    raise AssertionError("next_after never ran out")


# -- pages --------------------------------------------------------------------------------

def test_default_page_is_small_and_says_how_to_get_the_rest(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    add_results(ledger, 250)
    body = client.get("/v1/ledger").json()
    assert len(body["entries"]) == 100 and body["entries_limit"] == 100
    assert body["entries_count"] == 250 and body["entries_order"] == "oldest"
    last = body["entries"][-1]
    assert body["next_after"] == f"{last['accepted_at']}:{last['job_id']}"
    assert "ledger-pages-v1" in body["public_rules"]


def test_following_next_after_gives_every_result_once_in_order(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    ids = add_results(ledger, 40)
    oldest = walk(client)
    assert [e["job_id"] for e in oldest] == ids
    newest = walk(client, order="newest")
    assert [e["job_id"] for e in newest] == ids[::-1]


def test_results_accepted_in_the_same_second_are_neither_skipped_nor_repeated(
        tmp_path: Path) -> None:
    """The cursor carries the job id too: with only the time, a page boundary inside one
    second would drop or repeat rows."""
    client, _, ledger = make(tmp_path)
    ids = add_results(ledger, 23, same_time=True)
    for order in ("oldest", "newest"):
        got = [e["job_id"] for e in walk(client, order=order, limit=5)]
        assert sorted(got) == sorted(ids) and len(got) == len(set(got)) == 23
    # Right answer without the index too (it makes paging fast, not correct).
    with sqlite3.connect(ledger.path) as db:
        db.execute("DROP INDEX submissions_by_time")
    for order in ("oldest", "newest"):
        got = [e["job_id"] for e in walk(client, order=order, limit=5)]
        assert got == sorted(ids, reverse=order == "newest")


def test_ledger_refuses_pages_outside_the_limits(tmp_path: Path) -> None:
    """The ledger checks too, not only the API, so another caller can't ask for it all."""
    _, _, ledger = make(tmp_path)
    for limit in (0, -1, MAX_PAGE + 1):
        with pytest.raises(ValueError):
            ledger.entries_page(limit=limit)


def test_last_page_has_no_next_after_even_when_it_is_exactly_full(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    add_results(ledger, 10)
    body = client.get("/v1/ledger", params={"limit": 10}).json()
    assert len(body["entries"]) == 10 and body["next_after"] is None
    empty = make(tmp_path / "empty")[0].get("/v1/ledger").json()
    assert empty["entries"] == [] and empty["next_after"] is None
    assert empty["entries_count"] == 0


def test_new_results_show_up_on_later_pages(tmp_path: Path) -> None:
    """Oldest-first cursors keep working while the ledger grows: a result accepted after
    the first page was read is on a later page, not lost."""
    client, _, ledger = make(tmp_path)
    add_results(ledger, 5)
    first = client.get("/v1/ledger", params={"limit": 5}).json()
    assert first["next_after"] is None
    new = run_demo_job(client, "later")
    last = first["entries"][-1]
    nxt = client.get("/v1/ledger", params={"after": f"{last['accepted_at']}:{last['job_id']}"})
    assert [e["job_id"] for e in nxt.json()["entries"]] == [new]


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": MAX_PAGE + 1}, {"limit": -1}, {"limit": "lots"},
    {"limit": 10 ** 30}, {"order": "random"}, {"order": "x" * 50},
    {"after": "nonsense"}, {"after": "123"}, {"after": ":abc"}, {"after": "12:"},
    {"after": "-5:abc"}, {"after": "1e9:abc"}, {"after": "12:a b"}, {"after": "12:ab;--"},
    {"after": "12:" + "a" * 81}, {"after": "9" * 17 + ":abc"},
    {"after": f"{2 ** 53 + 1}:abc"}, {"after": "12:abc\n"}, {"after": "x" * 5000},
    {"after": "12:аbc"},  # Cyrillic a
])
def test_bad_page_parameters_are_refused_without_echoing_them(tmp_path: Path,
                                                               params: dict) -> None:
    client, _, ledger = make(tmp_path)
    add_results(ledger, 3)
    for headers in ({}, OP):
        r = client.get("/v1/ledger", params=params, headers=headers)
        assert r.status_code == 422, (params, r.status_code)
        for value in params.values():
            if isinstance(value, str) and len(value) > 3:
                assert value not in r.text


def test_a_valid_cursor_for_a_row_that_does_not_exist_just_continues(tmp_path: Path) -> None:
    """Cursors are positions, not lookups: any well-formed one is a place to start."""
    client, _, ledger = make(tmp_path)
    ids = add_results(ledger, 4)
    r = client.get("/v1/ledger", params={"after": f"{T0 - 10_000 + 1}:zzz"})
    assert r.status_code == 200
    assert [e["job_id"] for e in r.json()["entries"]] == ids[2:]
    assert client.get("/v1/ledger", params={"after": "0:a"}).json()["entries_count"] == 4


def test_largest_page_is_capped(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    add_results(ledger, MAX_PAGE + 5)
    body = client.get("/v1/ledger", params={"limit": MAX_PAGE}).json()
    assert len(body["entries"]) == MAX_PAGE and body["next_after"]
    assert len(client.get("/v1/ledger").content) < 50_000  # default page, about 20 KB


# -- what each view shows -----------------------------------------------------------------

def test_pages_keep_the_public_rules(tmp_path: Path) -> None:
    """Paging changes how many rows come back, not what a row shows: no result hashes in
    public, and the settle delay withholds confidence on every page."""
    client, clock, ledger = make(tmp_path)
    add_results(ledger, 6)
    fresh = run_demo_job(client, "fresh")
    public = walk(client, limit=2)
    assert all("result_hash" not in e for e in public)
    by_id = {e["job_id"]: e for e in public}
    assert by_id[fresh]["settlement_visible"] is False and by_id[fresh]["confidence"] is None
    assert all(e["settlement_visible"] for k, e in by_id.items() if k != fresh)
    live = walk(client, limit=2, headers=OP)
    assert all(e["result_hash"] == sha("p") for e in live if e["job_id"] != fresh)
    assert {e["job_id"]: e["confidence"] for e in live}[fresh] == "exact"


def test_public_totals_and_consistency_are_taken_once_per_window(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    clock.t = (T0 // INTERVAL) * INTERVAL + 10  # start of a window, then some
    first = client.get("/v1/ledger").json()
    assert first["total_simulated_points"] == 0 and first["consistency_problems"] == []
    assert first["totals_taken_at"] == clock.t
    run_demo_job(client, "a")
    with sqlite3.connect(ledger.path) as db:  # break the books on purpose
        db.execute("UPDATE submissions SET reward_points=99")
    clock.t += 5
    same = client.get("/v1/ledger").json()
    assert same["total_simulated_points"] == 0 and same["consistency_problems"] == []
    assert same["totals_taken_at"] == first["totals_taken_at"]
    assert same["entries_count"] == 1  # rows themselves are live, as before
    live = client.get("/v1/ledger", headers=OP).json()
    assert live["total_simulated_points"] == 99 and live["totals_taken_at"] == clock.t
    assert any("reward mismatch" in p for p in live["consistency_problems"])
    clock.t += INTERVAL
    later = client.get("/v1/ledger").json()
    assert later["total_simulated_points"] == 99 and later["totals_taken_at"] == clock.t
    assert any("reward mismatch" in p for p in later["consistency_problems"])


def test_public_summary_is_live_when_counts_are_live(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, public_counts_interval_seconds=0)
    assert client.get("/v1/ledger").json()["total_simulated_points"] == 0
    run_demo_job(client, "a")
    assert client.get("/v1/ledger").json()["total_simulated_points"] == 1


def test_public_totals_still_hold_back_recent_results(tmp_path: Path) -> None:
    """The batched totals use the settle delay as of the moment they were taken."""
    client, clock, _ = make(tmp_path)
    clock.t = (T0 // INTERVAL) * INTERVAL
    run_demo_job(client, "a")
    body = client.get("/v1/ledger").json()
    assert body["points_by_status"]["pending"] == 1
    clock.t += DELAY
    assert client.get("/v1/ledger").json()["points_by_status"]["pending"] == 0


# -- indexes: the public routes must not read the whole ledger ----------------------------

def plan(ledger: Ledger, sql: str, args: tuple) -> str:
    with sqlite3.connect(ledger.path) as db:
        return " | ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_page_query_uses_the_time_index(tmp_path: Path) -> None:
    _, _, ledger = make(tmp_path)
    for order, op in (("ASC", ">"), ("DESC", "<")):
        sql = ("SELECT job_id FROM submissions WHERE (accepted_at, job_id) " + op +
               f" (?, ?) ORDER BY accepted_at {order}, job_id {order} LIMIT ?")
        p = plan(ledger, sql, (1, "a", 10))
        assert "submissions_by_time" in p and "TEMP B-TREE" not in p, p
    # And the code really issues that query: a page of 1 out of many reads 2 rows.
    add_results(ledger, 50)
    rows, cursor = ledger.entries_page(limit=1)
    assert len(rows) == 1 and cursor is not None


def test_events_of_one_job_use_an_index(tmp_path: Path) -> None:
    _, _, ledger = make(tmp_path)
    p = plan(ledger, "SELECT * FROM events WHERE job_id=? ORDER BY id", ("x",))
    assert "events_by_job" in p and "SCAN events" not in p, p


def test_audit_lookups_use_an_index(tmp_path: Path) -> None:
    _, _, ledger = make(tmp_path)
    p = plan(ledger, "SELECT 1 FROM jobs a WHERE a.audit_of=? AND a.job_kind='audit'", ("x",))
    assert "jobs_by_audit_of" in p, p


def test_public_job_status_reads_only_that_jobs_events(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path)
    add_results(ledger, 30)
    job_id = run_demo_job(client, "mine")
    events = client.get(f"/v1/jobs/{job_id}").json()["events"]
    assert events and {e["job_id"] for e in events} == {job_id}


def test_an_older_ledger_gets_the_indexes_when_opened(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    Ledger(path)
    with sqlite3.connect(path) as db:
        for name in ("submissions_by_time", "events_by_job", "jobs_by_audit_of"):
            db.execute(f"DROP INDEX {name}")
    Ledger(path)
    with sqlite3.connect(path) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"submissions_by_time", "events_by_job", "jobs_by_audit_of"} <= names
    assert len(SCALE_INDEXES) == 3


def test_ledger_copies_with_and_without_the_new_indexes_both_pass(honest: dict) -> None:
    """A real exported copy now carries the indexes and opens. Copies made before this
    version lack them and must still open; a copy may not carry some *other* index, as
    before (`copy-open-v1`)."""
    with sqlite3.connect(honest["copy"]) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"submissions_by_time", "events_by_job", "jobs_by_audit_of"} <= names
    copy_guard.vet(honest["copy"])
    old = variant(honest, "old.sqlite3", "DROP INDEX submissions_by_time",
                  "DROP INDEX events_by_job", "DROP INDEX jobs_by_audit_of")
    copy_guard.vet(old)
    assert Ledger(old, untrusted=True).entries_page(limit=5)[0]
    odd = variant(honest, "odd.sqlite3", "CREATE INDEX events_odd ON events(detail)")
    with pytest.raises(copy_guard.UnsafeCopyError):
        copy_guard.vet(odd)
