"""Check the GitHub Actions workflows in `.github/workflows/` (`actions-pin-v1`).

A workflow step like `uses: actions/checkout@v4` runs whatever code the `v4` tag points at
on the day the job runs. Tags can be moved: if an action's repository is taken over, the
attacker moves the tag and every workflow that names it runs their code, with that job's
access. A 40-character commit hash can't be moved, so pinning to one means CI runs the code
someone looked at. (This happened for real: `tj-actions/changed-files`, March 2025.)

    python -m app.workflow_check [--workflows ../.github/workflows] [--out f.json]

It reads files only and checks every `*.yml` / `*.yaml` workflow:
  * `pinned`: every action a step or job `uses:` is pinned to a full 40-character lowercase
    commit hash (`owner/repo@<hash>`), or is a local `./` action, or a `docker://` image
    pinned by `@sha256:` digest. Tags and branch names fail.
  * `version-comment`: each pinned action says which release the hash is, as a comment on
    the same line (`# v4`), so a person can tell what they're looking at.
  * `same-pin`: one action is pinned to the same hash in every workflow.
  * `allowed-owner`: the action comes from an owner on `ALLOWED_OWNERS` (today only
    `actions`, GitHub's own). A new owner fails until someone reviews it and adds it.
  * `permissions`: the workflow sets `permissions:` at the top, and nothing (top level or
    job) asks for `write` access or `write-all`, unless the workflow is in `WRITE_ALLOWED`
    with a reason. The default token can otherwise push code in some repository setups.
  * `triggers`: no `pull_request_target` or `workflow_run` trigger. Both run with this
    repository's access while able to be started by someone else's code.
  * `checkout-credentials`: every `actions/checkout` step sets `persist-credentials: false`,
    so the job's token isn't left on disk where later steps (or tests) could read it.
  * `run-expressions`: no `${{ ... }}` inside a `run:` script. GitHub pastes those values
    into the script before it runs, so a crafted branch name or form input can become a
    command. Pass values through `env:` and use `"$NAME"` instead.

Prints one line per check, writes a `bnh-workflow-check/1` JSON report with `--out` (never
over an existing file), and exits 0 when nothing failed, 1 when something did, 2 on a usage
error or when there are no workflow files to read. It changes nothing and fetches nothing:
it can't tell whether a hash really belongs to the release its comment names (see
`docs/plan/README.md` for how the hashes here were found).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SCHEMA = "bnh-workflow-check/1"
RULE = "actions-pin-v1"
REPO_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_WORKFLOWS = REPO_DIR / ".github" / "workflows"

# Action owners a person has reviewed. Add one only after reading what it does.
ALLOWED_OWNERS = {
    "actions": "GitHub's own actions (checkout, setup-python, ...)",
}
# Workflows allowed to ask for write access: {"file.yml": "reason"}. None need it today.
WRITE_ALLOWED: dict[str, str] = {}
# Triggers that run with this repository's access but can be started by other people's code.
RISKY_TRIGGERS = {
    "pull_request_target": "runs with this repo's access on code from a fork",
    "workflow_run": "runs with this repo's access after a workflow a fork can start",
}

_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
_ACTION_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)((?:/[^@\s]+)?)@(\S+)\Z")
_DOCKER_RE = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}\Z")
_USES_LINE_RE = re.compile(
    r"""^\s*(?:-\s+)?uses:\s*(?P<q>['"]?)(?P<ref>[^\s'"#]+)(?P=q)\s*(?:\#\s*(?P<comment>.*?))?\s*$""")
_VERSION_COMMENT_RE = re.compile(r"^v\d+(\.\d+)*\b")


# ------------------------------------------------------------------ report

@dataclass
class Report:
    checks: list[dict] = field(default_factory=list)

    def add(self, status: str, name: str, detail: str, workflow: str | None = None) -> None:
        c = {"status": status, "name": name, "detail": detail}
        if workflow:
            c["workflow"] = workflow
        self.checks.append(c)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for c in self.checks:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        return {
            "schema": SCHEMA,
            "rule": RULE,
            "ok": counts.get("fail", 0) == 0,
            "counts": counts,
            "checks": self.checks,
        }


# ------------------------------------------------------------------ parsing helpers

def _triggers(doc: dict) -> list[str]:
    # YAML 1.1 reads a bare `on:` key as the boolean True.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return [str(t) for t in on]
    if isinstance(on, dict):
        return [str(t) for t in on]
    return []


def _write_grants(perms) -> list[str]:
    """Which scopes a `permissions:` value grants write access to."""
    if isinstance(perms, str):
        return ["write-all"] if perms.strip() == "write-all" else []
    if isinstance(perms, dict):
        return sorted(str(k) for k, v in perms.items() if str(v).strip() == "write")
    return []


def _uses_lines(text: str) -> dict[str, list[str | None]]:
    """Every `uses:` value written on its own line, with its trailing comment (or None)."""
    out: dict[str, list[str | None]] = {}
    for line in text.splitlines():
        m = _USES_LINE_RE.match(line)
        if m:
            out.setdefault(m.group("ref"), []).append(m.group("comment") or None)
    return out


def _steps_and_jobs(doc: dict):
    """Yield (where, mapping) for each job and each step in it."""
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        yield f"job {job_id}", job
        steps = job.get("steps")
        if isinstance(steps, list):
            for i, step in enumerate(steps, 1):
                if isinstance(step, dict):
                    label = step.get("name") or str(step.get("uses") or "").split("@")[0] or step.get("id") or f"#{i}"
                    yield f"job {job_id}, step {label!s}", step


# ------------------------------------------------------------------ the check

def check_workflows(files: dict[str, str]) -> dict:
    """Check workflow files given as {file name: text}."""
    report = Report()
    pins: dict[str, dict[str, list[str]]] = {}  # action -> hash -> [workflow files]

    for wf, text in sorted(files.items()):
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            report.add("fail", "yaml", f"can't read it as YAML: {str(e).splitlines()[0]}", wf)
            continue
        if not isinstance(doc, dict):
            report.add("fail", "yaml", "isn't a workflow (no top-level mapping)", wf)
            continue

        # Triggers.
        risky = [t for t in _triggers(doc) if t in RISKY_TRIGGERS]
        for t in risky:
            report.add("fail", "triggers", f"`{t}` {RISKY_TRIGGERS[t]}; use `pull_request` instead", wf)
        if not risky:
            report.add("pass", "triggers", "no pull_request_target / workflow_run", wf)

        # Permissions.
        if "permissions" not in doc:
            report.add("fail", "permissions", "no top-level `permissions:`; add `permissions: {contents: read}` "
                       "so the job token can only read", wf)
        writes = [f"top level: {', '.join(_write_grants(doc.get('permissions')))}"] \
            if _write_grants(doc.get("permissions")) else []
        jobs = doc.get("jobs") if isinstance(doc.get("jobs"), dict) else {}
        for job_id, job in jobs.items():
            if isinstance(job, dict) and _write_grants(job.get("permissions")):
                writes.append(f"job {job_id}: {', '.join(_write_grants(job['permissions']))}")
        if writes and wf not in WRITE_ALLOWED:
            for w in writes:
                report.add("fail", "permissions", f"asks for write access ({w}); if it really needs it, "
                           "add the file to WRITE_ALLOWED with a reason", wf)
        elif writes:
            report.add("note", "permissions", f"write access allowed: {WRITE_ALLOWED[wf]}", wf)
        elif "permissions" in doc:
            report.add("pass", "permissions", "read-only token", wf)

        # Actions, checkout credentials, run scripts.
        lines = _uses_lines(text)
        n_actions = 0
        n_injectable = 0
        for where, item in _steps_and_jobs(doc):
            run = item.get("run")
            if isinstance(run, str) and "${{" in run:
                n_injectable += 1
                report.add("fail", "run-expressions", f"{where}: `${{{{ ... }}}}` inside `run:`; "
                           "pass it through `env:` and use \"$NAME\" in the script", wf)
            ref = item.get("uses")
            if ref is None:
                continue
            ref = str(ref).strip()
            n_actions += 1
            if ref.startswith("./"):
                report.add("pass", "pinned", f"{where}: {ref} is a local action", wf)
                continue
            if ref.startswith("docker://"):
                if _DOCKER_RE.match(ref):
                    report.add("pass", "pinned", f"{where}: {ref}", wf)
                else:
                    report.add("fail", "pinned", f"{where}: {ref} isn't pinned by @sha256: digest", wf)
                continue
            m = _ACTION_RE.match(ref)
            if not m:
                report.add("fail", "pinned", f"{where}: can't read `{ref}` as owner/repo@ref", wf)
                continue
            owner, repo, path, at = m.group(1), m.group(2), m.group(3), m.group(4)
            action = f"{owner}/{repo}{path}"
            if owner not in ALLOWED_OWNERS:
                report.add("fail", "allowed-owner", f"{where}: `{owner}` isn't a reviewed action owner; "
                           "read the action's code, then add it to ALLOWED_OWNERS", wf)
            else:
                report.add("pass", "allowed-owner", f"{where}: {owner}", wf)
            if not _SHA_RE.match(at):
                report.add("fail", "pinned", f"{where}: {action}@{at} names a tag or branch, which can be "
                           "moved; pin it to the full 40-character commit hash", wf)
            else:
                report.add("pass", "pinned", f"{where}: {action}@{at[:12]}…", wf)
                pins.setdefault(action, {}).setdefault(at, []).append(wf)
            comments = lines.get(ref)
            if comments is None:
                report.add("fail", "version-comment", f"{where}: write `uses: {ref}` on its own line "
                           "so its version comment can be read", wf)
            elif _SHA_RE.match(at):
                bad = [c for c in comments if not (c and _VERSION_COMMENT_RE.match(c))]
                if bad:
                    report.add("fail", "version-comment", f"{where}: add the release it stands for as a "
                               "comment on the same line, e.g. `# v4`", wf)
                else:
                    report.add("pass", "version-comment", f"{where}: {comments[0]}", wf)
            if owner == "actions" and repo == "checkout":
                with_ = item.get("with") if isinstance(item.get("with"), dict) else {}
                pc = with_.get("persist-credentials")
                if pc is False or str(pc).strip().lower() == "false":
                    report.add("pass", "checkout-credentials", f"{where}: persist-credentials: false", wf)
                else:
                    report.add("fail", "checkout-credentials", f"{where}: set `persist-credentials: false` "
                               "under `with:` so the token isn't left in .git/config", wf)
        if n_actions == 0:
            report.add("note", "pinned", "uses no actions", wf)
        if n_injectable == 0:
            report.add("pass", "run-expressions", "no ${{ }} inside run: scripts", wf)

    for action, by_sha in sorted(pins.items()):
        if len(by_sha) > 1:
            where = "; ".join(f"{sha[:12]}… in {', '.join(sorted(set(w)))}" for sha, w in sorted(by_sha.items()))
            report.add("fail", "same-pin", f"{action} is pinned to different commits: {where}")
        else:
            report.add("pass", "same-pin", f"{action}: one commit everywhere")
    return report.summary()


def load_workflows(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        raise ValueError(f"{directory} isn't a folder")
    files = sorted(list(directory.glob("*.yml")) + list(directory.glob("*.yaml")))
    if not files:
        raise ValueError(f"no workflow files (*.yml, *.yaml) in {directory}")
    return {p.name: p.read_text(encoding="utf-8") for p in files}


# ------------------------------------------------------------------ CLI

def _print(result: dict) -> None:
    for c in result["checks"]:
        wf = f"[{c['workflow']}] " if c.get("workflow") else ""
        print(f"{c['status'].upper():5} {c['name']:20} {wf}{c['detail']}")
    counts = result.get("counts", {})
    print(f"\n{'OK' if result['ok'] else 'PROBLEMS FOUND'}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.workflow_check", description=__doc__.split("\n")[0])
    ap.add_argument("--workflows", type=Path, default=DEFAULT_WORKFLOWS)
    ap.add_argument("--out", type=Path)
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if args.out and args.out.exists():
        print(f"error: {args.out} already exists; not overwriting it", file=sys.stderr)
        return 2
    try:
        result = check_workflows(load_workflows(args.workflows))
    except (ValueError, OSError, UnicodeDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    _print(result)
    if args.out:
        try:
            with args.out.open("x", encoding="utf-8") as f:
                f.write(json.dumps(result, indent=2) + "\n")
        except OSError as e:
            print(f"error: couldn't write {args.out}: {e}", file=sys.stderr)
            return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
