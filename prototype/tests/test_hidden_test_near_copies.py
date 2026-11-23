"""Near copies of revealed hidden tests (`test-reuse-v2`, threat model #11).

`test-reuse-v1` refused a new hidden test only when its input was byte-for-byte the input of
a hidden test a ledger copy had revealed. But a rig that remembers "SECRET-CHALLENGE-name-a-
ship" as a test recognises "secret challenge: name a ship?" just as well, and for
`llm:generate` the seed, max_tokens and timeout are part of the payload, so changing the
seed alone made a "new" input. v2 compares a fingerprint of the recognisable text
(`tasks.input_fingerprint`): case, spacing, punctuation, invisible characters, NFKC look-alike
forms and the job's settings are ignored. Rewording is still not caught (documented).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import challenge_pool, tasks
from app.ledger import HiddenTestRevealed, Ledger
from app.security import sha256_text
from tests.test_audits import MODEL_ID, OP, OPERATOR, llm_payload, make, pinned  # noqa: F401  (fixture)
from tests.test_challenge_pool import NOW, llm_test, new_pool
from tests.test_hidden_test_reuse import exported, hidden_test, job_count, operator_health
from tests.test_ledger_copy import CHALLENGE_PROMPT, EXPECTED, PROMPT, UNTIL

pytestmark = pytest.mark.usefixtures("pinned")

ZWSP = "​"  # zero-width space


def payload(prompt: str, **settings) -> str:
    return json.dumps({"model_id": MODEL_ID, "prompt": prompt, "max_tokens": 16, "seed": 7,
                       "timeout_s": 30} | settings)


# The revealed hidden test's prompt is CHALLENGE_PROMPT = "SECRET-CHALLENGE-name-a-ship".
NEAR_COPIES = {
    "lower case": payload(CHALLENGE_PROMPT.lower()),
    "spaces for dashes": payload("SECRET CHALLENGE name a ship"),
    "punctuation added": payload("Secret challenge: name a ship?!"),
    "extra spaces and newlines": payload("  SECRET-CHALLENGE -\n name-a-ship  "),
    "zero-width space inside a word": payload(f"SECRET-CHAL{ZWSP}LENGE-name-a-ship"),
    "full-width letters": payload("ＳＥＣＲＥＴ-CHALLENGE-name-a-ship"),
    "another seed": payload(CHALLENGE_PROMPT, seed=8),
    "other max_tokens and timeout": payload(CHALLENGE_PROMPT, max_tokens=200, timeout_s=90),
    "keys in another order": json.dumps({"seed": 7, "prompt": CHALLENGE_PROMPT, "timeout_s": 30,
                                         "max_tokens": 16, "model_id": MODEL_ID}),
}


def fp(p: str, task_type: str = "llm:generate") -> str:
    return tasks.input_fingerprint(task_type, p)


# -- the fingerprint ------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(NEAR_COPIES))
def test_near_copies_share_a_fingerprint(name: str) -> None:
    assert NEAR_COPIES[name] != payload(CHALLENGE_PROMPT)  # a different payload hash...
    assert fp(NEAR_COPIES[name]) == fp(payload(CHALLENGE_PROMPT))  # ...the same question


def test_different_questions_get_different_fingerprints() -> None:
    base = fp(payload("Name a ship"))
    for other in ("Name a boat", "Name a ship please", "Name two ships", "Name a ship 2"):
        assert fp(payload(other)) != base, other
    # Same text under another task type is another input.
    assert tasks.input_fingerprint("demo:sha256", "Name a ship") != base
    # demo:sha256 uses the whole payload as its text.
    assert (tasks.input_fingerprint("demo:sha256", "Name a ship")
            == tasks.input_fingerprint("demo:sha256", "name-a-ship!"))


def test_fingerprint_never_raises_and_falls_back_to_exact_text() -> None:
    # Garbled or unexpected payloads (old rows, unknown task types) still get a fingerprint.
    for task_type, odd in (("llm:generate", "{not json"), ("llm:generate", "[1, 2]"),
                           ("llm:generate", '{"prompt": 5}'), ("no:such-task", "hello"),
                           ("llm:generate", "")):
        assert len(tasks.input_fingerprint(task_type, odd)) == 64
    # Text with no letters or digits only matches itself exactly, not every other symbol run.
    assert fp(payload("?!")) != fp(payload("..."))
    assert fp(payload("?!")) == fp(payload("?!", seed=9))
    # The fingerprint is a hash: it never contains the prompt.
    assert "ship" not in fp(payload("Name a ship"))


# -- the coordinator ------------------------------------------------------------------------

def test_near_copies_of_a_revealed_test_are_refused(tmp_path: Path) -> None:
    client, _clock, ledger, _ = exported(tmp_path)
    before = job_count(ledger)
    for name, near in NEAR_COPIES.items():
        r = client.post("/v1/challenges", headers=OP, json={
            "task_type": "llm:generate", "payload": near, "expected_result": EXPECTED})
        assert r.status_code == 409, name
        detail = r.json()["detail"]
        assert "differs only in case, spacing, punctuation or settings" in detail, name
        assert "already a hidden test" in detail and str(UNTIL) in detail
        assert CHALLENGE_PROMPT.lower() not in r.text.lower() and EXPECTED not in r.text
    assert job_count(ledger) == before  # nothing queued, nothing half-written
    # Aimed tests go through the same check.
    for extra in ({"target_worker": "wk-a"}, {"target_tier": "new"}):
        r = client.post("/v1/challenges", headers=OP, json={
            "task_type": "llm:generate", "payload": NEAR_COPIES["another seed"],
            "expected_result": EXPECTED, **extra})
        assert r.status_code == 409
    assert job_count(ledger) == before


def test_an_exact_copy_still_says_exact(tmp_path: Path) -> None:
    client, *_ = exported(tmp_path)
    r = hidden_test(client)
    assert r.status_code == 409 and r.json()["detail"].startswith("this exact input")


def test_different_questions_and_normal_jobs_are_still_fine(tmp_path: Path) -> None:
    client, *_ = exported(tmp_path)
    assert hidden_test(client, "SECRET-CHALLENGE-name-a-ship-and-a-boat").status_code == 200
    # The period's normal AI job (PROMPT) wasn't a test, so a near copy of it isn't a tell.
    assert hidden_test(client, PROMPT.upper()).status_code == 200
    # Only hidden tests are refused: a normal job with a near-copy input reveals nothing.
    r = client.post("/v1/jobs", json={"task_type": "llm:generate",
                                      "payload": NEAR_COPIES["another seed"]})
    assert r.status_code == 200


def test_only_tests_from_before_until_count(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    assert hidden_test(client, "Made in the open period").status_code == 200
    clock.t += 50
    ledger.record_copy(int(clock.t), int(clock.t) - 100, "0" * 64)  # copy ends before it
    assert hidden_test(client, "made in the open period!").status_code == 200
    ledger.record_copy(int(clock.t), int(clock.t), "1" * 64)  # now the copy covers it
    assert hidden_test(client, "MADE IN THE OPEN PERIOD").status_code == 409


def test_ledger_refuses_near_copies_without_the_http_layer(tmp_path: Path) -> None:
    """Direct ledger check: the refusal lives in the ledger, not only in the API."""
    _client, clock, ledger, _ = exported(tmp_path)
    before = job_count(ledger)
    near = NEAR_COPIES["punctuation added"]
    fields = {"job_id": "job-direct", "task_type": "llm:generate", "payload": near,
              "payload_hash": sha256_text(near), "reward_points": 1,
              "expires_at": int(clock.t) + 600, "schema_version": "x", "model_sha256": None}
    with pytest.raises(HiddenTestRevealed) as caught:
        ledger.create_job(fields, "sig", 3, int(clock.t), job_kind="challenge",
                          expected_result=EXPECTED)
    assert caught.value.match == "near-copy" and caught.value.revealed_until == UNTIL
    assert job_count(ledger) == before
    # The same input as a normal job goes in.
    ledger.create_job(fields, "sig", 3, int(clock.t))
    assert job_count(ledger) == before + 1


def test_health_counts_near_copies_as_the_same_input(tmp_path: Path) -> None:
    client, _clock, _ledger = make(tmp_path)
    assert hidden_test(client, "Name a schooner").status_code == 200
    assert not any("same input" in w for w in operator_health(client)["warnings"])
    r = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": payload("name a schooner.", seed=99),
        "expected_result": EXPECTED})
    assert r.status_code == 200  # no copy made yet, so allowed, but flagged
    health = operator_health(client)
    assert health["hidden_test_reuse"]["open_sharing_input"] == 2
    assert any("2 open hidden test(s) use the same input" in w and "seed" in w
               for w in health["warnings"])
    assert "schooner" not in str(health).lower()


def test_retired_inputs_counts_near_copies_once(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    for prompt in ("Name a ketch", "NAME A KETCH", "Name a yawl"):
        assert hidden_test(client, prompt).status_code == 200
    clock.t += 10
    ledger.record_copy(int(clock.t), int(clock.t), "0" * 64)
    assert operator_health(client)["hidden_test_reuse"]["retired_inputs"] == 2


# -- the private pool -----------------------------------------------------------------------

def test_pool_skips_near_copies_of_its_own_tests(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 1)  # holds "Secret prompt 0"
    pool = challenge_pool.load_pool(path)
    near = {"task_type": "llm:generate", "payload": payload("secret PROMPT 0", seed=3),
            "expected_result": EXPECTED}
    added, skipped = challenge_pool.add_tests(
        pool, [("near", near), ("fresh", llm_test("Secret prompt 1")),
               ("near-in-batch", llm_test("SECRET PROMPT 1!"))], "test", NOW)
    assert added == 1 and len(skipped) == 2
    assert all("near copy" in line for line in skipped)
    assert all("secret" not in line.lower() for line in skipped)  # never prints the input


def test_pool_remembers_used_fingerprints(tmp_path: Path) -> None:
    path = new_pool(tmp_path, 1)
    client, _clock, _ledger = make(tmp_path)
    assert len(challenge_pool.issue_tests(path, client, OPERATOR, 1)["issued"]) == 1
    pool = challenge_pool.load_pool(path)
    used = pool["used"][0]
    assert used["input_fingerprint"] == fp(llm_payload("Secret prompt 0"))
    added, skipped = challenge_pool.add_tests(pool, [("again", llm_test("secret-prompt-0"))],
                                              "test", NOW)
    assert added == 0 and "near copy" in skipped[0]


def test_pool_still_reads_used_records_from_before_fingerprints(tmp_path: Path) -> None:
    """Pools written before 0.5.39 have used records without `input_fingerprint`: they load,
    and still block the exact input."""
    path = new_pool(tmp_path, 0)
    pool = challenge_pool.load_pool(path)
    pool["used"].append({"id": "old", "task_type": "llm:generate",
                         "input_sha256": challenge_pool.input_hash(
                             "llm:generate", llm_payload("Old prompt")),
                         "outcome": "issued", "job_id": "j", "target": None, "at": "x"})
    challenge_pool.save_pool(path, pool)
    pool = challenge_pool.load_pool(path)
    added, skipped = challenge_pool.add_tests(pool, [("exact", llm_test("Old prompt"))],
                                              "test", NOW)
    assert added == 0 and "used before" in skipped[0]
    assert os.stat(path).st_mode & 0o077 == 0


def test_pool_retires_a_near_copy_the_coordinator_refuses(tmp_path: Path) -> None:
    client, _clock, _ledger, _ = exported(tmp_path)
    path = new_pool(tmp_path, 0)
    pool = challenge_pool.load_pool(path)
    added, _ = challenge_pool.add_tests(
        pool, [("near", {"task_type": "llm:generate", "payload": NEAR_COPIES["lower case"],
                         "expected_result": EXPECTED})], "test", NOW)
    assert added == 1
    challenge_pool.save_pool(path, pool)
    result = challenge_pool.issue_tests(path, client, OPERATOR, 1)
    assert result["retired"] == 1 and result["issued"] == []
    assert [u["outcome"] for u in challenge_pool.load_pool(path)["used"]] == ["retired"]


def test_ledger_class_exposes_the_rule_names(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.sqlite3")
    reuse = ledger.test_reuse()
    assert reuse["rule"] == "test-reuse-v2" and reuse["fingerprint"] == "input-fingerprint-v1"
