"""Tests for app.deps_check (`deps-check-v1`): pinned, hashed dependencies with reviewed licenses."""
from __future__ import annotations

import json
from email.message import Message
from pathlib import Path

import pytest

from app import deps_check as dc

H1 = "sha256:" + "a" * 64
H2 = "sha256:" + "b" * 64


def lock(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def pkg(name: str, version: str, *, marker: str | None = None, hashes=(H1,)) -> str:
    head = f"{name}=={version}" + (f" ; {marker}" if marker else "")
    return " \\\n".join([head] + [f"    --hash={h}" for h in hashes])


def licenses(**pkgs) -> dict:
    return {
        "schema": dc.LICENSES_SCHEMA,
        "allowed": ["MIT", "BSD-3-Clause", "Apache-2.0", "BSD-2-Clause", "MPL-2.0"],
        "packages": {n: {"version": v, "license": lic, "why": "test"} for n, (v, lic) in pkgs.items()},
    }


def fails(result: dict) -> list[str]:
    return [c["detail"] for c in result["checks"] if c["status"] == "fail"]


def statuses(result: dict, name: str) -> list[str]:
    return [c["status"] for c in result["checks"] if c["name"] == name]


class FakeDist:
    def __init__(self, name: str, version: str, *, expression=None, license=None, classifiers=()):
        self.version = version
        md = Message()
        md["Name"] = name
        md["Version"] = version
        if expression:
            md["License-Expression"] = expression
        if license:
            md["License"] = license
        for c in classifiers:
            md["Classifier"] = c
        self.metadata = md


# ------------------------------------------------------------------ the real repo files

def test_repo_lock_requirements_and_licenses_agree():
    result = dc.check_lock(dc.DEFAULT_LOCK.read_text(), dc.DEFAULT_REQUIREMENTS.read_text(),
                           dc.load_licenses(dc.DEFAULT_LICENSES))
    assert result["ok"], fails(result)
    assert result["schema"] == "bnh-deps-check/1"
    # Every direct requirement was checked against the lock.
    assert len(statuses(result, "requirements")) == 9


def test_repo_lock_has_no_unpinned_or_unhashed_package():
    report = dc.Report("lock")
    entries = dc.parse_lock(dc.DEFAULT_LOCK.read_text(), report)
    assert not report.checks
    assert len(entries) >= 20
    assert all(e.hashes and e.version for e in entries)


def test_repo_licenses_are_all_on_the_allowed_list():
    data = dc.load_licenses(dc.DEFAULT_LICENSES)
    for name, info in data["packages"].items():
        ok, reason = dc.license_allowed(info["license"], data["allowed"])
        assert ok, (name, reason)


# ------------------------------------------------------------------ lock parsing

def test_good_lock_with_comments_markers_and_continuations():
    text = lock("# header", "", pkg("Foo_Bar", "1.2.3", hashes=(H1, H2)), "    # via baz",
                pkg("win-only", "0.4.6", marker="sys_platform == 'win32'"))
    report = dc.Report("lock")
    entries = dc.parse_lock(text, report)
    assert report.checks == []
    assert [(e.name, e.version) for e in entries] == [("foo-bar", "1.2.3"), ("win-only", "0.4.6")]
    assert entries[0].hashes == [H1, H2]
    assert entries[1].marker == 'sys_platform == "win32"'


@pytest.mark.parametrize("line, what", [
    ("foo>=1.0 --hash=" + H1, "exact version"),
    ("foo --hash=" + H1, "exact version"),
    ("foo==1.* --hash=" + H1, "exact version"),
    ("foo~=1.0 --hash=" + H1, "exact version"),
    ("foo==1.0", "no --hash"),
    ("foo==1.0 --hash=md5:" + "a" * 32, "sha256"),
    ("foo==1.0 --hash=sha256:" + "a" * 63, "sha256"),
    ("foo==1.0 --hash=sha256:" + "A" * 64, "sha256"),
    ("foo @ https://example.test/foo-1.0.tar.gz --hash=" + H1, "URL"),
    ("--index-url https://evil.example/simple", "not allowed"),
    ("--extra-index-url https://evil.example/simple", "not allowed"),
    ("-i https://evil.example/simple", "not allowed"),
    ("--find-links ./wheels", "not allowed"),
    ("--trusted-host evil.example", "not allowed"),
    ("-e ./local-package", "not allowed"),
    ("-r other.txt", "not allowed"),
    ("foo==1.0 --hash=" + H1 + " --no-binary :all:", "unexpected option"),
])
def test_unsafe_or_loose_lock_lines_fail(line, what):
    report = dc.Report("lock")
    entries = dc.parse_lock(lock(line), report)
    assert entries == []
    assert [c["status"] for c in report.checks] == ["fail"]
    assert what in report.checks[0]["detail"]


def test_duplicate_package_fails_even_with_different_spelling():
    report = dc.Report("lock")
    entries = dc.parse_lock(lock(pkg("foo-bar", "1.0"), pkg("Foo.Bar", "1.0")), report)
    assert len(entries) == 1
    assert "listed twice" in report.checks[0]["detail"]


def test_hash_containing_hash_sign_is_not_treated_as_comment():
    # A '#' only starts a comment at the start of a line or after whitespace.
    report = dc.Report("lock")
    entries = dc.parse_lock(lock(pkg("foo", "1.0") + "  # via bar"), report)
    assert report.checks == [] and entries[0].hashes == [H1]


# ------------------------------------------------------------------ lock vs requirements vs licenses

def test_requirement_missing_from_lock_or_outside_range_fails():
    text = lock(pkg("foo", "2.0"))
    lic = licenses(foo=("2.0", "MIT"))
    assert dc.check_lock(text, "foo>=1,<3\n", lic)["ok"]
    out = dc.check_lock(text, "foo>=1,<2\n", lic)
    assert "outside" in " ".join(fails(out))
    out = dc.check_lock(text, "foo\nbar>=1\n", lic)
    assert any("bar is in requirements.txt but not in the lock" in f for f in fails(out))


def test_requirement_extras_and_name_spelling_match_the_lock():
    text = lock(pkg("uvicorn", "0.5"), pkg("python-dotenv", "1.0"))
    lic = licenses(uvicorn=("0.5", "BSD-3-Clause"), python_dotenv=("1.0", "BSD-3-Clause"))
    assert dc.check_lock(text, "uvicorn[standard]>=0.1,<1\nPython_Dotenv>=1\n", lic)["ok"]


def test_options_in_requirements_txt_fail():
    lic = licenses(foo=("1.0", "MIT"))
    out = dc.check_lock(lock(pkg("foo", "1.0")), "--index-url https://evil.example\nfoo\n", lic)
    assert any("option" in f for f in fails(out))


def test_new_package_without_license_review_fails():
    out = dc.check_lock(lock(pkg("foo", "1.0"), pkg("newdep", "0.1")), "foo\n", licenses(foo=("1.0", "MIT")))
    assert fails(out) == ["newdep==0.1 has no licenses.json entry; read its license and add it (with why it's needed)"]


def test_version_bump_needs_license_re_review():
    out = dc.check_lock(lock(pkg("foo", "1.1")), "foo\n", licenses(foo=("1.0", "MIT")))
    assert "re-check its license" in fails(out)[0]


def test_disallowed_license_fails_and_stale_entry_fails():
    out = dc.check_lock(lock(pkg("foo", "1.0")), "foo\n",
                        licenses(foo=("1.0", "GPL-3.0-only"), gone=("2.0", "MIT")))
    joined = " ".join(fails(out))
    assert "GPL-3.0-only is refused" in joined
    assert "lists gone, which is not in the lock" in joined


def test_missing_reason_fails():
    lic = licenses(foo=("1.0", "MIT"))
    lic["packages"]["foo"]["why"] = "  "
    assert "doesn't say why" in fails(dc.check_lock(lock(pkg("foo", "1.0")), "foo\n", lic))[0]


def test_empty_lock_fails():
    assert not dc.check_lock("# nothing\n", "", licenses())["ok"]


@pytest.mark.parametrize("expr, ok", [
    ("MIT", True),
    ("mit", True),
    ("Apache-2.0 OR BSD-3-Clause", True),
    ("GPL-3.0-only OR MIT", True),
    ("MIT AND GPL-3.0-only", False),
    ("MIT AND BSD-3-Clause", True),
    ("(GPL-2.0-only OR MIT) AND Apache-2.0", True),
    ("GPL-2.0-only OR (MIT AND LGPL-3.0-only)", False),
    ("GPL-3.0-only", False),
    ("Apache-2.0 WITH LLVM-exception", False),
    ("", False),
    ("MIT OR", False),
    ("(MIT", False),
    ("MIT BSD-3-Clause", False),
])
def test_license_expressions(expr, ok):
    assert dc.license_allowed(expr, ["MIT", "BSD-3-Clause", "Apache-2.0"])[0] is ok


def test_licenses_file_needs_the_right_schema(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"schema": "something-else", "allowed": [], "packages": {}}))
    with pytest.raises(ValueError):
        dc.load_licenses(p)
    p.write_text(json.dumps({"schema": dc.LICENSES_SCHEMA, "allowed": "MIT", "packages": {}}))
    with pytest.raises(ValueError):
        dc.load_licenses(p)


# ------------------------------------------------------------------ installed environment

LOCK2 = lock(pkg("foo", "1.0"), pkg("bar", "2.0"), pkg("winonly", "3.0", marker="sys_platform == 'win32'"))
LIC2 = licenses(foo=("1.0", "MIT"), bar=("2.0", "Apache-2.0 OR BSD-3-Clause"), winonly=("3.0", "MIT"))
LINUX = {"sys_platform": "linux"}


def test_installed_matches_lock():
    dists = [FakeDist("Foo", "1.0", expression="MIT"),
             FakeDist("bar", "2.0", expression="Apache-2.0 OR BSD-3-Clause"),
             FakeDist("pip", "24.0", license="MIT")]
    out = dc.check_installed(LOCK2, LIC2, dists, env=LINUX)
    assert out["ok"], fails(out)
    assert out["counts"] == {"pass": 4, "note": 1}  # winonly skipped; pip ignored


def test_installed_wrong_version_or_missing_fails():
    out = dc.check_installed(LOCK2, LIC2, [FakeDist("foo", "1.1", expression="MIT")], env=LINUX)
    joined = " ".join(fails(out))
    assert "foo: installed 1.1, the lock says 1.0" in joined
    assert "bar==2.0 is in the lock but not installed" in joined


def test_marker_for_this_platform_is_required():
    dists = [FakeDist("foo", "1.0", expression="MIT"), FakeDist("bar", "2.0", expression="Apache-2.0 OR BSD-3-Clause")]
    out = dc.check_installed(LOCK2, LIC2, dists, env={"sys_platform": "win32"})
    assert "winonly==3.0 is in the lock but not installed" in " ".join(fails(out))


def test_extra_installed_package_is_a_note():
    dists = [FakeDist("foo", "1.0", expression="MIT"), FakeDist("bar", "2.0", expression="Apache-2.0 OR BSD-3-Clause"),
             FakeDist("sneaky", "9.9", license="MIT"), FakeDist("setuptools", "79.0")]
    out = dc.check_installed(LOCK2, LIC2, dists, env=LINUX)
    assert out["ok"]
    notes = [c["detail"] for c in out["checks"] if c["name"] == "not-locked"]
    assert notes == ["sneaky==9.9 is installed but not in the lock"]


@pytest.mark.parametrize("dist_kwargs, reviewed, status", [
    ({"expression": "MIT"}, "MIT", "pass"),
    ({"expression": "BSD-3-Clause"}, "MIT", "fail"),
    ({"expression": "apache-2.0 or bsd-3-clause"}, "Apache-2.0 OR BSD-3-Clause", "pass"),
    ({"expression": "Apache-2.0"}, "Apache-2.0 OR BSD-3-Clause", "fail"),
    ({"license": "MIT License"}, "MIT", "pass"),
    ({"license": "MPL-2.0"}, "MPL-2.0", "pass"),
    ({"license": "GPL v3"}, "MIT", "note"),
    ({"classifiers": ["License :: OSI Approved :: MIT License"]}, "MIT", "pass"),
    ({"classifiers": ["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"]}, "MIT", "note"),
    ({"classifiers": ["License :: OSI Approved :: BSD License"]}, "BSD-3-Clause", "note"),
    ({"classifiers": ["License :: OSI Approved :: BSD License"]}, "MIT", "fail"),
    ({"license": "MIT License", "classifiers": ["License :: OSI Approved :: Apache Software License",
                                               "License :: OSI Approved :: MIT License"]},
     "MIT OR Apache-2.0", "pass"),
    ({"license": "MIT License"}, "MIT OR Apache-2.0", "pass"),
    ({"license": "MIT License"}, "Apache-2.0", "fail"),
    ({"license": "MIT License"}, "MIT AND Apache-2.0", "fail"),
    ({"license": "Copyright (c) someone\nPermission is hereby granted..."}, "MIT", "note"),
    ({}, "MIT", "note"),
])
def test_license_metadata_comparison(dist_kwargs, reviewed, status):
    d = FakeDist("x", "1", **dist_kwargs)
    assert dc.compare_license(reviewed, d.metadata)[0] == status


def test_license_metadata_mismatch_fails_installed_check():
    dists = [FakeDist("foo", "1.0", expression="GPL-3.0-only"),
             FakeDist("bar", "2.0", expression="Apache-2.0 OR BSD-3-Clause")]
    out = dc.check_installed(LOCK2, LIC2, dists, env=LINUX)
    assert fails(out) == ["foo: metadata says 'GPL-3.0-only' but licenses.json says 'MIT'"]


# ------------------------------------------------------------------ CLI

def _write_set(tmp_path: Path, lock_text: str, req: str, lic: dict) -> list[str]:
    (tmp_path / "requirements.lock").write_text(lock_text)
    (tmp_path / "requirements.txt").write_text(req)
    (tmp_path / "licenses.json").write_text(json.dumps(lic))
    return ["--lock", str(tmp_path / "requirements.lock"), "--licenses", str(tmp_path / "licenses.json")]


def test_cli_lock_exit_codes_and_report(tmp_path, capsys):
    args = _write_set(tmp_path, lock(pkg("foo", "1.0")), "foo\n", licenses(foo=("1.0", "MIT")))
    req = ["--requirements", str(tmp_path / "requirements.txt")]
    out = tmp_path / "r.json"
    assert dc.main(["lock", *args, *req, "--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["schema"] == "bnh-deps-check/1" and data["ok"] and data["command"] == "lock"
    # Never over an existing file.
    assert dc.main(["lock", *args, *req, "--out", str(out)]) == 2
    assert "already exists" in capsys.readouterr().err
    # A failing check exits 1.
    (tmp_path / "requirements.lock").write_text(lock("foo==1.0"))
    assert dc.main(["lock", *args, *req]) == 1


def test_cli_missing_files_and_bad_usage(tmp_path):
    assert dc.main(["lock", "--lock", str(tmp_path / "nope.lock")]) == 2
    args = _write_set(tmp_path, lock(pkg("foo", "1.0")), "foo\n", licenses(foo=("1.0", "MIT")))
    (tmp_path / "licenses.json").write_text("{not json")
    assert dc.main(["lock", *args, "--requirements", str(tmp_path / "requirements.txt")]) == 2
    assert dc.main([]) == 2
    assert dc.main(["frobnicate"]) == 2


def test_cli_installed_runs_on_this_environment(tmp_path):
    # Only checks the command runs end to end and writes its report; whether this
    # environment matches the real lock depends on how it was installed (CI installs the lock).
    out = tmp_path / "i.json"
    code = dc.main(["installed", "--out", str(out)])
    assert code in (0, 1)
    assert json.loads(out.read_text())["command"] == "installed"
