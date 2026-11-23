"""Threat model #14 follow-up (`public-no-hashes-v1`): no result hashes in public views.

`GET /v1/ledger` used to list every accepted result with its `result_hash`, which is the
sha256 of the answer itself. A re-check runs the same job again, so when it agrees it has
the *same* hash as the original. That undid most of `public-delay-v1`:

- anyone could pair a re-check with the job it re-checked, at once and forever, so a rig
  could count how often its own work is re-checked (and work out its trust tier);
- the auditor could find its own hash on another rig's earlier row and know its job had
  been a re-check, the very thing withholding `confidence` was meant to hide;
- a passed hidden test showed the sha256 of the operator's expected answer.

Public views now never carry a result hash. The operator's live view keeps them, and
ledger copies already blanked them (0.5.13).
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.auth_helpers import OP
from tests.test_audits import claim, pinned, run_llm_job, sha, submit  # noqa: F401
from tests.test_public_delay import DELAY, hidden_test, make, public


def everything_public(client, job_ids) -> str:
    """Every response anyone can read without a token, as one string to search in."""
    parts = [client.get("/v1/ledger").text, client.get("/health").text]
    parts += [client.get(f"/v1/jobs/{job_id}").text for job_id in job_ids]
    return "\n".join(parts)


def test_public_entries_have_no_result_hash(pinned, tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    run_llm_job(client, "wk-a", "The Salty Hoe")
    body = public(client)
    assert body["entries"] and all("result_hash" not in e for e in body["entries"])
    assert body["hidden_fields"] == ["result_hash"]
    assert "public-no-hashes-v1" in body["public_rules"]


def test_operator_view_keeps_result_hashes(pinned, tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    run_llm_job(client, "wk-a", "The Salty Hoe")
    live = client.get("/v1/ledger", headers=OP).json()
    assert live["entries"][0]["result_hash"] == sha("The Salty Hoe")
    assert live["hidden_fields"] == [] and live["public_rules"] == []


def test_a_recheck_cannot_be_paired_with_its_original(pinned, tmp_path: Path) -> None:
    """Before the fix both rows showed sha("The Salty Hoe"), so the pair was obvious, and
    wk-b could spot its own hash on wk-a's earlier row. Not even after the delay."""
    client, clock, ledger = make(tmp_path)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    audit = claim(client, "wk-b").json()["job_id"]
    assert ledger.get_job(audit)["audit_of"] == original
    assert submit(client, audit, "wk-b", "The Salty Hoe").status_code == 200
    answer_hash = sha("The Salty Hoe")
    assert answer_hash not in everything_public(client, [original, audit])
    clock.t += DELAY
    body = public(client)
    assert {e["confidence"] for e in body["entries"]} == {"replicated"}  # delay passed
    assert answer_hash not in everything_public(client, [original, audit])
    # The operator still sees that the two agree.
    live = client.get("/v1/ledger", headers=OP).json()["entries"]
    assert {e["result_hash"] for e in live} == {answer_hash}


def test_a_passed_hidden_test_does_not_show_its_expected_answer_hash(pinned,
                                                                      tmp_path: Path) -> None:
    client, clock, _ = make(tmp_path)
    job_id = hidden_test(client, "wk-a", "The Flagship")
    clock.t += DELAY
    assert sha("The Flagship") not in everything_public(client, [job_id])


def test_entries_keep_everything_else(pinned, tmp_path: Path) -> None:
    """Only the hash goes: job, rig, points, time and (after the delay) how it was checked
    stay public, so a rig can still find its own row."""
    client, clock, _ = make(tmp_path)
    job_id = run_llm_job(client, "wk-a", "The Salty Hoe")
    clock.t += DELAY
    entry = public(client)["entries"][0]
    assert set(entry) == {"job_id", "worker_id", "reward_points", "accepted_at",
                          "verifier_method", "confidence", "settlement_visible"}
    assert entry["job_id"] == job_id and entry["worker_id"] == "wk-a"
    assert json.dumps(entry)  # still plain JSON


def test_zero_delay_still_hides_hashes(pinned, tmp_path: Path) -> None:
    """`PUBLIC_SETTLE_DELAY_SECONDS=0` brings back the live settlement view, not the hashes:
    pairing a re-check with its original needs no timing at all."""
    client, _, _ = make(tmp_path, public_settle_delay_seconds=0)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    audit = claim(client, "wk-b").json()["job_id"]
    assert submit(client, audit, "wk-b", "The Salty Hoe").status_code == 200
    assert sha("The Salty Hoe") not in everything_public(client, [original, audit])
    rules = public(client)["public_rules"]
    assert "public-no-hashes-v1" in rules and "public-delay-v1" not in rules
