"""`verify --root` against a hostile release folder (`release-folder-v1`).

A release folder is something someone else handed over. Before this rule, three kinds of
planted code passed `verify` and then ran when the worker started (each test below shows
the code really runs, so the check is guarding against something real):
  * a sourceless `json.pyc` next to the code (shadows the standard library),
  * a `__pycache__/worker.<tag>.pyc` whose header matches the real source (run instead of it),
  * a linked folder named like a module.
"""
from __future__ import annotations

import os
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

from app import release
from tests.test_release import keys, repo, signed, signer_list  # noqa: F401 (fixtures)

TAG = sys.implementation.cache_tag
EVIL = "print('EVIL CODE RAN')\ndumps = str\n"
FIX = "delete the __pycache__ folders"


def verify(out: Path, tmp_path: Path, keys, root: Path) -> dict:
    return release.verify_release(out, signer_list(tmp_path, keys), root=root)


def run_worker(root: Path, *flags: str) -> str:
    """Start the release's worker the way a rig would (`python -m app.worker`)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    done = subprocess.run([sys.executable, *flags, "-m", "app.worker"], cwd=root, env=env,
                          capture_output=True, text=True, timeout=60)
    return done.stdout + done.stderr


@pytest.fixture
def honest(repo, tmp_path, keys):  # noqa: F811
    (repo / "app" / "worker.py").write_text(
        '"""Honest worker (docstring and assert, so -O and -OO caches differ)."""\n'
        "import json\nassert json\nprint('honest worker', json.dumps(1))\n")
    from tests.test_release import git
    git(repo, "commit", "-q", "-am", "worker imports json")
    out = signed(repo, tmp_path, keys)
    assert verify(out, tmp_path, keys, repo)["ok"]
    return out


def cache_header_like(source: Path, body: str) -> bytes:
    """A cache with the real source's time and size in its header but other code inside:
    Python's normal check (time + size) accepts it."""
    import importlib.util
    import marshal
    st = source.stat()
    code = compile(body, str(source.resolve()), "exec")
    return (importlib.util.MAGIC_NUMBER + (0).to_bytes(4, "little")
            + (int(st.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
            + (st.st_size & 0xFFFFFFFF).to_bytes(4, "little") + marshal.dumps(code))


# ---- the three holes, shown real and now caught -----------------------------------------

def test_sourceless_pyc_next_to_the_code_is_caught(repo, tmp_path, keys, honest):
    evil = tmp_path / "evil.py"
    evil.write_text(EVIL)
    py_compile.compile(str(evil), cfile=str(repo / "json.pyc"))
    assert "EVIL CODE RAN" in run_worker(repo)  # the threat is real
    report = verify(honest, tmp_path, keys, repo)
    assert not report["ok"]
    assert ("json.pyc: Python code not in the signed release (Python can import it)"
            in report["problems"])


def test_cache_with_matching_header_is_caught(repo, tmp_path, keys, honest):
    src = repo / "app" / "worker.py"
    (repo / "app" / "__pycache__").mkdir()
    (repo / "app" / "__pycache__" / f"worker.{TAG}.pyc").write_bytes(
        cache_header_like(src, "print('EVIL CODE RAN via cache')\n"))
    assert "EVIL CODE RAN via cache" in run_worker(repo)  # Python ran it instead of the source
    report = verify(honest, tmp_path, keys, repo)
    assert not report["ok"]
    assert any(p.startswith(f"app/__pycache__/worker.{TAG}.pyc: compiled cache doesn't match")
               and FIX in p for p in report["problems"])


def test_linked_folder_named_like_a_module_is_caught(repo, tmp_path, keys, honest):
    pkg = tmp_path / "evilpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(EVIL)
    os.symlink(pkg, repo / "json")
    assert "EVIL CODE RAN" in run_worker(repo)
    report = verify(honest, tmp_path, keys, repo)
    assert not report["ok"]
    assert any(p.startswith("json: link not in the signed release") for p in report["problems"])


# ---- honest caches still pass ----------------------------------------------------------

def test_caches_python_built_itself_pass(repo, tmp_path, keys, honest):
    assert "honest worker" in run_worker(repo)
    assert "honest worker" in run_worker(repo, "-O")
    assert "honest worker" in run_worker(repo, "-OO")
    names = sorted(p.name for p in (repo / "app" / "__pycache__").iterdir())
    assert f"worker.{TAG}.pyc" in names and f"worker.{TAG}.opt-1.pyc" in names \
        and f"worker.{TAG}.opt-2.pyc" in names
    report = verify(honest, tmp_path, keys, repo)
    assert report["ok"], report["problems"]
    assert report["folder_rule"] == "release-folder-v1"


@pytest.mark.parametrize("mode", [py_compile.PycInvalidationMode.CHECKED_HASH,
                                  py_compile.PycInvalidationMode.UNCHECKED_HASH,
                                  py_compile.PycInvalidationMode.TIMESTAMP])
def test_every_kind_of_honest_cache_passes(repo, tmp_path, keys, honest, mode):
    src = repo / "app" / "worker.py"
    py_compile.compile(str(src.resolve()), doraise=True, invalidation_mode=mode)
    assert (repo / "app" / "__pycache__" / f"worker.{TAG}.pyc").exists()
    assert verify(honest, tmp_path, keys, repo)["ok"]


def test_unchecked_hash_cache_with_other_code_is_caught(repo, tmp_path, keys, honest):
    """An "unchecked" cache is loaded without looking at the source at all."""
    src = repo / "app" / "worker.py"
    evil = tmp_path / "w.py"
    evil.write_text("print('EVIL CODE RAN unchecked')\n")
    cfile = repo / "app" / "__pycache__" / f"worker.{TAG}.pyc"
    py_compile.compile(str(evil), cfile=str(cfile), dfile=str(src.resolve()),
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    assert "EVIL CODE RAN unchecked" in run_worker(repo)
    report = verify(honest, tmp_path, keys, repo)
    assert any(p.startswith(f"app/__pycache__/worker.{TAG}.pyc: compiled cache")
               for p in report["problems"])


def test_moved_folder_caches_are_flagged_and_deleting_them_fixes_it(repo, tmp_path, keys,
                                                                    honest):
    """Caches remember the folder they were built in. After a move they no longer match
    byte for byte; that is flagged (the safe direction) and deleting them is the fix."""
    run_worker(repo)
    moved = tmp_path / "moved"
    repo.rename(moved)
    report = verify(honest, tmp_path, keys, moved)
    assert not report["ok"] and any(FIX in p for p in report["problems"])
    for cache in moved.rglob("__pycache__"):
        for f in cache.iterdir():
            f.unlink()
        cache.rmdir()
    assert verify(honest, tmp_path, keys, moved)["ok"]


# ---- other cache tricks ----------------------------------------------------------------

def make_cache(repo: Path, name: str, data: bytes) -> Path:
    folder = repo / "app" / "__pycache__"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data)
    return folder / name


def honest_cache_bytes(repo: Path, flags: int = 0, opt: int = 0) -> bytes:
    src = (repo / "app" / "worker.py").resolve()
    return release.expected_cache(src.read_bytes(), src, src.stat().st_mtime, flags, opt)


@pytest.mark.parametrize("change", ["truncated", "short", "extra_byte", "flags_2", "flags_4",
                                    "wrong_magic", "wrong_opt", "empty", "same_size_other_code",
                                    "flags_2_hash_layout"])
def test_damaged_or_odd_caches_are_caught(repo, tmp_path, keys, honest, change):
    good = honest_cache_bytes(repo)
    name = f"worker.{TAG}.pyc"
    data = {"truncated": good[:-1], "short": good[:10], "extra_byte": good + b"\0",
            "flags_2": good[:4] + (2).to_bytes(4, "little") + good[8:],
            "flags_4": good[:4] + (4).to_bytes(4, "little") + good[8:],
            "wrong_magic": b"\0\0\r\n" + good[4:], "wrong_opt": honest_cache_bytes(repo),
            "empty": b"",
            "same_size_other_code": good[:-8] + bytes(x ^ 1 for x in good[-8:]),
            "flags_2_hash_layout": (lambda h: h[:4] + (2).to_bytes(4, "little") + h[8:])(
                honest_cache_bytes(repo, flags=3))}[change]
    if change == "wrong_opt":
        name = f"worker.{TAG}.opt-2.pyc"  # an -O0 cache saved under the -OO name
        data = honest_cache_bytes(repo, opt=0)
        if data == honest_cache_bytes(repo, opt=2):
            pytest.skip("this source compiles the same at every optimisation level")
    make_cache(repo, name, data)
    report = verify(honest, tmp_path, keys, repo)
    assert not report["ok"]
    assert any(p.startswith(f"app/__pycache__/{name}: compiled cache") for p in
               report["problems"])


def test_cache_from_another_python_is_a_problem(repo, tmp_path, keys, honest):
    make_cache(repo, "worker.cpython-311.pyc" if TAG != "cpython-311" else "worker.cpython-312.pyc",
               b"x" * 40)
    report = verify(honest, tmp_path, keys, repo)
    assert any("compiled by another Python" in p and FIX in p for p in report["problems"])


def test_linked_cache_file_or_folder_is_a_problem(repo, tmp_path, keys, honest):
    real = tmp_path / "real.pyc"
    real.write_bytes(honest_cache_bytes(repo))
    (repo / "app" / "__pycache__").mkdir()
    os.symlink(real, repo / "app" / "__pycache__" / f"worker.{TAG}.pyc")
    report = verify(honest, tmp_path, keys, repo)
    assert any("not a plain file inside a Python cache folder" in p for p in report["problems"])

    os.unlink(repo / "app" / "__pycache__" / f"worker.{TAG}.pyc")
    (repo / "app" / "__pycache__").rmdir()
    elsewhere = tmp_path / "cachedir"
    elsewhere.mkdir()
    os.symlink(elsewhere, repo / "app" / "__pycache__")
    report = verify(honest, tmp_path, keys, repo)
    assert any(p.startswith("app/__pycache__: link not in the signed release")
               for p in report["problems"])


def test_caches_python_never_loads_are_one_note(repo, tmp_path, keys, honest):
    make_cache(repo, "gone.%s.pyc" % TAG, b"old module no longer in the release")
    make_cache(repo, f"worker.{TAG}-pytest-9.2.0.pyc", b"pytest's own rewrite cache")
    make_cache(repo, "README.txt", b"hello")
    report = verify(honest, tmp_path, keys, repo)
    assert report["ok"], report["problems"]
    assert ("app/__pycache__/: 3 cache file(s) Python won't load for this release (other "
            "tools, or files no longer in the release); left alone") in report["notes"]


# ---- anything importable that isn't in the release --------------------------------------

@pytest.mark.parametrize("suffix", sorted(set(release.CODE_SUFFIXES)))
@pytest.mark.parametrize("where", ["", "app/"])
def test_every_importable_ending_is_a_problem(repo, tmp_path, keys, honest, suffix, where):
    (repo / f"{where}hashlib{suffix}").write_bytes(b"\x7fELF not really")
    report = verify(honest, tmp_path, keys, repo)
    assert not report["ok"]
    assert f"{where}hashlib{suffix}: Python code not in the signed release (Python can " \
           "import it)" in report["problems"]


def test_code_suffix_list_covers_what_this_python_imports():
    import importlib.machinery
    for suffix in importlib.machinery.all_suffixes():
        assert suffix.endswith(release.CODE_SUFFIXES)
    assert ".pth" in release.CODE_SUFFIXES


def test_package_folder_of_compiled_code_is_caught(repo, tmp_path, keys, honest):
    """A real (not linked) folder `json/` holding only an `__init__.pyc`."""
    (repo / "json").mkdir()
    evil = tmp_path / "evil.py"
    evil.write_text(EVIL)
    py_compile.compile(str(evil), cfile=str(repo / "json" / "__init__.pyc"))
    assert "EVIL CODE RAN" in run_worker(repo)
    report = verify(honest, tmp_path, keys, repo)
    assert "json/__init__.pyc: Python code not in the signed release (Python can import it)" \
        in report["problems"]


def test_linked_plain_file_is_a_problem_but_ignored_folders_stay_ignored(repo, tmp_path, keys,
                                                                         honest):
    (tmp_path / "notes.txt").write_text("hi")
    os.symlink(tmp_path / "notes.txt", repo / "notes.txt")
    report = verify(honest, tmp_path, keys, repo)
    assert any(p.startswith("notes.txt: link not in the signed release")
               for p in report["problems"])
    os.unlink(repo / "notes.txt")
    # data/ (keys, databases) may be a link to another disk, and is never imported.
    (repo / "data").rename(tmp_path / "data-elsewhere")
    os.symlink(tmp_path / "data-elsewhere", repo / "data")
    assert verify(honest, tmp_path, keys, repo)["ok"]


# ---- reading the folder safely ----------------------------------------------------------

def test_unreadable_folder_is_a_problem_not_a_silent_skip(repo, tmp_path, keys, honest,
                                                          monkeypatch):
    (repo / "app" / "sub").mkdir()
    (repo / "app" / "sub" / "x.txt").write_text("x")
    real = os.scandir

    def scandir(path):
        if str(path).endswith("sub"):
            raise PermissionError(13, "Permission denied")
        return real(path)
    monkeypatch.setattr(release.os, "scandir", scandir)
    report = verify(honest, tmp_path, keys, repo)
    assert any(p.startswith("app/sub/: can't read this folder") for p in report["problems"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any folder")
def test_unreadable_folder_for_real(repo, tmp_path, keys, honest):
    (repo / "locked").mkdir()
    (repo / "locked" / "x.txt").write_text("x")
    os.chmod(repo / "locked", 0)
    try:
        report = verify(honest, tmp_path, keys, repo)
    finally:
        os.chmod(repo / "locked", 0o755)
    assert any(p.startswith("locked/: can't read this folder") for p in report["problems"])


def test_huge_folder_stops_with_a_problem(repo, tmp_path, keys, honest, monkeypatch):
    for i in range(20):
        (repo / f"log{i}.txt").write_text("x")
    monkeypatch.setattr(release, "MAX_FOLDER_ENTRIES", 10)
    report = verify(honest, tmp_path, keys, repo)
    assert any("more than 10 files and folders" in p for p in report["problems"])


def test_many_extra_files_are_summarised(repo, tmp_path, keys, honest, monkeypatch):
    for i in range(10):
        (repo / f"log{i}.txt").write_text("x")
    monkeypatch.setattr(release, "MAX_LISTED_NOTES", 3)
    report = verify(honest, tmp_path, keys, repo)
    assert report["ok"]
    extra = [n for n in report["notes"] if n.startswith("log")]
    assert len(extra) == 3
    assert "... and 7 more extra files (not code)" in report["notes"]


def test_listed_file_replaced_by_a_pipe_does_not_hang(repo, tmp_path, keys, honest):
    (repo / "README.md").unlink()
    os.mkfifo(repo / "README.md")
    report = verify(honest, tmp_path, keys, repo)  # opened without blocking, never read
    assert "README.md: not a plain file (a pipe, device or folder)" in report["problems"]


def test_huge_listed_file_is_not_read(repo, tmp_path, keys, honest, monkeypatch):
    with open(repo / "README.md", "r+b") as fh:
        fh.truncate(5 * 1024 ** 3)  # sparse: takes no disk space
    read_sizes = []
    real = release.os.fdopen

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        def fileno(self):
            return self.fh.fileno()

        def read(self, n=-1):
            data = self.fh.read(n)
            read_sizes.append(len(data))
            return data
    monkeypatch.setattr(release.os, "fdopen", lambda fd, *a, **k: Spy(real(fd, *a, **k)))
    report = verify(honest, tmp_path, keys, repo)
    assert "README.md: contents differ from the signed release" in report["problems"]
    assert read_sizes and max(read_sizes) < 1024 ** 2  # only small files and cache headers


def test_hostile_file_names_print_cleanly(repo, tmp_path, keys, honest):
    (repo / "run\x1b[2J\x1b[31mOK.log").write_text("x")
    (repo / "evil\x1b]0;owned\x07.py").write_text("x")
    os.close(os.open(bytes(repo) + b"/bad\xff\xfename.pyc", os.O_CREAT | os.O_WRONLY, 0o644))
    report = verify(honest, tmp_path, keys, repo)
    text = release.report_text(report)
    assert "\x1b" not in text and "\x07" not in text
    text.encode("utf-8")  # no lone surrogates left to crash print()
    assert not report["ok"]
    assert sum("Python code not in the signed release" in p for p in report["problems"]) == 2


@pytest.mark.parametrize("version", ["0.5.57\n", " 0.5.57", "0.5.57 ", "0.5.​57"])
def test_versions_with_hidden_characters_are_refused(version):
    problems = release.check_manifest({
        "format": release.MANIFEST_FORMAT, "rule": release.RULE, "name": "bnh-worker",
        "version": version, "source_commit": "a" * 40, "made_at": "x",
        "files": [{"path": "a.py", "sha256": "0" * 64, "size": 0}]})
    assert "release version is not a valid version number" in problems


def test_cli_prints_problems_and_exits_1(repo, tmp_path, keys, honest, capsys):
    (repo / "json.so").write_bytes(b"x")
    code = release.main(["verify", str(honest), "--signers",
                         str(signer_list(tmp_path, keys)), "--root", str(repo)])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  json.so: Python code not in the signed release" in out
    assert out.rstrip().endswith("NOT OK: don't run this release")
