"""Publishing signed reports and ledger copies in one public place (`reports-v1`).

The repo's `reports/` folder is served by the site at /reports/ and kept by GitHub. A
period is published only when its signed report re-checks against its redacted copy, the
period ended long enough ago, and the earlier history is unchanged. Nothing published is
ever replaced, and `check` re-verifies the whole folder.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import publish_report as pub
from app.ledger_copy import export_copy
from tests.test_audits import OP, T0, make, pinned  # noqa: F401  (pytest fixture)
from tests.test_ledger_copy import UNTIL, busy_period
from tests.test_rewards import sha_job

UNTIL2 = T0 + 400
LATER = UNTIL2 + 8 * 86400  # past the default 7-day wait for both periods


def signed(client, since: int, until: int) -> dict:
    r = client.get("/v1/rewards/report", headers=OP,
                   params={"since": since, "until": until, "sign": "true"})
    assert r.status_code == 200, r.text
    return r.json()


def two_periods(tmp_path: Path) -> dict:
    """Period 1 = [0, UNTIL), busy; period 2 = [UNTIL, UNTIL2), one more job. A signed
    report and a ledger copy for each, as the operator would make them."""
    client, clock, ledger = make(tmp_path)
    ids = busy_period(client)
    clock.t = T0 + 200
    export_copy(ledger.path, tmp_path / "copy-1.sqlite3", UNTIL, int(clock.t))
    clock.t = T0 + 250
    ids["later"] = sha_job(client, "wk-a", payload="period two")
    clock.t = T0 + 600
    export_copy(ledger.path, tmp_path / "copy-2.sqlite3", UNTIL2, int(clock.t))
    (tmp_path / "report-1.json").write_text(json.dumps(signed(client, 0, UNTIL)))
    (tmp_path / "report-2.json").write_text(json.dumps(signed(client, UNTIL, UNTIL2)))
    return {"client": client, "ledger": ledger, "ids": ids, "dir": tmp_path / "reports",
            "key": client.app.state.signer.public_key_hex,
            "r1": tmp_path / "report-1.json", "c1": tmp_path / "copy-1.sqlite3",
            "r2": tmp_path / "report-2.json", "c2": tmp_path / "copy-2.sqlite3"}


def add(s: dict, n: int, now: int = LATER, **kw):
    return pub.publish(s[f"r{n}"], s[f"c{n}"], s["dir"], kw.pop("key", s["key"]), now, **kw)


def snapshot(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.mark.usefixtures("pinned")
def test_two_periods_publish_and_check_out(tmp_path: Path) -> None:
    s = two_periods(tmp_path)
    first = add(s, 1)
    second = add(s, 2)
    d = s["dir"]
    assert first["id"] == pub.period_id(0, UNTIL) and first["previous"] is None
    assert second["previous"] == {"id": first["id"], "history_unchanged": True}
    for e, report, copy in ((first, s["r1"], s["c1"]), (second, s["r2"], s["c2"])):
        folder = d / e["id"]
        assert sorted(p.name for p in folder.iterdir()) == [
            "SHA256SUMS", "ledger-copy.sqlite3", "report.json"]
        # Byte-for-byte the files that were checked.
        assert (folder / "report.json").read_bytes() == report.read_bytes()
        assert (folder / "ledger-copy.sqlite3").read_bytes() == copy.read_bytes()
        assert e["signed_by"]["public_key"] == s["key"]
    index = json.loads((d / "index.json").read_text())
    assert index["format"] == "bnh-reports-index/1"
    assert [p["id"] for p in index["periods"]] == [first["id"], second["id"]]
    assert index["coordinator"]["public_key"] == s["key"]
    assert "Simulated" in index["notice"]
    assert second["accepted_results"] == 1 and second["rigs"] == 1
    # Nothing half-written left behind, and the full re-check passes, with and without a key.
    assert not [p for p in d.iterdir() if p.name.startswith(".")]
    assert pub.check_folder(d, s["key"]) == []
    assert pub.check_folder(d) == []
    before = snapshot(d)
    pub.check_folder(d, s["key"])
    assert snapshot(d) == before  # checking changes nothing (no journal files either)


@pytest.mark.usefixtures("pinned")
def test_the_live_ledger_is_never_published(tmp_path: Path) -> None:
    s = two_periods(tmp_path)
    with pytest.raises(pub.PublishError, match="full ledger, not a redacted copy"):
        pub.publish(s["r1"], s["ledger"].path, s["dir"], s["key"], LATER)
    assert not s["dir"].exists()


@pytest.mark.usefixtures("pinned")
def test_report_must_be_signed_by_the_key_you_trust(tmp_path: Path) -> None:
    s = two_periods(tmp_path)
    unsigned = tmp_path / "unsigned.json"
    report = json.loads(s["r1"].read_text())
    unsigned.write_text(json.dumps({k: v for k, v in report.items() if k != "signature"}))
    with pytest.raises(pub.PublishError, match="not signed"):
        pub.publish(unsigned, s["c1"], s["dir"], s["key"], LATER)
    other = "11" * 32
    with pytest.raises(pub.PublishError, match="signed by a different key|not a valid"):
        add(s, 1, key=other)
    with pytest.raises(pub.PublishError, match="public key"):
        add(s, 1, key="")
    report["workers"][0]["payable_points"] += 100  # edited after signing
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(report))
    with pytest.raises(pub.PublishError, match="was edited"):
        pub.publish(edited, s["c1"], s["dir"], s["key"], LATER)
    assert not s["dir"].exists()


@pytest.mark.usefixtures("pinned")
def test_the_copy_must_match_the_report(tmp_path: Path) -> None:
    s = two_periods(tmp_path)
    with pytest.raises(pub.PublishError, match="copy ends at"):
        pub.publish(s["r1"], s["c2"], s["dir"], s["key"], LATER)
    # Same period, but the copy says something different about it.
    with closing(sqlite3.connect(s["c1"])) as db:
        db.execute("UPDATE submissions SET reward_points=reward_points+5 WHERE job_id=?",
                   (s["ids"]["exact"],))
        db.execute("UPDATE jobs SET reward_points=reward_points+5 WHERE job_id=?",
                   (s["ids"]["exact"],))
        db.commit()
    with pytest.raises(pub.PublishError, match="does not match the copy"):
        add(s, 1)


@pytest.mark.usefixtures("pinned")
def test_waits_after_the_period_ends(tmp_path: Path) -> None:
    s = two_periods(tmp_path)
    too_soon = UNTIL + 7 * 86400 - 1
    with pytest.raises(pub.PublishError, match="publish on or after"):
        add(s, 1, now=too_soon)
    assert add(s, 1, now=too_soon + 1)["id"]
    # The owner can choose another wait.
    assert add(s, 2, now=UNTIL2 + 3600, min_wait_hours=1)["id"]


@pytest.mark.usefixtures("pinned")
def test_published_periods_are_never_replaced(tmp_path: Path) -> None:
    s = two_periods(tmp_path)
    add(s, 1)
    before = snapshot(s["dir"])
    with pytest.raises(pub.PublishError, match="already published"):
        add(s, 1)
    assert snapshot(s["dir"]) == before


@pytest.mark.usefixtures("pinned")
def test_periods_go_forward_only(tmp_path: Path) -> None:
    s = two_periods(tmp_path)
    add(s, 1)
    # Starts 10 seconds before period 1 ended: the overlap would count those results twice.
    overlap = tmp_path / "overlap.json"
    overlap.write_text(json.dumps(signed(s["client"], UNTIL - 10, UNTIL2)))
    with pytest.raises(pub.PublishError, match="can't overlap or go back"):
        pub.publish(overlap, s["c2"], s["dir"], s["key"], LATER)
    # And an earlier period can't be added after a later one.
    other = tmp_path / "other"
    pub.publish(s["r2"], s["c2"], other, s["key"], LATER)
    with pytest.raises(pub.PublishError, match="can't overlap or go back"):
        pub.publish(s["r1"], s["c1"], other, s["key"], LATER)


@pytest.mark.usefixtures("pinned")
def test_a_copy_that_rewrites_published_history_is_refused(tmp_path: Path) -> None:
    """The operator turns wk-b's outvoted result from period 1 into a confirmed one in the
    period-2 copy. Period 2's own report doesn't cover it, so only the comparison sees it."""
    s = two_periods(tmp_path)
    add(s, 1)
    with closing(sqlite3.connect(s["c2"])) as db:
        db.execute("UPDATE submissions SET audit_status='confirmed' WHERE job_id=?",
                   (s["ids"]["audit"],))
        db.commit()
    with pytest.raises(pub.PublishError, match="changes history already published"):
        add(s, 2)


@pytest.mark.usefixtures("pinned")
def test_check_catches_tampering(tmp_path: Path) -> None:
    s = two_periods(tmp_path)
    first, second = add(s, 1), add(s, 2)
    d = s["dir"]
    report = d / first["id"] / "report.json"
    original = report.read_bytes()
    report.write_bytes(original.replace(b'"payable_points": ', b'"payable_points": 9', 1))
    problems = pub.check_folder(d, s["key"])
    assert f"{first['id']}: report.json doesn't match its sha256 in the index" in problems
    report.write_bytes(original)
    assert pub.check_folder(d, s["key"]) == []
    # A deleted copy, a stray folder, an edited checksum file, an untrusted key.
    (d / second["id"] / "ledger-copy.sqlite3").unlink()
    (d / "2099-01-01T0000Z_2099-01-08T0000Z").mkdir()
    (d / first["id"] / "SHA256SUMS").write_text("nope\n")
    problems = pub.check_folder(d, "22" * 32)
    assert f"{second['id']}: missing ledger-copy.sqlite3" in problems
    assert "folder 2099-01-01T0000Z_2099-01-08T0000Z is not listed in index.json" in problems
    assert f"{first['id']}: SHA256SUMS is missing or doesn't match the index" in problems
    assert any(p.startswith(f"{first['id']}: signed by ") for p in problems)
    assert pub.check_folder(tmp_path / "nowhere") == ["index.json is missing"]


@pytest.mark.usefixtures("pinned")
def test_cli_dry_run_writes_nothing_and_check_reports(tmp_path: Path, capsys,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    s = two_periods(tmp_path)
    monkeypatch.setattr(pub.time, "time", lambda: float(LATER))  # the CLI reads the clock
    d = s["dir"]
    d.mkdir()
    (d / "index.json").write_text(json.dumps(pub.empty_index()))
    args = [str(s["r1"]), str(s["c1"]), "--dir", str(d), "--public-key", s["key"]]
    assert pub.main(["add", *args, "--dry-run"]) == 0
    assert "would publish" in capsys.readouterr().out
    assert sorted(p.name for p in d.iterdir()) == ["index.json"]
    assert pub.main(["check", "--dir", str(d)]) == 0
    assert "OK: 0 published period(s)" in capsys.readouterr().out
    assert pub.main(["add", *args]) == 0
    assert "open a pull request" in capsys.readouterr().out
    assert pub.main(["check", "--dir", str(d), "--public-key", s["key"]]) == 0
    assert pub.main(["add", str(s["r1"]), str(s["ledger"].path), "--dir", str(d),
                     "--public-key", s["key"]]) == 2
    assert "full ledger" in capsys.readouterr().err


def test_the_repo_reports_folder_checks_out() -> None:
    """The real folder the site serves. CI runs this, so a broken publish fails the build."""
    assert pub.DEFAULT_DIR.name == "reports"
    assert pub.check_folder(pub.DEFAULT_DIR) == []
