"""`lead-tests-v1`: following up the vouch check with aimed hidden tests.

The vouch check (0.5.43) lists rigs that agreed with a rig that was later caught. The fair
follow-up is one hidden known-answer test aimed at each. These tests check that the vouch
check knows which leads already have a test waiting (so a second run doesn't pile tests onto
one rig) and that `challenge_pool issue-leads` aims exactly one test of the right task type
at each lead that still needs one, keeps every pool rule, and sends nothing on a dry run.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app import challenge_pool, vouches
from app.challenge_pool import PoolError
from app.security import sha256_text
from tests.test_audits import OP, OPERATOR, claim, pinned, submit  # noqa: F401  (fixture)
from tests.test_challenge_pool import NOW, llm_test, mock_client
from tests.test_vouches import confirmed_pair, fail_hidden_test, make_vouch, report, warnings

pytestmark = pytest.mark.usefixtures("pinned")
EXPECTED = "The Flagship"


def caught_with_leads(tmp_path: Path, auditors=("wk-b",)):
    """wk-a's work is confirmed by each auditor, then wk-a fails a hidden test."""
    client, clock, ledger = make_vouch(tmp_path)
    for i, auditor in enumerate(auditors):
        confirmed_pair(client, "wk-a", auditor, f"Name boat {i}.")
    fail_hidden_test(client, "wk-a")
    return client, clock, ledger


def pool_with(tmp_path: Path, items: list[dict[str, str]]) -> Path:
    path = tmp_path / "pool.json"
    challenge_pool.init_pool(path, NOW)
    pool = challenge_pool.load_pool(path)
    added, skipped = challenge_pool.add_tests(
        pool, [(f"t{i}", item) for i, item in enumerate(items)], "test", NOW)
    assert skipped == [] and added == len(items)
    challenge_pool.save_pool(path, pool)
    return path


def demo_test(seed: str) -> dict[str, str]:
    return {"task_type": "demo:sha256", "payload": seed, "expected_result": sha256_text(seed)}


def challenge_jobs(ledger) -> list[dict]:
    with ledger._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT job_id, task_type, target, status FROM jobs WHERE job_kind='challenge' "
            "AND target != 'worker:wk-a' ORDER BY created_at, job_id").fetchall()]


# -- the vouch check knows which leads already have a test waiting --------------------------

def test_lead_with_an_aimed_test_waiting_is_not_listed_to_test(tmp_path: Path) -> None:
    client, clock, _ = caught_with_leads(tmp_path)
    r = report(client)
    (lead,) = r["leads"]
    assert lead["open_aimed_tests"] == 0 and lead["task_types"] == ["llm:generate"]
    assert r["unchecked_leads"] == ["wk-b"] and r["leads_to_test"] == ["wk-b"]

    c = client.post("/v1/challenges", headers=OP, json={
        **llm_test("Fresh secret"), "target_worker": "wk-b", "ttl_seconds": 300})
    assert c.status_code == 200, c.text
    r = report(client)
    assert r["leads"][0]["open_aimed_tests"] == 1
    assert r["unchecked_leads"] == ["wk-b"] and r["leads_to_test"] == []
    (warning,) = [w for w in warnings(client) if "vouches" in w]
    assert "1 already have one waiting" in warning and "issue-leads" in warning
    summary = client.get("/v1/operator/health", headers=OP).json()["vouches"]
    assert summary["leads_to_test"] == []

    # Past its deadline the test no longer counts: the lead needs a new one.
    clock.t += 301
    r = report(client)
    assert r["leads"][0]["open_aimed_tests"] == 0 and r["leads_to_test"] == ["wk-b"]


def test_tests_aimed_at_a_tier_or_at_another_rig_dont_count(tmp_path: Path) -> None:
    client, clock, ledger = caught_with_leads(tmp_path)
    for target in ({"target_tier": "new"}, {"target_worker": "wk-c"}):
        c = client.post("/v1/challenges", headers=OP, json={
            **llm_test(f"Other secret {target}"), **target})
        assert c.status_code == 200, c.text
    assert report(client)["leads_to_test"] == ["wk-b"]
    assert ledger.open_aimed_tests(int(clock.t)) == {"wk-c": 1}


def test_pure_report_lists_leads_to_test() -> None:
    def row(job_id: str, kind: str, worker: str) -> dict:
        return {"job_id": job_id, "job_kind": kind, "worker_id": worker, "reward_points": 1,
                "accepted_at": 10}

    def rec(strikes: int = 0, passed: int = 0, revoked: bool = False) -> dict:
        return {"owner": "o", "revoked": revoked, "strikes": strikes,
                "hidden_tests_passed": passed, "first_struck_result_at": None}

    groups = [{"root": f"g{w}", "task_type": t, "agreed": [row(f"g{w}", "work", "x"),
                                                          row(f"g{w}a", "audit", w)]}
              for w, t in (("p", "llm:generate"), ("q", "demo:sha256"), ("r", "llm:generate"),
                           ("s", "llm:generate"), ("t", "llm:generate"))]
    groups.append({"root": "gq2", "task_type": "llm:generate",
                   "agreed": [row("gq2", "work", "x"), row("gq2a", "audit", "q")]})
    record = {"x": rec(1), "p": rec(), "q": rec(), "r": rec(passed=1), "s": rec(revoked=True),
              "t": rec(1)}
    r = vouches.build_report(groups, record, now=100, open_aimed={"p": 2})
    leads = {x["worker_id"]: x for x in r["leads"]}
    assert leads["q"]["task_types"] == ["demo:sha256", "llm:generate"]
    assert leads["p"]["open_aimed_tests"] == 2 and leads["q"]["open_aimed_tests"] == 0
    assert r["unchecked_leads"] == ["q", "p"]  # q agreed twice, so it comes first
    assert r["leads_to_test"] == ["q"]
    # Without the open-test counts (older callers), every unchecked lead is to test.
    assert vouches.build_report(groups, record, now=100)["leads_to_test"] == ["q", "p"]


# -- issue-leads against an in-process coordinator ------------------------------------------

def test_issue_leads_aims_one_test_per_lead_and_a_second_run_adds_none(tmp_path: Path) -> None:
    client, _, ledger = caught_with_leads(tmp_path, auditors=("wk-b", "wk-c"))
    path = pool_with(tmp_path, [llm_test(f"Lead secret {i}") for i in range(4)])
    first = challenge_pool.issue_to_leads(path, client, OPERATOR)
    assert first["stopped"] is None and first["skipped"] == [] and first["not_reached"] == []
    assert sorted(a["worker_id"] for a in first["aimed"]) == ["wk-b", "wk-c"]
    assert {a["task_type"] for a in first["aimed"]} == {"llm:generate"}
    jobs = challenge_jobs(ledger)
    assert sorted(j["target"] for j in jobs) == ["worker:wk-b", "worker:wk-c"]
    assert {j["job_id"] for j in jobs} == {a["job_id"] for a in first["aimed"]}
    assert first["ready_left"] == 2

    second = challenge_pool.issue_to_leads(path, client, OPERATOR)
    assert second["aimed"] == [] and sorted(second["already_waiting"]) == ["wk-b", "wk-c"]
    assert len(challenge_jobs(ledger)) == 2 and second["ready_left"] == 2
    assert "left alone" in challenge_pool.leads_text(second)

    # wk-b takes its test like normal work and passes: it is no longer unchecked.
    job = claim(client, "wk-b").json()
    assert job["job_id"] in {a["job_id"] for a in first["aimed"]}
    assert submit(client, job["job_id"], "wk-b", EXPECTED).status_code == 200
    assert report(client)["unchecked_leads"] == ["wk-c"]
    assert not any("vouches" in w and "wk-b" in w for w in warnings(client))


def test_issue_leads_uses_a_task_type_the_lead_agreed_on(tmp_path: Path) -> None:
    client, _, ledger = caught_with_leads(tmp_path)
    path = pool_with(tmp_path, [demo_test("practice-1")])
    before = path.read_bytes()
    result = challenge_pool.issue_to_leads(path, client, OPERATOR)
    assert result["aimed"] == []
    assert result["skipped"] == [{"worker_id": "wk-b",
                                  "reason": "no ready llm:generate test left in the pool"}]
    assert path.read_bytes() == before and challenge_jobs(ledger) == []
    # The operator can choose another type on purpose.
    forced = challenge_pool.issue_to_leads(path, client, OPERATOR, task_type="demo:sha256")
    assert [(a["worker_id"], a["task_type"]) for a in forced["aimed"]] == [
        ("wk-b", "demo:sha256")]


def test_dry_run_sends_nothing_and_leaves_the_pool_alone(tmp_path: Path) -> None:
    client, _, ledger = caught_with_leads(tmp_path, auditors=("wk-b", "wk-c"))
    path = pool_with(tmp_path, [llm_test("Only one secret")])
    before = path.read_bytes()
    result = challenge_pool.issue_to_leads(path, client, OPERATOR, dry_run=True)
    assert path.read_bytes() == before and challenge_jobs(ledger) == []
    # Only one test, so the dry run says one rig would get it and the other wouldn't.
    assert len(result["aimed"]) == 1 and result["aimed"][0]["job_id"] is None
    assert len(result["skipped"]) == 1
    text = challenge_pool.leads_text(result)
    assert "Would aim" in text and "Nothing was sent" in text


def test_max_leads_per_run(tmp_path: Path) -> None:
    client, _, ledger = caught_with_leads(tmp_path, auditors=("wk-b", "wk-c"))
    path = pool_with(tmp_path, [llm_test(f"Max secret {i}") for i in range(3)])
    result = challenge_pool.issue_to_leads(path, client, OPERATOR, max_leads=1)
    assert len(result["aimed"]) == 1 and len(result["not_reached"]) == 1
    assert len(challenge_jobs(ledger)) == 1
    # Next run picks up the one left over, not the one that already has a test.
    again = challenge_pool.issue_to_leads(path, client, OPERATOR, max_leads=1)
    assert [a["worker_id"] for a in again["aimed"]] == result["not_reached"]
    assert len(challenge_jobs(ledger)) == 2


def test_output_has_no_inputs_or_answers(tmp_path: Path) -> None:
    client, _, _ = caught_with_leads(tmp_path)
    path = pool_with(tmp_path, [llm_test("Very secret prompt")])
    result = challenge_pool.issue_to_leads(path, client, OPERATOR)
    text = challenge_pool.leads_text(result) + json.dumps(result)
    assert "Very secret prompt" not in text and EXPECTED not in text


# -- refusals and trouble (mock coordinator) -------------------------------------------------

def vouch_body(**extra) -> dict:
    body = {"leads": [{"worker_id": "wk-b", "task_types": ["llm:generate"]},
                      {"worker_id": "wk-c", "task_types": ["llm:generate"]}],
            "unchecked_leads": ["wk-b", "wk-c"], "leads_to_test": ["wk-b", "wk-c"]}
    return body | extra


def coordinator(posts: list[httpx.Response], body: dict | None = None, sent=None):
    sent = [] if sent is None else sent

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/v1/operator/vouches"
            return httpx.Response(200, json=vouch_body() if body is None else body)
        sent.append(json.loads(request.content))
        return posts.pop(0)
    return mock_client(handler)


@pytest.mark.parametrize("body, match", [
    ({"leads": [], "unchecked_leads": []}, "update it"),  # coordinator older than 0.5.44
    ("not json", "update it")])
def test_refuses_a_coordinator_without_leads_to_test(tmp_path: Path, body, match) -> None:
    path = pool_with(tmp_path, [llm_test("Old coordinator")])
    before = path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "nothing may be sent"
        return (httpx.Response(200, json=body) if isinstance(body, dict)
                else httpx.Response(200, text=body))
    with pytest.raises(PoolError, match=match):
        challenge_pool.issue_to_leads(path, mock_client(handler), OPERATOR)
    assert path.read_bytes() == before


@pytest.mark.parametrize("response, match", [
    (httpx.Response(403, json={"detail": "operator token required"}), "403"),
    (httpx.ConnectError("down"), "couldn't reach")])
def test_refuses_when_the_vouch_check_cant_be_read(tmp_path: Path, response, match) -> None:
    path = pool_with(tmp_path, [llm_test("Unreadable")])
    before = path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if isinstance(response, Exception):
            raise response
        return response
    with pytest.raises(PoolError, match=match):
        challenge_pool.issue_to_leads(path, mock_client(handler), OPERATOR)
    assert path.read_bytes() == before


def test_a_revealed_test_is_retired_and_another_one_is_sent(tmp_path: Path) -> None:
    path = pool_with(tmp_path, [llm_test(f"Maybe revealed {i}") for i in range(3)])
    sent: list[dict] = []
    posts = [httpx.Response(409, json={"detail": "this input was already a hidden test"}),
             httpx.Response(200, json={"job_id": "job-b"}),
             httpx.Response(200, json={"job_id": "job-c"})]
    result = challenge_pool.issue_to_leads(path, coordinator(posts, sent=sent), OPERATOR)
    assert result["retired"] == 1 and result["stopped"] is None
    assert [(a["worker_id"], a["job_id"]) for a in result["aimed"]] == [
        ("wk-b", "job-b"), ("wk-c", "job-c")]
    assert [s["target_worker"] for s in sent] == ["wk-b", "wk-b", "wk-c"]
    assert len({s["payload"] for s in sent}) == 3  # never the same test twice
    pool = challenge_pool.load_pool(path)
    assert sorted(u["outcome"] for u in pool["used"]) == ["issued", "issued", "retired"]


def test_stops_at_the_first_refusal_and_puts_the_test_back(tmp_path: Path) -> None:
    path = pool_with(tmp_path, [llm_test(f"Paused {i}") for i in range(2)])
    posts = [httpx.Response(503, json={"detail": "coordinator paused"})]
    result = challenge_pool.issue_to_leads(path, coordinator(posts), OPERATOR)
    assert result["aimed"] == [] and result["stopped"].startswith("503")
    assert result["not_reached"] == ["wk-c"] and result["skipped"] == []
    pool = challenge_pool.load_pool(path)
    assert len(pool["entries"]) == 2 and pool["used"] == []
    assert "Not reached this run: wk-c" in challenge_pool.leads_text(result)


@pytest.mark.parametrize("kwargs, match", [
    ({"max_leads": 0}, "--max"), ({"max_leads": challenge_pool.MAX_LEADS + 1}, "--max"),
    ({"task_type": "no:such"}, "unknown task type")])
def test_bad_options_are_refused_before_asking_anyone(tmp_path: Path, kwargs, match) -> None:
    path = pool_with(tmp_path, [llm_test("Options")])

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")
    with pytest.raises(PoolError, match=match):
        challenge_pool.issue_to_leads(path, mock_client(handler), OPERATOR, **kwargs)


def test_cli_needs_the_token_in_the_environment(tmp_path: Path, capsys, monkeypatch) -> None:
    path = pool_with(tmp_path, [llm_test("Cli lead")])
    monkeypatch.delenv("OPERATOR_TOKEN", raising=False)
    assert challenge_pool.main(["issue-leads", "--pool", str(path),
                                "--url", "http://127.0.0.1:9"]) == 2
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR)
    assert challenge_pool.main(["issue-leads", "--pool", str(path), "--url",
                                "http://127.0.0.1:9", "--max", "99"]) == 2
    out = capsys.readouterr()
    assert "--max" in out.err and "Cli lead" not in out.out + out.err
