"""Tests for app.vuln_check (`vuln-check-v1`): known security problems in locked packages."""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app import deps_check as dc
from app import vuln_check as vc

H = "sha256:" + "a" * 64
TODAY = dt.date(2026, 11, 24)


def lock(**pkgs: str) -> str:
    return "\n".join(f"{n}=={v} \\\n    --hash={H}" for n, v in pkgs.items()) + "\n"


def rec(vid, aliases=(), fixed=("2.0",), details="Bad thing.", withdrawn=None, summary=None):
    return {"id": vid, "aliases": list(aliases), "fixed_in": list(fixed), "details": details,
            "summary": summary, "link": f"https://osv.dev/vulnerability/{vid}", "source": "osv",
            "withdrawn": withdrawn}


def answer(name, version, vulns=()):
    return {"info": {"name": name, "version": version}, "vulnerabilities": list(vulns)}


def fake_fetch(table: dict):
    calls = []

    def fetch(name, version):
        calls.append((name, version))
        v = table.get((name, version))
        if isinstance(v, Exception):
            raise v
        if v is None:
            return answer(name, version)
        return v
    fetch.calls = calls
    return fetch


def exc(package="badpkg", vid="GHSA-aaaa", reason="Only used by the test runner on a one-user CI machine.",
        reviewed="2026-11-20", expires="2027-01-20"):
    return {"package": package, "id": vid, "reason": reason, "reviewed": reviewed, "expires": expires}


def exfile(*entries):
    return {"schema": vc.EXCEPTIONS_SCHEMA, "exceptions": list(entries)}


def by(result, status, name=None):
    return [c["detail"] for c in result["checks"] if c["status"] == status and (name is None or c["name"] == name)]


# ------------------------------------------------------------------ basic results

def test_clean_lock_passes_and_every_package_is_asked_about():
    f = fake_fetch({})
    r = vc.check_vulns(lock(alpha="1.0", beta="2.0"), exfile(), f, TODAY)
    assert r["ok"] and r["schema"] == vc.SCHEMA and r["rule"] == vc.RULE
    assert f.calls == [("alpha", "1.0"), ("beta", "2.0")]
    assert r["packages"] == r["packages_checked"] == 2 and r["findings"] == []
    assert r["checked_on"] == "2026-11-24"


def test_windows_only_packages_are_checked_too():
    text = f"colorama==0.4.6 ; sys_platform == 'win32' \\\n    --hash={H}\n"
    f = fake_fetch({})
    assert vc.check_vulns(text, exfile(), f, TODAY)["ok"]
    assert f.calls == [("colorama", "0.4.6")]


def test_known_problem_fails_with_fix_version_and_cve():
    f = fake_fetch({("badpkg", "1.0"): answer("badpkg", "1.0", [rec("GHSA-aaaa", ["CVE-2026-1"], ["1.5"])])})
    r = vc.check_vulns(lock(badpkg="1.0", good="1.0"), exfile(), f, TODAY)
    assert not r["ok"]
    [d] = by(r, "fail", "vulns")
    assert "badpkg==1.0" in d and "GHSA-aaaa" in d and "CVE-2026-1" in d and "fixed in 1.5" in d
    assert r["findings"][0]["package"] == "badpkg" and r["findings"][0]["accepted"] is None
    assert by(r, "pass", "vulns") == ["good==1.0: no known problems"]


def test_same_problem_under_two_ids_is_reported_once():
    vulns = [rec("GHSA-aaaa", ["CVE-2026-1", "PYSEC-1"]), rec("PYSEC-1", ["CVE-2026-1", "GHSA-aaaa"])]
    f = fake_fetch({("badpkg", "1.0"): answer("badpkg", "1.0", vulns)})
    r = vc.check_vulns(lock(badpkg="1.0"), exfile(), f, TODAY)
    assert len(r["findings"]) == 1 and r["findings"][0]["ids"] == ["GHSA-aaaa", "PYSEC-1"]
    assert len(by(r, "fail", "vulns")) == 1


def test_chained_aliases_merge_and_different_problems_stay_apart():
    vulns = [rec("A-1", ["CVE-1"]), rec("B-1", ["CVE-2"]), rec("C-1", ["CVE-1", "CVE-2"]), rec("D-1", ["CVE-9"])]
    groups, withdrawn = vc.group_vulns(vulns)
    assert withdrawn == 0
    assert [g["ids"] for g in groups] == [["A-1", "B-1", "C-1"], ["D-1"]]


def test_withdrawn_reports_are_ignored_with_a_note():
    f = fake_fetch({("p", "1"): answer("p", "1", [rec("GHSA-x", withdrawn="2026-01-01T00:00:00Z")])})
    r = vc.check_vulns(lock(p="1"), exfile(), f, TODAY)
    assert r["ok"] and any("withdrawn" in d for d in by(r, "note"))


def test_no_fixed_version_is_said_plainly():
    f = fake_fetch({("p", "1"): answer("p", "1", [rec("GHSA-x", fixed=())])})
    [d] = by(vc.check_vulns(lock(p="1"), exfile(), f, TODAY), "fail")
    assert "no fixed version listed yet" in d


def test_long_descriptions_are_cut_short():
    f = fake_fetch({("p", "1"): answer("p", "1", [rec("GHSA-x", details="word " * 200)])})
    r = vc.check_vulns(lock(p="1"), exfile(), f, TODAY)
    assert len(r["findings"][0]["summary"]) <= vc.DETAIL_CHARS


# ------------------------------------------------------------------ couldn't check

@pytest.mark.parametrize("bad", [
    vc.LookupFailed("couldn't reach PyPI"),
    {"info": {"name": "other", "version": "1.0"}, "vulnerabilities": []},
    {"info": {"name": "p", "version": "9.9"}, "vulnerabilities": []},
    {"info": {"name": "p", "version": "1.0"}},
    {"info": {"name": "p", "version": "1.0"}, "vulnerabilities": "none"},
    {"vulnerabilities": []},
])
def test_anything_short_of_a_proper_answer_fails(bad):
    f = fake_fetch({("p", "1.0"): bad})
    r = vc.check_vulns(lock(p="1.0"), exfile(), f, TODAY)
    assert not r["ok"] and by(r, "fail", "lookup") and r["packages_checked"] == 0


def test_name_spelling_differences_still_match():
    f = fake_fetch({("pyyaml", "6.0.3"): answer("PyYAML", "6.0.3")})
    assert vc.check_vulns(lock(pyyaml="6.0.3"), exfile(), f, TODAY)["ok"]


def test_empty_or_unsafe_lock_fails():
    assert not vc.check_vulns("", exfile(), fake_fetch({}), TODAY)["ok"]
    r = vc.check_vulns("--index-url https://evil.example/simple\n" + lock(p="1"), exfile(), fake_fetch({}), TODAY)
    assert not r["ok"] and by(r, "fail", "lock-option")


# ------------------------------------------------------------------ exceptions

def bad_lock_and_fetch():
    return lock(badpkg="1.0"), fake_fetch({("badpkg", "1.0"): answer("badpkg", "1.0", [rec("GHSA-aaaa", ["CVE-2026-1"])])})


@pytest.mark.parametrize("vid", ["GHSA-aaaa", "ghsa-aaaa", "CVE-2026-1"])
def test_reviewed_exception_turns_fail_into_note(vid):
    text, f = bad_lock_and_fetch()
    r = vc.check_vulns(text, exfile(exc(vid=vid)), f, TODAY)
    assert r["ok"], r["checks"]
    assert any("accepted until 2027-01-20" in d for d in by(r, "note", "vulns"))
    assert r["findings"][0]["accepted"]["expires"] == "2027-01-20"


def test_exception_for_another_package_does_not_count():
    text, f = bad_lock_and_fetch()
    r = vc.check_vulns(text, exfile(exc(package="otherpkg")), f, TODAY)
    assert not r["ok"] and any("matches nothing" in d for d in by(r, "note", "exception"))


def test_expired_exception_fails_again():
    text, f = bad_lock_and_fetch()
    r = vc.check_vulns(text, exfile(exc(reviewed="2026-09-01", expires="2026-11-24")), f, TODAY)
    assert not r["ok"]
    assert any("ended on 2026-11-24" in d for d in by(r, "fail", "exception"))
    assert by(r, "fail", "vulns")


@pytest.mark.parametrize("changes, words", [
    ({"reason": "fine"}, "reason"),
    ({"reason": ""}, "reason"),
    ({"id": ""}, "'id'"),
    ({"package": ""}, "'package'"),
    ({"expires": "soon"}, "date"),
    ({"reviewed": "2026-11-25", "expires": "2026-12-01"}, "future"),
    ({"expires": "2026-11-20"}, "at most"),
    ({"expires": "2027-03-01"}, "at most"),
])
def test_malformed_exceptions_fail_and_do_not_excuse(changes, words):
    text, f = bad_lock_and_fetch()
    r = vc.check_vulns(text, exfile({**exc(), **changes}), f, TODAY)
    assert not r["ok"]
    assert any(words in d for d in by(r, "fail", "exception")), by(r, "fail")
    assert by(r, "fail", "vulns")


def test_duplicate_exception_fails():
    text, f = bad_lock_and_fetch()
    r = vc.check_vulns(text, exfile(exc(), exc(vid="ghsa-AAAA")), f, TODAY)
    assert any("listed twice" in d for d in by(r, "fail", "exception"))


def test_non_object_exception_fails():
    text, f = bad_lock_and_fetch()
    assert any("not an object" in d for d in by(vc.check_vulns(text, exfile("GHSA-aaaa"), f, TODAY), "fail"))


def test_load_exceptions(tmp_path):
    assert vc.load_exceptions(tmp_path / "missing.json")["exceptions"] == []
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"schema": "wrong", "exceptions": []}))
    with pytest.raises(ValueError):
        vc.load_exceptions(p)
    p.write_text(json.dumps({"schema": vc.EXCEPTIONS_SCHEMA}))
    with pytest.raises(ValueError):
        vc.load_exceptions(p)


def test_repo_exceptions_file_is_valid_and_empty():
    data = vc.load_exceptions(vc.DEFAULT_EXCEPTIONS)
    assert data["exceptions"] == []


def test_repo_lock_parses_and_has_no_known_problem_from_this_session():
    # The lock must no longer carry the pytest version vuln-check-v1 caught (CVE-2025-71176).
    report = dc.Report("x")
    entries = {e.name: e.version for e in dc.parse_lock(dc.DEFAULT_LOCK.read_text(), report)}
    from packaging.version import Version
    assert Version(entries["pytest"]) >= Version("9.0.3")


# ------------------------------------------------------------------ the PyPI fetcher

def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_fetcher_asks_the_right_url_and_returns_json():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json=answer("p", "1.0"))
    f = vc.make_pypi_fetch(client_for(handler))
    assert f("p", "1.0")["info"]["version"] == "1.0"
    assert seen == ["https://pypi.org/pypi/p/1.0/json"]


def test_fetcher_quotes_odd_characters():
    seen = []

    def handler(req):
        seen.append(req.url.raw_path)
        return httpx.Response(200, json=answer("p", "1.0"))
    vc.make_pypi_fetch(client_for(handler))("p", "1.0/../../x")
    # the odd version stays one path segment under /pypi/p/, it can't climb to another URL
    assert seen[0].startswith(b"/pypi/p/1.0%2F..%2F..%2Fx/json")


def test_fetcher_retries_once_then_fails():
    n = {"calls": 0}
    sleeps = []

    def handler(req):
        n["calls"] += 1
        return httpx.Response(503)
    f = vc.make_pypi_fetch(client_for(handler), sleep=sleeps.append)
    with pytest.raises(vc.LookupFailed, match="503"):
        f("p", "1.0")
    assert n["calls"] == 2 and sleeps == [2.0]


def test_fetcher_recovers_after_a_network_error():
    n = {"calls": 0}

    def handler(req):
        n["calls"] += 1
        if n["calls"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=answer("p", "1.0"))
    assert vc.make_pypi_fetch(client_for(handler), sleep=lambda s: None)("p", "1.0")


@pytest.mark.parametrize("response, words", [
    (httpx.Response(404), "no such version"),
    (httpx.Response(403), "403"),
    (httpx.Response(200, content=b"<html>"), "wasn't JSON"),
    (httpx.Response(200, json=[1, 2]), "JSON object"),
])
def test_fetcher_refuses_bad_answers_without_retrying(response, words):
    n = {"calls": 0}

    def handler(req):
        n["calls"] += 1
        return response
    with pytest.raises(vc.LookupFailed, match=words):
        vc.make_pypi_fetch(client_for(handler), sleep=lambda s: None)("p", "1.0")
    assert n["calls"] == 1


def test_fetcher_refuses_a_redirect_to_another_site():
    def handler(req):
        if req.url.host == "pypi.org":
            return httpx.Response(302, headers={"Location": "https://evil.example/p.json"})
        return httpx.Response(200, json=answer("p", "1.0"))
    with pytest.raises(vc.LookupFailed, match="another site"):
        vc.make_pypi_fetch(client_for(handler))("p", "1.0")


# ------------------------------------------------------------------ CLI

def write_inputs(tmp_path, lock_text, exceptions=None):
    lk = tmp_path / "requirements.lock"
    lk.write_text(lock_text)
    ex = tmp_path / "vuln-exceptions.json"
    ex.write_text(json.dumps(exceptions or exfile()))
    return lk, ex


def test_cli_exit_codes_and_report(tmp_path, capsys):
    lk, ex = write_inputs(tmp_path, lock(p="1.0"))
    out = tmp_path / "r.json"
    assert vc.main(["--lock", str(lk), "--exceptions", str(ex), "--out", str(out)], fetch=fake_fetch({})) == 0
    assert json.loads(out.read_text())["schema"] == vc.SCHEMA
    assert "1 of 1 packages checked" in capsys.readouterr().out
    # never overwrites
    assert vc.main(["--lock", str(lk), "--exceptions", str(ex), "--out", str(out)], fetch=fake_fetch({})) == 2

    bad = fake_fetch({("p", "1.0"): answer("p", "1.0", [rec("GHSA-x")])})
    assert vc.main(["--lock", str(lk), "--exceptions", str(ex)], fetch=bad) == 1


def test_cli_usage_and_file_errors(tmp_path):
    lk, ex = write_inputs(tmp_path, lock(p="1.0"))
    assert vc.main(["--bogus"], fetch=fake_fetch({})) == 2
    assert vc.main(["--lock", str(tmp_path / "nope.lock"), "--exceptions", str(ex)], fetch=fake_fetch({})) == 2
    ex.write_text("{not json")
    assert vc.main(["--lock", str(lk), "--exceptions", str(ex)], fetch=fake_fetch({})) == 2
