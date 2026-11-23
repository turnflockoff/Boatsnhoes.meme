"""Two-worker rehearsal (Track F): `python -m app.rehearsal`.

The rehearsal acts as two workers against a coordinator and records how it answers good and
bad submissions. These tests run it against an in-process coordinator, check it passes on
the real rules, fails when a rule is broken, and refuses to run where it could take someone
else's work."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import rehearsal
from app.rehearsal import Rehearsal, local_coordinator, main
from app.tasks import Sha256Task, Verdict

TRACK_F_CASES = {
    "dispatch_both_accepted", "duplicate_is_idempotent", "changed_answer_after_accept",
    "other_worker_replays_finished_job", "wrong_worker", "result_hash_mismatch",
    "missing_field", "not_json", "bad_timestamps", "unknown_receipt_version", "no_key",
    "wrong_key", "unknown_job", "receipt_replayed_on_other_job", "wrong_answer_requeued",
    "requeued_job_done_by_other_worker", "not_leased", "expired_submission",
    "expired_job_refused_by_worker", "tampered_job_refused", "signed_jobs_verify",
    "ledger_consistent", "no_jobs_left_open", "workers_revoked",
}


def run_local(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run once; return the report and what the coordinator's ledger looked like after
    (read before the temporary database is deleted)."""
    with local_coordinator() as (client, token, clock):
        report = Rehearsal(client, token, now=clock, wait=clock.advance, **kwargs).run()
        ledger = client.app.state.ledger
        after = {"paid_workers": {e["worker_id"] for e in ledger.entries()},
                 "revoked": {w for w in report["workers"]
                             if (ledger.get_worker(w) or {}).get("revoked_at") is not None},
                 "total_points": ledger.total_points()}
        return report, after


def by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in report["checks"]}


def test_every_check_passes_against_the_real_rules() -> None:
    report, after = run_local(run_id="rehearsal-test")
    assert report["aborted"] is None
    assert report["all_passed"] is True and report["failed"] == 0
    checks = by_name(report)
    assert TRACK_F_CASES <= checks.keys()
    assert checks["wrong_worker"]["got"] == 403
    assert checks["expired_submission"]["got"] == 410
    assert checks["receipt_replayed_on_other_job"]["got"] == 422
    # Both workers really got work, and both ended revoked.
    assert after["paid_workers"] == set(report["workers"])
    assert after["revoked"] == set(report["workers"])
    # Never claims more than it tested.
    assert report["proves_gpu"] is False and report["proves_inference"] is False
    assert report["rehearsal_version"] == "bnh-rehearsal/1"


def test_duplicate_is_paid_once() -> None:
    report, after = run_local()
    # Five jobs were accepted (1, 2, 3, 4 by the other worker, 5) at one point each,
    # despite the duplicate and the replays.
    assert after["total_points"] == 5
    assert by_name(report)["duplicate_is_idempotent"]["passed"]


def test_report_holds_no_secrets(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    with local_coordinator() as (client, token, clock):
        r = Rehearsal(client, token, now=clock, wait=clock.advance)
        report = r.run()
        keys = list(r.keys.values())
    rehearsal.write_report(out, report)
    text = out.read_text()
    assert token not in text
    assert keys and not any(k in text for k in keys)


def test_local_run_does_not_really_wait() -> None:
    import time
    start = time.monotonic()
    run_local()
    assert time.monotonic() - start < rehearsal.EXPIRY_TTL  # the clock moved, not the wall


def test_a_broken_rule_shows_up_as_a_failed_check(monkeypatch: pytest.MonkeyPatch) -> None:
    # A coordinator that accepts any answer: the wrong answer is no longer refused.
    monkeypatch.setattr(Sha256Task, "verify", lambda self, payload, result:
                        Verdict(True, "deterministic-recompute", "exact", ""))
    report, _ = run_local()
    checks = by_name(report)
    assert checks["wrong_answer_requeued"]["passed"] is False
    assert checks["wrong_answer_requeued"]["got"] == 200
    assert report["all_passed"] is False
    # It stops when its next step can't happen, and still cleans up after itself.
    assert report["aborted"] and checks["workers_revoked"]["passed"]


def test_refuses_a_coordinator_with_queued_work() -> None:
    with local_coordinator() as (client, token, clock):
        r = client.post("/v1/jobs", json={"payload": "someone's real job"},
                        headers={"X-Operator-Token": token})
        assert r.status_code == 200
        report = Rehearsal(client, token, now=clock, wait=clock.advance).run()
        assert "queued job" in report["aborted"]
        assert report["checks"] == [] and report["all_passed"] is False
        assert client.app.state.ledger.status_counts() == {"queued": 1}


def test_refuses_a_wrong_operator_token() -> None:
    with local_coordinator() as (client, _token, clock):
        report = Rehearsal(client, "x" * 40, now=clock, wait=clock.advance).run()
    assert "operator token was refused" in report["aborted"]
    assert report["all_passed"] is False


class InjectsForeignJob:
    """Client wrapper: just before the rehearsal queues its third job, someone else queues
    a job, so the next claim hands a rehearsal worker that job instead (it is a second
    older, and the oldest queued job goes first)."""

    def __init__(self, client: Any, token: str, clock: Any) -> None:
        self.client, self.token, self.clock, self.queued = client, token, clock, 0
        self.foreign: str | None = None

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if method == "POST" and url == "/v1/jobs":
            self.queued += 1
            if self.queued == 3 and self.foreign is None:
                r = self.client.post("/v1/jobs", json={"payload": "real work"},
                                     headers={"X-Operator-Token": self.token})
                self.foreign = r.json()["job_id"]
                self.clock.advance(1)
        return self.client.request(method, url, **kwargs)


def test_stops_without_running_someone_elses_job() -> None:
    with local_coordinator() as (client, token, clock):
        wrapper = InjectsForeignJob(client, token, clock)
        report = Rehearsal(wrapper, token, now=clock,  # type: ignore[arg-type]
                           wait=clock.advance).run()
        ledger = client.app.state.ledger
        assert "another job was queued" in report["aborted"]
        # Every check so far passed, but a run that stopped early never counts as passing.
        assert report["failed"] == 0 and report["all_passed"] is False
        assert by_name(report)["workers_revoked"]["passed"]
        # Never answered, and revoking the worker put it back in the queue for its owner.
        foreign = ledger.get_job(wrapper.foreign)
        assert foreign["status"] == "queued"
        assert all(e["job_id"] != wrapper.foreign for e in ledger.entries())


class Always429ThenOk:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls += 1
        if self.calls == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": "slow"})
        return httpx.Response(200, json={"ok": True})


def test_waits_as_asked_when_told_to_slow_down() -> None:
    waits: list[float] = []
    fake = Always429ThenOk()
    r = Rehearsal(fake, "t" * 40, wait=waits.append)  # type: ignore[arg-type]
    assert r.request("GET", "/v1/ledger").status_code == 200
    assert waits == [7] and fake.calls == 2


def test_cli_local_writes_a_report_and_never_overwrites(tmp_path: Path,
                                                        capsys: pytest.CaptureFixture) -> None:
    out = tmp_path / "rehearsal.json"
    assert main(["--local", "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["all_passed"] and report["coordinator"].startswith("in-process")
    assert "checks passed" in capsys.readouterr().out
    before = out.read_text()
    assert main(["--local", "--out", str(out)]) == 2
    assert out.read_text() == before


def test_write_report_never_overwrites(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    out.write_text("earlier run")
    with pytest.raises(FileExistsError):
        rehearsal.write_report(out, {"x": 1})
    assert out.read_text() == "earlier run"


def test_cli_url_needs_allow_writes_and_a_token_from_the_environment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "r.json"
    assert main(["--url", "http://127.0.0.1:9", "--out", str(out)]) == 2
    monkeypatch.delenv("OPERATOR_TOKEN", raising=False)
    assert main(["--url", "http://127.0.0.1:9", "--allow-writes", "--out", str(out)]) == 2
    assert not out.exists()
    with pytest.raises(SystemExit):  # one place to run against, not both
        main(["--local", "--url", "http://127.0.0.1:9", "--out", str(out)])
