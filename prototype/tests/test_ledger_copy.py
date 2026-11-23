"""Shareable ledger copies (threat model #10, second step): a redacted copy of the ledger up
to the end of a closed period, that anyone can re-run a signed reward report against."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import ledger_copy, rewards
from app.ledger import Ledger
from app.ledger_copy import CopyError, export_copy
from app.security import sha256_text
from tests.auth_helpers import enroll
from tests.test_audits import (
    OP,
    T0,
    claim,
    llm_payload,
    make,
    pinned,  # noqa: F401  (pytest fixture)
    submit,
)
from tests.test_rewards import sha_job

UNTIL = T0 + 100
PROMPT = "SECRET-PROMPT-name-a-yacht"
ANSWER = "SECRET-ANSWER-The-Salty-Hoe"
CHALLENGE_PROMPT = "SECRET-CHALLENGE-name-a-ship"
EXPECTED = "SECRET-EXPECTED-The-Flagship"
WRONG = "SECRET-WRONG-ANSWER-Boaty"


def rows(path: Path, sql: str, args: tuple = ()) -> list[dict]:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def busy_period(client) -> dict[str, str]:
    """One of everything: an exact job, a disputed AI job settled by a tie-breaker (wk-b is
    outvoted), and a failed hidden challenge. All at T0, i.e. inside [0, UNTIL)."""
    ids = {"exact": sha_job(client, "wk-a", points=4)}
    assert client.post("/v1/jobs", json={"task_type": "llm:generate",
                                         "payload": llm_payload(PROMPT)}).status_code == 200
    ids["llm"] = claim(client, "wk-a").json()["job_id"]
    assert submit(client, ids["llm"], "wk-a", ANSWER).status_code == 200
    ids["audit"] = claim(client, "wk-b").json()["job_id"]
    assert submit(client, ids["audit"], "wk-b", WRONG).status_code == 200
    ids["tiebreak"] = claim(client, "wk-c").json()["job_id"]
    assert submit(client, ids["tiebreak"], "wk-c", ANSWER).status_code == 200
    c = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload(CHALLENGE_PROMPT),
        "expected_result": EXPECTED}).json()
    ids["challenge"] = claim(client, "wk-c").json()["job_id"]
    assert ids["challenge"] == c["job_id"]
    assert submit(client, ids["challenge"], "wk-c", WRONG).status_code == 200
    return ids


def signed_report(client, until: int = UNTIL) -> dict:
    r = client.get("/v1/rewards/report", params={"until": until, "sign": "true"}, headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.usefixtures("pinned")
def test_signed_report_rechecks_against_the_copy(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    ids = busy_period(client)
    clock.t += 200
    rep = signed_report(client)
    out = export_copy(ledger.path, tmp_path / "copy.sqlite3", UNTIL, int(clock.t))
    assert out["format"] == "bnh-ledger-copy/1" and out["submissions"] == 5
    copy = Ledger(tmp_path / "copy.sqlite3", read_only=True)
    assert copy.copy_until() == UNTIL and ledger.copy_until() is None
    # The same signed report re-checks cleanly against the copy: numbers and signature.
    key = client.app.state.signer.public_key_hex
    assert rewards.verify_report(rep, copy, key) == []
    assert copy.check_consistency() == []
    # The settlement story survives: the tie-breaker sided with wk-a, wk-b was outvoted.
    status = {r["job_id"]: r["audit_status"]
              for r in rows(copy.path, "SELECT job_id, audit_status FROM submissions")}
    assert status[ids["llm"]] == "confirmed" and status[ids["audit"]] == "outvoted"
    assert status[ids["challenge"]] == "failed-challenge"
    # Nothing is left lying around next to it.
    assert sorted(p.name for p in tmp_path.glob("copy.sqlite3*")) == ["copy.sqlite3"]


@pytest.mark.usefixtures("pinned")
def test_copy_holds_no_secrets(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    busy_period(client)
    clock.t += 200
    src = ledger.path
    export_copy(src, tmp_path / "copy.sqlite3", UNTIL, int(clock.t))
    raw = (tmp_path / "copy.sqlite3").read_bytes()
    secrets = [PROMPT, ANSWER, WRONG, CHALLENGE_PROMPT, EXPECTED, sha256_text(EXPECTED),
               sha256_text(ANSWER), sha256_text(WRONG), "owner-wk-a", "owner-wk-b"]
    secrets += list(client.worker_keys.values())
    secrets += [hashlib.sha256(k.encode()).hexdigest() for k in client.worker_keys.values()]
    for r in rows(src, "SELECT signature, payload_hash, expected_result FROM jobs"):
        secrets += [r["signature"], r["payload_hash"]]
        if r["expected_result"]:
            secrets.append(r["expected_result"])
    secrets += [r["code_hash"] for r in rows(src, "SELECT code_hash FROM enrollments")]
    secrets += [r["result_hash"] for r in rows(src, "SELECT result_hash FROM submissions")]
    assert len(secrets) > 20
    for s in secrets:
        assert s.encode() not in raw, s
    copy = tmp_path / "copy.sqlite3"
    assert rows(copy, "SELECT COUNT(*) AS n FROM enrollments")[0]["n"] == 0
    assert rows(copy, "SELECT COUNT(*) AS n FROM events WHERE detail IS NOT NULL")[0]["n"] == 0
    assert {r["key_hash"] for r in rows(copy, "SELECT key_hash FROM workers")} == {None}


@pytest.mark.usefixtures("pinned")
def test_owner_labels_and_payload_hashes_keep_their_shape(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    enroll(client, "wk-d", owner="owner-wk-a")  # same person as wk-a
    ids = busy_period(client)
    clock.t += 200
    export_copy(ledger.path, tmp_path / "copy.sqlite3", UNTIL, int(clock.t))
    owners = {r["worker_id"]: r["owner"]
              for r in rows(tmp_path / "copy.sqlite3", "SELECT worker_id, owner FROM workers")}
    assert owners["wk-a"] == owners["wk-d"] and owners["wk-a"] != owners["wk-b"]
    assert set(owners.values()) == {"owner-1", "owner-2", "owner-3"}
    hashes = {r["job_id"]: r["payload_hash"]
              for r in rows(tmp_path / "copy.sqlite3", "SELECT job_id, payload_hash FROM jobs")}
    live = {r["job_id"]: r["payload_hash"]
            for r in rows(ledger.path, "SELECT job_id, payload_hash FROM jobs")}
    # A re-run still visibly has the same input as its original, but the value can't be
    # matched against live jobs (fresh random key per copy).
    assert hashes[ids["llm"]] == hashes[ids["audit"]] == hashes[ids["tiebreak"]]
    assert hashes[ids["llm"]] != live[ids["llm"]]
    export_copy(ledger.path, tmp_path / "copy2.sqlite3", UNTIL, int(clock.t))
    again = rows(tmp_path / "copy2.sqlite3", "SELECT payload_hash FROM jobs WHERE job_id=?",
                 (ids["llm"],))[0]["payload_hash"]
    assert again != hashes[ids["llm"]]


def test_open_period_is_refused(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    sha_job(client, "wk-a")
    with pytest.raises(CopyError, match="still open"):
        export_copy(ledger.path, tmp_path / "copy.sqlite3", UNTIL, int(clock.t))
    assert not list(tmp_path.glob("copy.sqlite3*"))


@pytest.mark.usefixtures("pinned")
def test_unfinished_jobs_are_refused(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    sha_job(client, "wk-a")
    client.post("/v1/jobs", json={"payload": "never picked up"})
    clock.t += 200
    with pytest.raises(CopyError, match="still queued or running"):
        export_copy(ledger.path, tmp_path / "copy.sqlite3", UNTIL, int(clock.t))
    assert not list(tmp_path.glob("copy.sqlite3*"))
    # A copy for a period that ended before that job was created is fine.
    out = export_copy(ledger.path, tmp_path / "early.sqlite3", T0, int(clock.t))
    assert out["jobs"] == 0


@pytest.mark.usefixtures("pinned")
def test_later_jobs_are_left_out_but_late_rechecks_stay(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    assert client.post("/v1/jobs", json={"task_type": "llm:generate",
                                         "payload": llm_payload(PROMPT)}).status_code == 200
    original = claim(client, "wk-a").json()["job_id"]
    submit(client, original, "wk-a", ANSWER)          # accepted at T0, re-check pending
    clock.t = UNTIL                                   # the period [0, UNTIL) is now over
    audit = claim(client, "wk-b").json()["job_id"]
    submit(client, audit, "wk-b", WRONG)              # dispute -> tie-breaker made after UNTIL
    tiebreak = claim(client, "wk-c").json()["job_id"]
    submit(client, tiebreak, "wk-c", ANSWER)
    later = sha_job(client, "wk-a")                   # created after UNTIL
    clock.t = T0 + 300
    rep = signed_report(client)
    export_copy(ledger.path, tmp_path / "copy.sqlite3", UNTIL, int(clock.t))
    kept = {r["job_id"] for r in rows(tmp_path / "copy.sqlite3", "SELECT job_id FROM jobs")}
    assert kept == {original, audit, tiebreak} and later not in kept
    assert not rows(tmp_path / "copy.sqlite3", "SELECT * FROM events WHERE at >= ?", (UNTIL,))
    copy = Ledger(tmp_path / "copy.sqlite3", read_only=True)
    assert rewards.verify_report(rep, copy, client.app.state.signer.public_key_hex) == []
    # A report reaching past the copy's end would be built from missing data: refused.
    with pytest.raises(rewards.ReportError, match="only covers"):
        rewards.build_report(copy, 0, T0 + 300)


@pytest.mark.usefixtures("pinned")
def test_edited_copy_is_caught(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    ids = busy_period(client)
    clock.t += 200
    rep = signed_report(client)
    path = tmp_path / "copy.sqlite3"
    export_copy(ledger.path, path, UNTIL, int(clock.t))
    with closing(sqlite3.connect(path)) as db:  # someone edits wk-b's held result to confirmed
        db.execute("UPDATE submissions SET audit_status='confirmed' WHERE job_id=?",
                   (ids["audit"],))
        db.commit()
    problems = rewards.verify_report(rep, Ledger(path, read_only=True))
    assert "ledger gives a different report for this period" in problems
    assert f"entry differs: {ids['audit']}" in problems
    # The relational checks still work on a copy: a faked self-audit is flagged.
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE submissions SET worker_id='wk-a' WHERE job_id=?", (ids["audit"],))
        db.commit()
    assert any("audited its own result" in p
               for p in Ledger(path, read_only=True).check_consistency())


def test_ledger_with_problems_is_not_shared(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    job = sha_job(client, "wk-a")
    with closing(sqlite3.connect(ledger.path)) as db:
        db.execute("UPDATE submissions SET reward_points=999 WHERE job_id=?", (job,))
        db.commit()
    clock.t += 200
    with pytest.raises(CopyError, match="consistency"):
        export_copy(ledger.path, tmp_path / "copy.sqlite3", UNTIL, int(clock.t))


def test_existing_file_is_never_overwritten(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    clock.t += 200
    (tmp_path / "copy.sqlite3").write_text("keep me")
    with pytest.raises(CopyError, match="already exists"):
        export_copy(ledger.path, tmp_path / "copy.sqlite3", UNTIL, int(clock.t))
    with pytest.raises(CopyError, match="different file"):
        export_copy(ledger.path, ledger.path, UNTIL, int(clock.t))
    assert (tmp_path / "copy.sqlite3").read_text() == "keep me"


@pytest.mark.usefixtures("pinned")
def test_cli_export_info_and_verify_leave_the_copy_unchanged(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client, clock, ledger = make(tmp_path)
    busy_period(client)
    clock.t += 200
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(signed_report(client)))
    monkeypatch.setattr(ledger_copy.time, "time", lambda: float(T0 + 1000))
    copy = tmp_path / "copy.sqlite3"
    assert ledger_copy.main(["export", "--db", str(ledger.path), "--until", str(UNTIL),
                             "--out", str(copy)]) == 0
    printed = capsys.readouterr().out
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    assert f"sha256={digest}" in printed
    key = client.app.state.signer.public_key_hex
    assert rewards.main(["verify", str(report_path), "--db", str(copy),
                         "--public-key", key]) == 0
    assert "OK: report matches the ledger" in capsys.readouterr().out
    assert ledger_copy.main(["info", str(copy)]) == 0
    assert f"until={UNTIL}" in capsys.readouterr().out
    assert ledger_copy.main(["info", str(ledger.path)]) == 1
    # Checking never changes a byte, so the published sha256 stays valid.
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == digest
    assert sorted(p.name for p in tmp_path.glob("copy.sqlite3*")) == ["copy.sqlite3"]
    # Asking for a period that isn't over yet is refused by the CLI too.
    assert ledger_copy.main(["export", "--db", str(ledger.path), "--until", str(T0 + 5000),
                             "--out", str(tmp_path / "x.sqlite3")]) == 2
