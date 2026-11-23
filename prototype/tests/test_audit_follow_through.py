"""Threat model #4 follow-up: count the audits and tie-breakers a worker was randomly
handed and then let go (never picked up, lease ran out, result refused), so the operator can
spot a rig that keeps skipping checks. Operator-only; changes nothing about points."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from tests.auth_helpers import OP
from tests.test_assignment import assigned, make, other
from tests.test_audits import claim, llm_payload, pinned, run_llm_job, submit  # noqa: F401

pytestmark = pytest.mark.usefixtures("pinned")

WS = ("wk-a", "wk-b", "wk-c")


def follow(client, worker: str) -> dict[str, int]:
    r = client.get(f"/v1/workers/{worker}/trust", headers=OP)
    assert r.status_code == 200, r.text
    return r.json()["audit_follow_through"]


def test_unclaimed_assignment_counts_against_the_silent_pick_only(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, WS, assign=60)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    first = assigned(ledger, original)
    second = other(WS, "wk-a", first)
    clock.t += 61  # the first pick never showed up
    audit = claim(client, second).json()
    assert submit(client, audit["job_id"], second, "The Salty Hoe").status_code == 200
    assert follow(client, first) == {"assigned": 1, "completed": 0, "not_claimed": 1,
                                     "not_reached": 0, "lease_expired": 0, "refused": 0, "dropped": 1}
    assert follow(client, second) == {"assigned": 1, "completed": 1, "not_claimed": 0,
                                      "not_reached": 0, "lease_expired": 0, "refused": 0, "dropped": 0}
    assert follow(client, "wk-a")["assigned"] == 0  # the original worker is never a pick
    assert ledger.check_consistency() == []


def test_lease_that_runs_out_is_counted_once_not_twice(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, WS)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    first = assigned(ledger, original)
    assert claim(client, first).status_code == 200
    clock.t += 31  # took it, then went silent: lease runs out
    second = other(WS, "wk-a", first)
    assert claim(client, second).status_code == 200
    f = follow(client, first)
    assert (f["lease_expired"], f["not_claimed"], f["dropped"]) == (1, 0, 1)


def test_refused_audit_result_is_counted(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, WS)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    first = assigned(ledger, original)
    audit = claim(client, first).json()
    assert submit(client, audit["job_id"], first, "   ").status_code == 422  # blank output
    f = follow(client, first)
    assert (f["refused"], f["not_claimed"], f["dropped"]) == (1, 0, 1)
    # A refused result on ordinary work is not an audit drop.
    job = {"task_type": "llm:generate", "payload": llm_payload("Another boat.")}
    assert client.post("/v1/jobs", json=job).status_code == 200
    work = claim(client, "wk-a").json()
    assert submit(client, work["job_id"], "wk-a", "   ").status_code == 422
    assert follow(client, "wk-a")["refused"] == 0


def test_picked_again_for_the_same_audit_is_judged_on_the_latest_pick(tmp_path: Path) -> None:
    """Only one eligible auditor: it is re-picked after letting the first pick lapse."""
    client, clock, ledger = make(tmp_path, ("wk-a", "wk-b"), assign=60)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert assigned(ledger, original) == "wk-b"
    clock.t += 61
    audit = claim(client, "wk-b").json()  # re-picked (nobody else) and claims this time
    assert submit(client, audit["job_id"], "wk-b", "The Salty Hoe").status_code == 200
    assert follow(client, "wk-b") == {"assigned": 2, "completed": 1, "not_claimed": 1,
                                      "not_reached": 0, "lease_expired": 0, "refused": 0, "dropped": 1}


def test_revoked_pick_is_not_counted_as_a_drop(tmp_path: Path) -> None:
    client, _, ledger = make(tmp_path, WS)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    first = assigned(ledger, original)
    assert client.post(f"/v1/workers/{first}/revoke", headers=OP).status_code == 200
    second = other(WS, "wk-a", first)
    assert claim(client, second).status_code == 200
    assert follow(client, first)["dropped"] == 0


def test_drop_event_stays_private(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, WS, assign=60)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    first = assigned(ledger, original)
    clock.t += 61
    audit = claim(client, other(WS, "wk-a", first)).json()
    assert any(e["event"] == "audit_assignment_expired" for e in ledger.events(audit["job_id"]))
    public = client.get(f"/v1/jobs/{audit['job_id']}").json()
    assert all(e["event"] != "audit_assignment_expired" for e in public["events"])


def test_operator_health_warns_at_the_threshold(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path, ("wk-a", "wk-b"), assign=60)
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["audit_dropouts"] == {}
    assert not any("dropped" in w for w in health["warnings"])
    for i in range(3):  # wk-b is the only possible auditor and lets three audits lapse
        run_llm_job(client, "wk-a", f"boat {i}")
        clock.t += 61
        claim(client, "wk-a")  # wk-a's request triggers the re-assignment check
    health = client.get("/v1/operator/health", headers=OP).json()
    assert health["audit_dropouts"]["wk-b"] >= 3
    warning = next(w for w in health["warnings"] if "dropped" in w)
    assert "wk-b" in warning and "offline" in warning and "wk-a" not in warning
    assert health["status"] == "attention"
    assert client.get("/v1/operator/health").status_code in (401, 403)


def test_audit_drop_warn_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_DROP_WARN", "5")
    assert get_settings().audit_drop_warn == 5
    monkeypatch.setenv("AUDIT_DROP_WARN", "0")
    with pytest.raises(ValueError):
        get_settings()


def test_old_lease_does_not_excuse_a_later_unclaimed_pick(tmp_path: Path) -> None:
    """wk-b takes the audit and lets the lease run out, is picked again (nobody else is
    eligible) and this time never picks it up: both drops count."""
    client, clock, ledger = make(tmp_path, ("wk-a", "wk-b"), assign=60)
    original = run_llm_job(client, "wk-a", "The Salty Hoe")
    assert claim(client, "wk-b").status_code == 200
    clock.t += 31  # lease (30s) runs out
    claim(client, "wk-a")  # re-assigned to wk-b, the only eligible worker
    assert assigned(ledger, original) == "wk-b"
    clock.t += 61  # and this time wk-b never shows up
    claim(client, "wk-a")
    f = follow(client, "wk-b")
    # Picked a third time (still its turn), so assigned is 3; two drops so far.
    assert (f["assigned"], f["lease_expired"], f["not_claimed"], f["dropped"]) == (3, 1, 1, 2)


def queue_backlog(client, clock, n: int) -> None:
    for i in range(n):
        job = {"task_type": "llm:generate", "payload": llm_payload(f"Backlog boat {i}.")}
        assert client.post("/v1/jobs", json=job).status_code == 200
    clock.t += 1  # the backlog is strictly older than what comes next


def test_busy_pick_working_through_older_jobs_is_not_counted_as_a_drop(tmp_path: Path) -> None:
    """Jobs are handed out oldest first, so a pick that is online and busy with work queued
    before the audit can't reach the audit before its window runs out. It never saw the
    audit and had no say in it: `not_reached`, not a drop."""
    client, clock, ledger = make(tmp_path, ("wk-a", "wk-b"), assign=60)
    queue_backlog(client, clock, 4)
    first = claim(client, "wk-a").json()
    assert submit(client, first["job_id"], "wk-a", "The Salty Hoe").status_code == 200
    assert assigned(ledger, first["job_id"]) == "wk-b"  # the only eligible auditor
    audits = {a["job_id"] for a in ledger.audits() if a["audit_of"] == first["job_id"]}
    took = []
    for _ in range(4):  # wk-b keeps working: older backlog first, then the audit
        job = claim(client, "wk-b").json()
        took.append(job["job_id"])
        clock.t += 25  # inside the 30s lease
        assert submit(client, job["job_id"], "wk-b", "The Salty Hoe").status_code == 200
    assert took[-1] in audits and not set(took[:-1]) & audits
    f = follow(client, "wk-b")
    assert f == {"assigned": 2, "completed": 1, "not_claimed": 0, "not_reached": 1,
                 "lease_expired": 0, "refused": 0, "dropped": 0}
    # wk-a never came back for the audits it was handed: those still count.
    fa = follow(client, "wk-a")
    assert fa["not_claimed"] >= 1 and fa["not_reached"] == 0
    assert fa["dropped"] == fa["not_claimed"]
    assert ledger.check_consistency() == []


def test_work_taken_before_the_pick_does_not_make_it_busy(tmp_path: Path) -> None:
    """Only jobs leased inside the assignment window count as being busy."""
    client, clock, ledger = make(tmp_path, ("wk-a", "wk-b"), assign=60)
    queue_backlog(client, clock, 2)
    assert claim(client, "wk-b").status_code == 200  # takes backlog 0, then goes silent
    job = claim(client, "wk-a").json()
    assert submit(client, job["job_id"], "wk-a", "The Salty Hoe").status_code == 200
    assert assigned(ledger, job["job_id"]) == "wk-b"
    clock.t += 61
    claim(client, "wk-a")  # triggers the re-assignment check
    f = follow(client, "wk-b")
    assert (f["not_claimed"], f["not_reached"], f["dropped"]) == (1, 0, 1)


def test_busy_misses_do_not_trigger_the_health_warning(tmp_path: Path) -> None:
    # Warn at the very first drop.
    client, clock, ledger = make(tmp_path, ("wk-a", "wk-b"), assign=60, audit_drop_warn=1)
    queue_backlog(client, clock, 4)
    first = claim(client, "wk-a").json()
    assert submit(client, first["job_id"], "wk-a", "The Salty Hoe").status_code == 200
    for _ in range(4):  # same as above: busy with older work, then the audit
        job = claim(client, "wk-b").json()
        clock.t += 25
        assert submit(client, job["job_id"], "wk-b", "The Salty Hoe").status_code == 200
    f = follow(client, "wk-b")
    assert (f["not_reached"], f["dropped"]) == (1, 0)
    health = client.get("/v1/operator/health", headers=OP).json()
    assert "wk-b" not in health["audit_dropouts"]
    assert not any("wk-b" in w and "dropped" in w for w in health["warnings"])
    # wk-a, handed wk-b's audits and silent ever since, is still flagged.
    assert health["audit_dropouts"].get("wk-a", 0) >= 1
