"""`test-pool-v1`: a private stock of hidden tests, issued a few at a time (app/challenge_pool.py).

The rules that matter: the pool file is private, every test is checked like the coordinator
would check it, each test is used at most once (even after a crash or a lost connection),
a test a ledger copy revealed is retired, and nothing printed shows an input or an answer.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from app import challenge_pool
from app.challenge_pool import PoolError
from app.security import sha256_text
from tests.test_aimed_tests import EXPECTED, claim, make, submit, tier
from tests.test_audits import OP, OPERATOR, llm_payload, pinned  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("pinned")
NOW = 1_800_000_000.0


def llm_test(prompt: str, answer: str = EXPECTED) -> dict[str, str]:
    return {"task_type": "llm:generate", "payload": llm_payload(prompt), "expected_result": answer}


def new_pool(tmp_path: Path, n: int = 0) -> Path:
    path = tmp_path / "pool.json"
    challenge_pool.init_pool(path, NOW)
    if n:
        pool = challenge_pool.load_pool(path)
        added, skipped = challenge_pool.add_tests(
            pool, [(f"t{i}", llm_test(f"Secret prompt {i}")) for i in range(n)], "test", NOW)
        assert (added, skipped) == (n, [])
        challenge_pool.save_pool(path, pool)
    return path


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://coordinator")


# -- the file -------------------------------------------------------------------------------

def test_init_is_owner_only_and_never_overwrites(tmp_path: Path) -> None:
    path = new_pool(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert challenge_pool.pool_status(challenge_pool.load_pool(path))["ready_total"] == 0
    with pytest.raises(PoolError, match="already exists"):
        challenge_pool.init_pool(path, NOW)


def test_init_refuses_a_git_folder(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "sub").mkdir()
    with pytest.raises(PoolError, match="git folder"):
        challenge_pool.init_pool(tmp_path / "repo" / "sub" / "pool.json", NOW)
    assert not (tmp_path / "repo" / "sub" / "pool.json").exists()


def test_refuses_a_pool_others_can_read(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 1)
    os.chmod(path, 0o640)
    with pytest.raises(PoolError, match="other users"):
        challenge_pool.load_pool(path)


def test_refuses_a_damaged_or_foreign_file(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    for content in ("not json", json.dumps({"format": "something-else", "entries": [],
                                            "used": []})):
        path.write_text(content)
        os.chmod(path, 0o600)
        with pytest.raises(PoolError, match="pool file"):
            challenge_pool.load_pool(path)


def test_saving_keeps_it_owner_only(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 2)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_name(path.name + ".tmp").exists()


# -- adding tests ---------------------------------------------------------------------------

def test_add_checks_each_test_like_the_coordinator(tmp_path: Path) -> None:
    path = new_pool(tmp_path)
    pool = challenge_pool.load_pool(path)
    items = [
        ("good", llm_test("Name a ship.")),
        ("same again", llm_test("Name a ship.")),
        ("bad type", {"task_type": "gpu:mine", "payload": "x", "expected_result": "y"}),
        ("unpinned model", {"task_type": "llm:generate", "expected_result": "y",
                            "payload": json.dumps({"model_id": "nope", "prompt": "hi"})}),
        ("empty answer", llm_test("Other prompt", answer="   ")),
        ("too long", llm_test("x" * 5000)),
        ("wrong sha", {"task_type": "demo:sha256", "payload": "abc", "expected_result": "0" * 64}),
        ("right sha", {"task_type": "demo:sha256", "payload": "abc",
                       "expected_result": sha256_text("abc")}),
        ("not an object", ["llm:generate"]),
    ]
    added, skipped = challenge_pool.add_tests(pool, items, "test", NOW)
    assert added == 2
    reasons = "\n".join(skipped)
    for label, why in (("same again", "already in the pool"), ("bad type", "unknown task_type"),
                       ("unpinned model", "unknown model_id"), ("empty answer", "verifier"),
                       ("too long", "1-4096"), ("wrong sha", "verifier"),
                       ("not an object", "not a JSON object")):
        assert f"{label}: " in reasons and why in reasons.split(f"{label}: ")[1].split("\n")[0]
    # Skipped lines never repeat the input or the answer.
    assert "Name a ship" not in reasons and EXPECTED not in reasons


def test_a_used_test_can_never_be_added_again(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 1)
    client, _clock, _ledger = make(tmp_path)
    result = challenge_pool.issue_tests(path, client, OPERATOR, 1)
    assert len(result["issued"]) == 1
    pool = challenge_pool.load_pool(path)
    added, skipped = challenge_pool.add_tests(pool, [("again", llm_test("Secret prompt 0"))],
                                         "test", NOW)
    assert added == 0 and "used before" in skipped[0]


def test_make_demo_tests_pass_their_own_check() -> None:
    items = challenge_pool.make_demo_tests(5)
    assert len({i["payload"] for _, i in items}) == 5
    assert all(challenge_pool.check_test(i["task_type"], i["payload"], i["expected_result"]) is None
               for _, i in items)


def test_status_shows_counts_only(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 3)
    pool = challenge_pool.load_pool(path)
    status = challenge_pool.pool_status(pool)
    assert status["ready"] == {"llm:generate": 3} and status["used_total"] == 0
    text = challenge_pool.status_text(status) + json.dumps(status)
    assert "Secret prompt" not in text and EXPECTED not in text
    assert "Running low" in text


# -- issuing to a real (in-process) coordinator ---------------------------------------------

def test_issue_aims_tests_at_a_tier_and_crosses_them_off(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 3)
    client, _clock, ledger = make(tmp_path, workers=("wk-a",))
    assert tier(client, "wk-a") == "new"
    result = challenge_pool.issue_tests(path, client, OPERATOR, 2, target_tier="new")
    assert len(result["issued"]) == 2 and result["stopped"] is None
    assert result["ready_left"] == 1 and result["target"] == "tier:new"

    pool = challenge_pool.load_pool(path)
    assert len(pool["entries"]) == 1
    assert [u["outcome"] for u in pool["used"]] == ["issued", "issued"]
    issued = {u["job_id"] for u in pool["used"]}
    assert issued == {i["job_id"] for i in result["issued"]}
    for job_id in issued:
        job = ledger.get_job(job_id)
        assert job["job_kind"] == "challenge" and job["target"] == "tier:new"

    # The rig gets them like normal work, and the known answer is what's checked.
    job_id = claim(client, "wk-a").json()["job_id"]
    assert job_id in issued
    assert submit(client, job_id, "wk-a", EXPECTED).status_code == 200


def test_issue_never_sends_a_test_twice(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 3)
    client, _clock, ledger = make(tmp_path)
    first = challenge_pool.issue_tests(path, client, OPERATOR, 2)
    second = challenge_pool.issue_tests(path, client, OPERATOR, 2)
    assert len(first["issued"]) == 2 and len(second["issued"]) == 1  # pool ran out
    payloads = [ledger.get_job(i["job_id"])["payload"]
                for i in first["issued"] + second["issued"]]
    assert len(set(payloads)) == 3
    with pytest.raises(PoolError, match="no unused tests"):
        challenge_pool.issue_tests(path, client, OPERATOR, 1)
    assert "ran out" in challenge_pool.issue_text(second)


def test_issue_to_one_rig(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 1)
    client, _clock, ledger = make(tmp_path)
    result = challenge_pool.issue_tests(path, client, OPERATOR, 1, target_worker="wk-c")
    job = ledger.get_job(result["issued"][0]["job_id"])
    assert job["target"] == "worker:wk-c"


@pytest.mark.parametrize("setup, status", [
    ("pause", 503), ("wrong-token", 403), ("unknown-rig", 404), ("revoked-rig", 409)])
def test_clear_refusals_put_the_test_back_and_stop(tmp_path: Path, setup: str,
                                                   status: int) -> None:
    path = new_pool(tmp_path, 3)
    client, _clock, _ledger = make(tmp_path)
    token, worker = OPERATOR, None
    if setup == "pause":
        assert client.post("/v1/operator/pause", json={"reason": "drill"},
                           headers=OP).status_code == 200
    elif setup == "wrong-token":
        token = "x" * 40
    elif setup == "unknown-rig":
        worker = "wk-nobody"
    else:
        assert client.post("/v1/workers/wk-b/revoke", headers=OP).status_code == 200
        worker = "wk-b"
    before = challenge_pool.load_pool(path)
    result = challenge_pool.issue_tests(path, client, token, 3, target_worker=worker)
    assert result["issued"] == [] and result["stopped"].startswith(str(status))
    after = challenge_pool.load_pool(path)
    assert after["used"] == [] and len(after["entries"]) == 3
    assert {e["id"] for e in after["entries"]} == {e["id"] for e in before["entries"]}


def test_coordinator_refusing_the_test_itself_puts_it_back(tmp_path: Path,
                                                            monkeypatch) -> None:
    # The pool was filled where the model is pinned; this coordinator doesn't have it.
    path = new_pool(tmp_path, 1)
    client, _clock, _ledger = make(tmp_path)
    unpinned = tmp_path / "unpinned.json"
    unpinned.write_text(json.dumps({"models": [
        {"model_id": "tiny-test-model", "file_name": "tiny.gguf", "sha256": None,
         "source_url": "https://example.invalid/t", "license": "Apache-2.0",
         "size_bytes": None, "min_vram_mb": 1}]}))
    monkeypatch.setenv("MODEL_REGISTRY", str(unpinned))
    result = challenge_pool.issue_tests(path, client, OPERATOR, 1)
    assert result["stopped"].startswith("422")
    assert len(challenge_pool.load_pool(path)["entries"]) == 1


# -- the awkward answers (fake coordinator) -------------------------------------------------

def test_a_revealed_test_is_retired_and_the_batch_goes_on(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 2)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(409, json={"detail": "this exact input was already a hidden "
                                                       "test in a period a ledger copy covers"})
        return httpx.Response(200, json={"job_id": "job-2", "status": "queued"})

    result = challenge_pool.issue_tests(path, mock_client(handler), OPERATOR, 2)
    assert result["retired"] == 1 and [i["job_id"] for i in result["issued"]] == ["job-2"]
    pool = challenge_pool.load_pool(path)
    assert sorted(u["outcome"] for u in pool["used"]) == ["issued", "retired"]
    assert pool["entries"] == []


def test_lost_connection_keeps_the_test_crossed_off(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 3)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    result = challenge_pool.issue_tests(path, mock_client(handler), OPERATOR, 3)
    assert "lost contact" in result["stopped"] and result["unknown"] == 1
    pool = challenge_pool.load_pool(path)
    assert [u["outcome"] for u in pool["used"]] == ["unknown"]
    assert len(pool["entries"]) == 2  # stopped after the first


def test_unexpected_server_error_counts_as_maybe_sent(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 2)
    result = challenge_pool.issue_tests(
        path, mock_client(lambda r: httpx.Response(500, text="boom")), OPERATOR, 2)
    assert result["unknown"] == 1 and "500" in result["stopped"]
    assert [u["outcome"] for u in challenge_pool.load_pool(path)["used"]] == ["unknown"]


def test_a_crash_mid_send_leaves_the_test_used(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("power cut")

    with pytest.raises(RuntimeError):
        challenge_pool.issue_tests(path, mock_client(handler), OPERATOR, 1)
    pool = challenge_pool.load_pool(path)
    assert pool["entries"] == [] and pool["used"][0]["outcome"] == "sending"


def test_rate_limit_waits_then_carries_on(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 1)
    answers = [httpx.Response(429, headers={"Retry-After": "7"}),
               httpx.Response(200, json={"job_id": "job-1"})]
    waits: list[float] = []
    result = challenge_pool.issue_tests(path, mock_client(lambda r: answers.pop(0)), OPERATOR, 1,
                                   wait=waits.append)
    assert waits == [7] and [i["job_id"] for i in result["issued"]] == ["job-1"]


def test_issue_sends_no_points_and_the_right_target(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 1)
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.headers.get("X-Operator-Token"), json.loads(request.content)))
        return httpx.Response(200, json={"job_id": "job-1"})

    challenge_pool.issue_tests(path, mock_client(handler), OPERATOR, 1, target_tier="suspect",
                          ttl_seconds=900)
    token, body = sent[0]
    assert token == OPERATOR
    # Points come from the coordinator (POINTS_TABLE), so tests can't be spotted by them.
    assert "reward_points" not in body
    assert body["target_tier"] == "suspect" and body["ttl_seconds"] == 900
    assert body["expected_result"] == EXPECTED


@pytest.mark.parametrize("kwargs, why", [
    ({"count": 0}, "--count"), ({"count": 51}, "--count"),
    ({"count": 1, "target_tier": "vip"}, "--tier"),
    ({"count": 1, "target_tier": "new", "target_worker": "wk-a"}, "not both"),
    ({"count": 1, "task_type": "demo:sha256"}, "no unused tests in the pool for demo:sha256")])
def test_issue_refuses_bad_requests_before_touching_the_pool(tmp_path: Path, kwargs,
                                                             why: str) -> None:
    path = new_pool(tmp_path, 1)
    before = path.read_text()
    with pytest.raises(PoolError, match=why):
        challenge_pool.issue_tests(path, mock_client(lambda r: httpx.Response(200)), OPERATOR,
                              **kwargs)
    assert path.read_text() == before


# -- command line ---------------------------------------------------------------------------

def test_cli_round_trip(tmp_path: Path, capsys, monkeypatch) -> None:
    path = tmp_path / "pool.json"
    assert challenge_pool.main(["init", "--pool", str(path)]) == 0
    assert challenge_pool.main(["make-demo", "--pool", str(path), "--count", "4"]) == 0
    lines = tmp_path / "new.jsonl"
    lines.write_text(json.dumps(llm_test("Cli prompt")) + "\n\nnot json\n")
    # One line is skipped (not JSON), so the exit code is 1.
    assert challenge_pool.main(["add", "--pool", str(path), "--file", str(lines)]) == 1
    capsys.readouterr()
    assert challenge_pool.main(["status", "--pool", str(path), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["ready"] == {"demo:sha256": 4, "llm:generate": 1}

    monkeypatch.delenv("OPERATOR_TOKEN", raising=False)
    assert challenge_pool.main(["issue", "--pool", str(path), "--url", "http://127.0.0.1:9",
                           "--count", "1"]) == 2
    assert challenge_pool.main(["init", "--pool", str(path)]) == 2
    out = capsys.readouterr()
    assert "Cli prompt" not in out.out + out.err and EXPECTED not in out.out + out.err
