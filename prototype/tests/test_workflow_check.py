"""Tests for app.workflow_check (`actions-pin-v1`): actions pinned to commits, read-only tokens."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import workflow_check as wc

CO = "a" * 40
SP = "b" * 40


def wf(steps: str, *, on: str = "on:\n  push:\n", perms: str = "permissions:\n  contents: read\n",
       job_extra: str = "") -> str:
    return (f"name: t\n{on}{perms}jobs:\n  test:\n    runs-on: ubuntu-latest\n{job_extra}"
            f"    steps:\n{steps}")


GOOD_STEPS = (
    f"      - uses: actions/checkout@{CO} # v4.2.2\n"
    "        with:\n"
    "          persist-credentials: false\n"
    f"      - uses: actions/setup-python@{SP} # v5\n"
    "        with:\n"
    "          python-version: '3.11'\n"
    "      - name: Test\n"
    "        env:\n"
    "          URL: ${{ inputs.url }}\n"
    "        run: pytest -q --url \"$URL\"\n"
)


def check(**files: str) -> dict:
    return wc.check_workflows({f"{k}.yml": v for k, v in files.items()})


def fails(result: dict, name: str | None = None) -> list[str]:
    return [c["detail"] for c in result["checks"] if c["status"] == "fail" and (name is None or c["name"] == name)]


def passes(result: dict, name: str) -> list[str]:
    return [c["detail"] for c in result["checks"] if c["status"] == "pass" and c["name"] == name]


# ------------------------------------------------------------------ the happy path

def test_good_workflow_passes():
    r = check(ci=wf(GOOD_STEPS))
    assert r["ok"], fails(r)
    assert r["schema"] == "bnh-workflow-check/1" and r["rule"] == "actions-pin-v1"
    for name in ("triggers", "permissions", "pinned", "version-comment", "allowed-owner",
                 "checkout-credentials", "run-expressions", "same-pin"):
        assert passes(r, name), name


def test_repo_workflows_pass():
    """The real .github/workflows in this repo pass (CI runs this check too)."""
    r = wc.check_workflows(wc.load_workflows(wc.DEFAULT_WORKFLOWS))
    assert r["ok"], fails(r)
    assert len(passes(r, "pinned")) >= 4


def test_repo_workflows_use_one_commit_per_action():
    files = wc.load_workflows(wc.DEFAULT_WORKFLOWS)
    shas = {}
    for text in files.values():
        for ref in wc._uses_lines(text):
            action, _, sha = ref.partition("@")
            shas.setdefault(action, set()).add(sha)
    assert shas and all(len(s) == 1 for s in shas.values()), shas


# ------------------------------------------------------------------ pinning

@pytest.mark.parametrize("ref", ["v4", "v4.2.2", "main", "11bd71901bbe", "A" * 40, "a" * 39, "a" * 41, "g" * 40])
def test_tags_branches_short_or_bad_hashes_fail(ref):
    steps = f"      - uses: actions/setup-python@{ref} # v5\n"
    r = check(ci=wf(steps))
    assert fails(r, "pinned"), ref


def test_pinned_with_path_passes():
    r = check(ci=wf(f"      - uses: actions/cache/restore@{CO} # v4\n"))
    assert not fails(r, "pinned") and passes(r, "pinned")


def test_local_action_passes():
    r = check(ci=wf("      - uses: ./.github/actions/mine\n"))
    assert r["ok"], fails(r)


def test_docker_needs_digest():
    ok = check(ci=wf(f"      - uses: docker://alpine@sha256:{'c' * 64}\n"))
    assert ok["ok"], fails(ok)
    bad = check(ci=wf("      - uses: docker://alpine:3.20\n"))
    assert fails(bad, "pinned")


def test_job_level_reusable_workflow_must_be_pinned():
    text = ("on: push\npermissions:\n  contents: read\njobs:\n  call:\n"
            "    uses: actions/reusable/.github/workflows/x.yml@main\n")
    r = check(ci=text)
    assert fails(r, "pinned")
    text_ok = text.replace("@main", f"@{CO} # v1")
    assert check(ci=text_ok)["ok"]


def test_unreadable_ref_fails():
    r = check(ci=wf("      - uses: just-a-name\n"))
    assert fails(r, "pinned")


def test_quoted_uses_is_read():
    r = check(ci=wf(f"      - uses: \"actions/setup-python@{SP}\" # v5\n"))
    assert r["ok"], fails(r)


# ------------------------------------------------------------------ version comments

@pytest.mark.parametrize("comment", ["", " # pinned", " # 4.2.2", " #", " # latest v4", " # v4x"])
def test_missing_or_vague_version_comment_fails(comment):
    r = check(ci=wf(f"      - uses: actions/setup-python@{SP}{comment}\n"))
    assert fails(r, "version-comment"), comment


def test_flow_style_uses_cant_be_read_for_a_comment():
    r = check(ci=wf(f"      - {{uses: actions/setup-python@{SP}}}\n"))
    assert fails(r, "version-comment")


def test_one_missing_comment_among_two_uses_fails():
    steps = (f"      - uses: actions/setup-python@{SP} # v5\n"
             f"      - uses: actions/setup-python@{SP}\n")
    r = check(ci=wf(steps))
    assert fails(r, "version-comment")


# ------------------------------------------------------------------ one hash per action

def test_same_action_two_hashes_fails():
    a = wf(f"      - uses: actions/setup-python@{SP} # v5\n")
    b = wf(f"      - uses: actions/setup-python@{'d' * 40} # v5\n")
    r = check(a=a, b=b)
    assert fails(r, "same-pin")
    assert any("a.yml" in d and "b.yml" in d for d in fails(r, "same-pin"))


def test_same_hash_in_two_files_passes():
    r = check(a=wf(GOOD_STEPS), b=wf(GOOD_STEPS))
    assert r["ok"], fails(r)


# ------------------------------------------------------------------ owners

def test_unreviewed_owner_fails_even_when_pinned():
    r = check(ci=wf(f"      - uses: tj-actions/changed-files@{CO} # v45\n"))
    assert fails(r, "allowed-owner")
    assert not fails(r, "pinned")


def test_owner_lookalike_fails():
    r = check(ci=wf(f"      - uses: actions-x/checkout@{CO} # v4\n"))
    assert fails(r, "allowed-owner")


def test_allowed_owner_can_be_extended(monkeypatch):
    monkeypatch.setitem(wc.ALLOWED_OWNERS, "astral-sh", "reviewed")
    r = check(ci=wf(f"      - uses: astral-sh/setup-uv@{CO} # v6\n"))
    assert not fails(r, "allowed-owner")


# ------------------------------------------------------------------ permissions

def test_missing_top_level_permissions_fails():
    r = check(ci=wf(GOOD_STEPS, perms=""))
    assert fails(r, "permissions")


@pytest.mark.parametrize("perms", [
    "permissions: write-all\n",
    "permissions:\n  contents: write\n",
    "permissions:\n  contents: read\n  pull-requests: write\n",
])
def test_top_level_write_fails(perms):
    r = check(ci=wf(GOOD_STEPS, perms=perms))
    assert fails(r, "permissions"), perms


def test_job_level_write_fails():
    r = check(ci=wf(GOOD_STEPS, job_extra="    permissions:\n      contents: write\n"))
    assert fails(r, "permissions")


@pytest.mark.parametrize("perms", ["permissions: read-all\n", "permissions: {}\n",
                                   "permissions:\n  contents: read\n  actions: none\n"])
def test_read_or_none_permissions_pass(perms):
    r = check(ci=wf(GOOD_STEPS, perms=perms))
    assert not fails(r, "permissions"), perms


def test_write_allowed_with_reason(monkeypatch):
    monkeypatch.setitem(wc.WRITE_ALLOWED, "ci.yml", "publishes reports")
    r = check(ci=wf(GOOD_STEPS, perms="permissions:\n  contents: write\n"))
    assert not fails(r, "permissions")
    assert any(c["status"] == "note" and "publishes reports" in c["detail"] for c in r["checks"])


# ------------------------------------------------------------------ triggers

@pytest.mark.parametrize("on", [
    "on: pull_request_target\n",
    "on: [push, pull_request_target]\n",
    "on:\n  pull_request_target:\n    types: [opened]\n",
    "on:\n  workflow_run:\n    workflows: [ci]\n",
    "'on': pull_request_target\n",
])
def test_risky_triggers_fail(on):
    r = check(ci=wf(GOOD_STEPS, on=on))
    assert fails(r, "triggers"), on


@pytest.mark.parametrize("on", ["on: push\n", "on: [push, pull_request]\n",
                                "on:\n  workflow_dispatch:\n    inputs:\n      url:\n        default: x\n"])
def test_safe_triggers_pass(on):
    r = check(ci=wf(GOOD_STEPS, on=on))
    assert not fails(r, "triggers") and passes(r, "triggers"), on


# ------------------------------------------------------------------ checkout credentials

@pytest.mark.parametrize("with_", ["", "        with:\n          fetch-depth: 0\n",
                                   "        with:\n          persist-credentials: true\n"])
def test_checkout_without_persist_credentials_false_fails(with_):
    r = check(ci=wf(f"      - uses: actions/checkout@{CO} # v4\n{with_}"))
    assert fails(r, "checkout-credentials"), with_


def test_checkout_persist_credentials_quoted_false_passes():
    r = check(ci=wf(f"      - uses: actions/checkout@{CO} # v4\n        with:\n          persist-credentials: 'false'\n"))
    assert not fails(r, "checkout-credentials")


def test_unpinned_checkout_still_checked_for_credentials():
    r = check(ci=wf("      - uses: actions/checkout@v4\n"))
    assert fails(r, "pinned") and fails(r, "checkout-credentials")


# ------------------------------------------------------------------ run scripts

@pytest.mark.parametrize("run", [
    "        run: echo ${{ github.event.pull_request.title }}\n",
    "        run: |\n          set -e\n          curl \"${{ inputs.url }}\"\n",
    "        run: echo ${{github.head_ref}}\n",
])
def test_expression_inside_run_fails(run):
    r = check(ci=wf(f"      - name: x\n{run}"))
    assert fails(r, "run-expressions"), run


def test_expression_in_env_passes():
    r = check(ci=wf("      - name: x\n        env:\n          T: ${{ github.head_ref }}\n        run: echo \"$T\"\n"))
    assert not fails(r, "run-expressions") and passes(r, "run-expressions")


# ------------------------------------------------------------------ bad files and CLI

def test_bad_yaml_fails():
    r = check(ci="on: [push\njobs: {")
    assert fails(r, "yaml")


def test_not_a_mapping_fails():
    r = check(ci="- just\n- a list\n")
    assert fails(r, "yaml")


def test_load_needs_workflow_files(tmp_path: Path):
    with pytest.raises(ValueError):
        wc.load_workflows(tmp_path)
    with pytest.raises(ValueError):
        wc.load_workflows(tmp_path / "missing")


def test_cli_ok_and_report(tmp_path: Path, capsys):
    d = tmp_path / "wf"
    d.mkdir()
    (d / "ci.yml").write_text(wf(GOOD_STEPS))
    out = tmp_path / "r.json"
    assert wc.main(["--workflows", str(d), "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["ok"] and rep["schema"] == wc.SCHEMA
    assert "OK:" in capsys.readouterr().out
    # Never overwrites a report.
    assert wc.main(["--workflows", str(d), "--out", str(out)]) == 2


def test_cli_fails_on_problem_and_errors_on_no_files(tmp_path: Path):
    d = tmp_path / "wf"
    d.mkdir()
    assert wc.main(["--workflows", str(d)]) == 2
    (d / "ci.yaml").write_text(wf("      - uses: actions/checkout@v4\n"))
    assert wc.main(["--workflows", str(d)]) == 1
    assert wc.main(["--bogus"]) == 2
