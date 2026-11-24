"""Signed worker releases with several signers (`release-sign-v1`)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app import release
from app.release import ReleaseError

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          env=GIT_ENV, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "worker.py").write_text("print('honest worker')\n")
    (root / "README.md").write_text("# worker\n")
    (root / ".gitignore").write_text("data/\n.env\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "first")
    # Things a rig keeps next to the code that must never end up in a release.
    (root / ".env").write_text("OPERATOR_TOKEN=secret\n")
    (root / "data").mkdir()
    (root / "data" / "coordinator-signing.key").write_text("not a real key")
    return root


@pytest.fixture
def keys(tmp_path: Path) -> dict[str, dict]:
    out = {}
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    for name in ("alice", "bob", "carol", "mallory"):
        path = private / f"{name}.key"
        out[name] = {**release.keygen(path, name), "path": path}
    return out


def signer_list(tmp_path: Path, keys: dict, names=("alice", "bob", "carol"), threshold=2,
                filename="signers.json") -> Path:
    path = tmp_path / filename
    path.write_text(json.dumps({
        "format": release.SIGNERS_FORMAT, "threshold": threshold,
        "signers": [{"name": n, "public_key": keys[n]["public_key"]} for n in names]}))
    return path


def made(repo: Path, tmp_path: Path, version: str = "0.5.49") -> Path:
    manifest = release.make_manifest(repo, "bnh-worker", version)
    out = tmp_path / "out" / "release.json"
    release.write_release(manifest, out)
    return out


def signed(repo, tmp_path, keys, who=("alice", "bob"), version="0.5.49") -> Path:
    out = made(repo, tmp_path, version)
    for name in who:
        release.sign_release(out, keys[name]["path"], name)
    return out


# ---- make -------------------------------------------------------------------------------

def test_make_lists_only_tracked_files_with_hashes(repo, tmp_path):
    manifest = release.make_manifest(repo, "bnh-worker", "0.5.49")
    paths = [f["path"] for f in manifest["files"]]
    assert paths == [".gitignore", "README.md", "app/__init__.py", "app/worker.py"]
    assert ".env" not in paths and not any(p.startswith("data/") for p in paths)
    worker = next(f for f in manifest["files"] if f["path"] == "app/worker.py")
    assert worker["sha256"] == release.sha256_file(repo / "app" / "worker.py")
    assert worker["size"] == len("print('honest worker')\n")
    assert manifest["source_commit"] == git(repo, "rev-parse", "HEAD").strip()
    assert release.check_manifest(manifest) == []


def test_make_refuses_uncommitted_changes(repo):
    (repo / "app" / "worker.py").write_text("print('edited')\n")
    with pytest.raises(ReleaseError, match="uncommitted"):
        release.make_manifest(repo, "bnh-worker", "0.5.49")


def test_untracked_files_do_not_block_make(repo):
    (repo / "notes.txt").write_text("scratch")
    manifest = release.make_manifest(repo, "bnh-worker", "0.5.49")
    assert "notes.txt" not in [f["path"] for f in manifest["files"]]


def test_make_refuses_symlinks(repo):
    os.symlink("/etc/hostname", repo / "link")
    git(repo, "add", "link")
    git(repo, "commit", "-q", "-m", "link")
    with pytest.raises(ReleaseError, match="symlink"):
        release.make_manifest(repo, "bnh-worker", "0.5.49")


def test_make_outside_git_refused(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "a.py").write_text("")
    with pytest.raises(ReleaseError, match="git"):
        release.make_manifest(folder, "bnh-worker", "0.5.49")


def test_make_refuses_bad_version(repo):
    with pytest.raises(ReleaseError, match="version"):
        release.make_manifest(repo, "bnh-worker", "latest")


def test_write_release_never_overwrites_and_writes_sha256sums(repo, tmp_path):
    out = made(repo, tmp_path)
    sums = (out.parent / "SHA256SUMS").read_text()
    assert f"{release.sha256_file(repo / 'app/worker.py')}  app/worker.py\n" in sums
    manifest = json.loads(out.read_text())
    with pytest.raises(ReleaseError, match="never overwritten"):
        release.write_release(manifest, out)


def test_sha256sums_works_with_coreutils(repo, tmp_path):
    out = made(repo, tmp_path)
    result = subprocess.run(["sha256sum", "-c", str(out.parent / "SHA256SUMS")], cwd=repo,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# ---- manifest shape ---------------------------------------------------------------------

def good_manifest(repo) -> dict:
    return release.make_manifest(repo, "bnh-worker", "0.5.49")


@pytest.mark.parametrize("path", ["/etc/passwd", "../x.py", "a/../b.py", "a//b", "./a.py",
                                  "a\\b.py", ".git/hooks/pre-commit", "a/.git/config", "",
                                  "C:/x.py", "a\nb"])
def test_bad_paths_refused(repo, path):
    manifest = good_manifest(repo)
    manifest["files"][0]["path"] = path
    assert any("file #1" in p for p in release.check_manifest(manifest))


def test_duplicate_and_case_duplicate_paths_refused(repo):
    manifest = good_manifest(repo)
    dup = dict(manifest["files"][-1])
    dup["path"] = dup["path"].upper()
    manifest["files"].append(dup)
    assert any("twice" in p for p in release.check_manifest(manifest))


def test_unsorted_or_bad_entries_refused(repo):
    manifest = good_manifest(repo)
    manifest["files"].reverse()
    assert "files must be sorted by path" in release.check_manifest(manifest)
    manifest = good_manifest(repo)
    manifest["files"][0]["sha256"] = manifest["files"][0]["sha256"].upper()
    manifest["files"][1]["size"] = True
    manifest["files"][2]["extra"] = 1
    problems = release.check_manifest(manifest)
    assert any("sha256" in p for p in problems)
    assert any("size" in p for p in problems)
    assert any("file #3" in p for p in problems)


def test_unknown_or_missing_manifest_fields_refused(repo):
    manifest = good_manifest(repo)
    manifest["install_script"] = "curl evil | sh"
    assert any("fields wrong" in p for p in release.check_manifest(manifest))
    manifest = good_manifest(repo)
    del manifest["source_commit"]
    assert any("fields wrong" in p for p in release.check_manifest(manifest))
    assert release.check_manifest([]) == ["release manifest is not a JSON object"]


# ---- signer list ------------------------------------------------------------------------

def test_signer_list_rules(tmp_path, keys):
    good = json.loads(signer_list(tmp_path, keys).read_text())
    assert release.check_signers(good) == []
    one = {**good, "threshold": 1}
    assert any("at least 2" in p for p in release.check_signers(one))
    too_high = {**good, "threshold": 4}
    assert any("more than the 3" in p for p in release.check_signers(too_high))
    same_key = {**good, "signers": good["signers"] + [
        {"name": "dave", "public_key": keys["alice"]["public_key"]}]}
    assert any("same public key" in p for p in release.check_signers(same_key))
    same_name = {**good, "signers": good["signers"] + [
        {"name": "alice", "public_key": keys["mallory"]["public_key"]}]}
    assert any("listed twice" in p for p in release.check_signers(same_name))
    upper = {**good, "signers": [{"name": "alice",
                                  "public_key": keys["alice"]["public_key"].upper()},
                                 *good["signers"][1:]]}
    assert any("lowercase hex" in p for p in release.check_signers(upper))
    assert any("format" in p for p in release.check_signers({**good, "format": "x"}))
    assert any("whole number" in p for p in release.check_signers({**good, "threshold": True}))


def test_bad_signer_list_is_an_input_error(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    bad = signer_list(tmp_path, keys, threshold=1, filename="one.json")
    with pytest.raises(ReleaseError, match="at least 2"):
        release.verify_release(out, bad)


# ---- keygen and sign --------------------------------------------------------------------

def test_keygen_is_private_and_never_overwrites(tmp_path, keys):
    path = keys["alice"]["path"]
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(ReleaseError, match="already exists"):
        release.keygen(path, "alice")


def test_keygen_refuses_git_folder(repo):
    with pytest.raises(ReleaseError, match="git folder"):
        release.keygen(repo / "keys" / "alice.key", "alice")
    assert not (repo / "keys" / "alice.key").exists()


def test_sign_refuses_key_readable_by_others(repo, tmp_path, keys):
    out = made(repo, tmp_path)
    keys["alice"]["path"].chmod(0o644)
    with pytest.raises(ReleaseError, match="other users"):
        release.sign_release(out, keys["alice"]["path"], "alice")


def test_sign_never_creates_a_key(repo, tmp_path):
    out = made(repo, tmp_path)
    missing = tmp_path / "nope.key"
    with pytest.raises(ReleaseError, match="does not exist"):
        release.sign_release(out, missing, "alice")
    assert not missing.exists()


def test_same_signer_or_key_cannot_sign_twice(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys, who=("alice",))
    with pytest.raises(ReleaseError, match="already signed"):
        release.sign_release(out, keys["alice"]["path"], "alice")
    with pytest.raises(ReleaseError, match="already signed this release under the name"):
        release.sign_release(out, keys["alice"]["path"], "bob")


def test_sign_refuses_signature_file_of_another_release(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys, who=("alice",))
    manifest = json.loads(out.read_text())
    manifest["version"] = "0.5.50"
    out.write_text(json.dumps(manifest))
    with pytest.raises(ReleaseError, match="different release"):
        release.sign_release(out, keys["bob"]["path"], "bob")


def test_sign_refuses_malformed_release(repo, tmp_path, keys):
    out = made(repo, tmp_path)
    manifest = json.loads(out.read_text())
    manifest["files"][0]["path"] = "../escape.py"
    out.write_text(json.dumps(manifest))
    with pytest.raises(ReleaseError, match="malformed"):
        release.sign_release(out, keys["alice"]["path"], "alice")


# ---- verify -----------------------------------------------------------------------------

def test_two_of_three_signers_and_matching_files_pass(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    report = release.verify_release(out, signer_list(tmp_path, keys), root=repo)
    assert report["ok"], report
    assert report["valid_signers"] == ["alice", "bob"]
    assert report["checked_root"] is True and report["files"] == 4
    # .env and data/ sit next to the code but are neither problems nor notes.
    assert not any(".env" in n or "data/" in n for n in report["notes"])


def test_one_signer_is_not_enough(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys, who=("alice",))
    report = release.verify_release(out, signer_list(tmp_path, keys))
    assert not report["ok"]
    assert any("only 1 of the required 2" in p for p in report["problems"])


def test_unlisted_signer_does_not_count(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys, who=("alice", "mallory"))
    report = release.verify_release(out, signer_list(tmp_path, keys))
    assert not report["ok"] and report["valid_signers"] == ["alice"]
    assert any("not on your signer list" in n for n in report["notes"])


def test_stolen_name_with_other_key_fails(repo, tmp_path, keys):
    out = made(repo, tmp_path)
    release.sign_release(out, keys["alice"]["path"], "alice")
    release.sign_release(out, keys["mallory"]["path"], "bob")  # mallory pretends to be bob
    report = release.verify_release(out, signer_list(tmp_path, keys))
    assert not report["ok"]
    assert any("claims to be bob" in p for p in report["problems"])


def test_copied_signature_counts_once(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    sigs_path = release.sigs_path_for(out)
    sigs = json.loads(sigs_path.read_text())
    sigs["signatures"].append(dict(sigs["signatures"][0]))
    sigs["signatures"].append(dict(sigs["signatures"][0]))
    sigs_path.write_text(json.dumps(sigs))
    report = release.verify_release(out, signer_list(tmp_path, keys, threshold=3))
    assert not report["ok"] and report["valid_signers"] == ["alice", "bob"]
    assert any("counted once" in n for n in report["notes"])


def test_manifest_edited_after_signing_fails(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    manifest = json.loads(out.read_text())
    manifest["files"][-1]["sha256"] = "0" * 64
    out.write_text(json.dumps(manifest))
    report = release.verify_release(out, signer_list(tmp_path, keys))
    assert not report["ok"]
    assert any("different release_sha256" in p for p in report["problems"])
    assert report["valid_signers"] == []


def test_signature_from_another_release_does_not_transfer(repo, tmp_path, keys):
    old = signed(repo, tmp_path, keys, version="0.5.48")
    (repo / "app" / "worker.py").write_text("print('new')\n")
    git(repo, "commit", "-qam", "new")
    new_manifest = release.make_manifest(repo, "bnh-worker", "0.5.49")
    new = tmp_path / "new" / "release.json"
    release.write_release(new_manifest, new)
    sigs = json.loads(release.sigs_path_for(old).read_text())
    sigs["release_sha256"] = release.manifest_sha256(new_manifest)  # relabel, keep old sigs
    release.sigs_path_for(new).write_text(json.dumps(sigs))
    report = release.verify_release(new, signer_list(tmp_path, keys))
    assert not report["ok"]
    assert sum("does not match this release" in p for p in report["problems"]) == 2


def test_job_signature_purpose_is_not_a_release_signature(repo, tmp_path, keys):
    """A signature the same key made for something else (no release purpose) is refused."""
    from app.signing import ManifestSigner
    out = made(repo, tmp_path)
    manifest = json.loads(out.read_text())
    fields = release.signed_fields(manifest)
    sigs = {"format": release.SIGS_FORMAT, "release_sha256": fields["release_sha256"],
            "signatures": []}
    for name in ("alice", "bob"):
        signer = ManifestSigner.load(keys[name]["path"])
        wrong = {k: v for k, v in fields.items() if k != "purpose"}
        sigs["signatures"].append({"signer": name, "public_key": signer.public_key_hex,
                                   "signature": signer.sign(wrong), "signed_at": "x"})
    release.sigs_path_for(out).write_text(json.dumps(sigs))
    report = release.verify_release(out, signer_list(tmp_path, keys))
    assert not report["ok"] and report["valid_signers"] == []


def test_garbage_signature_fails_even_with_enough_good_ones(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    release.sign_release(out, keys["carol"]["path"], "carol")
    sigs_path = release.sigs_path_for(out)
    sigs = json.loads(sigs_path.read_text())
    sigs["signatures"][2]["signature"] = "ed25519:" + "00" * 64
    sigs_path.write_text(json.dumps(sigs))
    report = release.verify_release(out, signer_list(tmp_path, keys))
    assert not report["ok"] and report["valid_signers"] == ["alice", "bob"]
    assert any("carol does not match" in p for p in report["problems"])


def test_changed_file_on_disk_fails(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    (repo / "app" / "worker.py").write_text("print('send keys to evil')\n")
    report = release.verify_release(out, signer_list(tmp_path, keys), root=repo)
    assert not report["ok"]
    assert "app/worker.py: contents differ from the signed release" in report["problems"]


def test_same_size_change_still_caught(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    (repo / "app" / "worker.py").write_text("print('honest wOrker')\n")
    report = release.verify_release(out, signer_list(tmp_path, keys), root=repo)
    assert "app/worker.py: contents differ from the signed release" in report["problems"]


def test_missing_file_fails(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    (repo / "README.md").unlink()
    report = release.verify_release(out, signer_list(tmp_path, keys), root=repo)
    assert "README.md: missing" in report["problems"]


def test_extra_python_file_fails_other_extras_are_notes(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    (repo / "app" / "sitecustomize.py").write_text("import os\n")
    (repo / "run.log").write_text("log")
    (repo / "app" / "__pycache__").mkdir()
    (repo / "app" / "__pycache__" / "worker.cpython-311.pyc").write_bytes(b"x")
    report = release.verify_release(out, signer_list(tmp_path, keys), root=repo)
    assert "app/sitecustomize.py: Python file not in the signed release" in report["problems"]
    assert any(n.startswith("run.log") for n in report["notes"])
    assert not any("pycache" in x for x in report["problems"] + report["notes"])


def test_symlinked_file_on_disk_fails(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text((repo / "app" / "worker.py").read_text())
    (repo / "app" / "worker.py").unlink()
    os.symlink(elsewhere, repo / "app" / "worker.py")
    report = release.verify_release(out, signer_list(tmp_path, keys), root=repo)
    assert any("app/worker.py: is a symlink" in p for p in report["problems"])


def test_symlink_inside_the_folder_still_fails(repo, tmp_path, keys):
    """Even a link to an identical file inside the folder (here in the ignored data/ folder,
    which a later edit could change unseen) is refused: releases are plain files only."""
    out = signed(repo, tmp_path, keys)
    (repo / "data" / "worker.py").write_text((repo / "app" / "worker.py").read_text())
    (repo / "app" / "worker.py").unlink()
    os.symlink(repo / "data" / "worker.py", repo / "app" / "worker.py")
    report = release.verify_release(out, signer_list(tmp_path, keys), root=repo)
    assert any("app/worker.py: is a symlink" in p for p in report["problems"])


def test_symlinked_folder_pointing_outside_fails(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    outside = tmp_path / "outside"
    outside.mkdir()
    for f in ("__init__.py", "worker.py"):
        (outside / f).write_text((repo / "app" / f).read_text())
        (repo / "app" / f).unlink()
    (repo / "app").rmdir()
    os.symlink(outside, repo / "app")
    report = release.verify_release(out, signer_list(tmp_path, keys), root=repo)
    assert not report["ok"]
    assert any("points outside the release folder" in p for p in report["problems"])


def test_rollback_floor(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys, version="0.5.40")
    signers = signer_list(tmp_path, keys)
    assert release.verify_release(out, signers, not_older_than="0.5.40")["ok"]
    report = release.verify_release(out, signers, not_older_than="0.5.48")
    assert not report["ok"] and any("older than 0.5.48" in p for p in report["problems"])
    with pytest.raises(ReleaseError, match="not a version"):
        release.verify_release(out, signers, not_older_than="newest")


def test_without_root_says_files_not_checked(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    report = release.verify_release(out, signer_list(tmp_path, keys))
    assert report["ok"] and report["checked_root"] is False
    assert any("not checked" in n for n in report["notes"])


def test_missing_signature_file_is_input_error(repo, tmp_path, keys):
    out = made(repo, tmp_path)
    with pytest.raises(ReleaseError, match="does not exist"):
        release.verify_release(out, signer_list(tmp_path, keys))


def test_malformed_manifest_reported_not_crashing(repo, tmp_path, keys):
    out = signed(repo, tmp_path, keys)
    out.write_text(json.dumps({"format": "nope"}))
    report = release.verify_release(out, signer_list(tmp_path, keys))
    assert not report["ok"] and report["problems"]


# ---- CLI --------------------------------------------------------------------------------

def test_cli_end_to_end(repo, tmp_path, capsys):
    private = tmp_path / "keys"
    private.mkdir(mode=0o700)
    entries = []
    for name in ("alice", "bob", "carol"):
        assert release.main(["keygen", "--out", str(private / f"{name}.key"),
                             "--name", name]) == 0
        line = capsys.readouterr().out.strip().splitlines()[-1]
        entries.append(json.loads(line))
    signers = tmp_path / "signers.json"
    signers.write_text(json.dumps({"format": release.SIGNERS_FORMAT, "threshold": 2,
                                   "signers": entries}))
    out = tmp_path / "rel" / "release.json"
    assert release.main(["make", "--root", str(repo), "--version", "0.5.49",
                         "--out", str(out)]) == 0
    assert release.main(["sign", str(out), "--key", str(private / "alice.key"),
                         "--name", "alice"]) == 0
    assert release.main(["verify", str(out), "--signers", str(signers),
                         "--root", str(repo)]) == 1
    assert "NOT OK" in capsys.readouterr().out
    assert release.main(["sign", str(out), "--key", str(private / "carol.key"),
                         "--name", "carol"]) == 0
    assert release.main(["verify", str(out), "--signers", str(signers), "--root", str(repo),
                         "--json"]) == 0
    printed = capsys.readouterr().out
    report = json.loads(printed[printed.index("{"):])
    assert report["ok"] and report["valid_signers"] == ["alice", "carol"]
    assert release.main(["make", "--root", str(repo), "--version", "0.5.49",
                         "--out", str(out)]) == 2
    assert "never overwritten" in capsys.readouterr().err


def test_cli_output_never_contains_private_key(repo, tmp_path, capsys):
    path = tmp_path / "k" / "alice.key"
    path.parent.mkdir(mode=0o700)
    release.main(["keygen", "--out", str(path), "--name", "alice"])
    printed = capsys.readouterr().out
    assert "PRIVATE KEY" not in printed
    body = "".join(path.read_text().splitlines()[1:-1])
    assert body not in printed


def test_real_prototype_folder_can_be_listed():
    """The tool works on this repo's own prototype/ folder (files only, nothing written)."""
    root = Path(__file__).resolve().parents[1]
    try:
        paths, commit = release.git_tracked_files(root)
    except ReleaseError as exc:
        if "uncommitted" in str(exc):
            pytest.skip("working tree has uncommitted changes")
        raise
    assert "app/release.py" in paths or "app/main.py" in paths
    assert not any(p.startswith("data/") or p == ".env" for p in paths)
    assert len(commit) == 40
