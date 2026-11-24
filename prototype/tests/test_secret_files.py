"""Secret and pinned files on disk (`secret-file-v1`, code audit eighth pass).

Each finding of the pass is replayed here: the ledger readable by every user (with a real
second account when the tests run as root), a worker key or pinned key other users can
read or change, a named pipe in place of a key hanging the worker or coordinator, and a
pinned key swapped through a folder anyone can write to.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from fastapi.testclient import TestClient

import demo_worker
from app import secret_file
from app.config import Settings
from app.ledger import Ledger
from app.main import create_app
from app.secret_file import UnsafeFileError
from app.signing import ManifestSigner, SigningKeyError

POSIX = os.name == "posix"
ROOT = POSIX and os.geteuid() == 0
GOOD_KEY = "bnhw_" + "A" * 43
OPERATOR = "op-" + "x" * 40

posix_only = pytest.mark.skipif(not POSIX, reason="file permissions are POSIX-only")


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def finishes(action: Callable[[], Any], seconds: float = 5.0) -> BaseException | None:
    """Run `action` in a thread; fail if it is still running after `seconds` (a hang).
    Returns the exception it raised, if any."""
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            action()
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 (SystemExit included on purpose)
            outcome.append(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "hung (a named pipe in place of a file?)"
    return outcome[0]


def fifo(path: Path) -> Path:
    os.mkfifo(path)
    return path


def key_server(public_key: str) -> httpx.Client:
    return httpx.Client(base_url="https://coord.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"public_key": public_key})))


# -- the module itself -------------------------------------------------------------------

@posix_only
def test_new_files_are_owner_only_in_an_owner_only_folder(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        target = tmp_path / "new" / "deeper" / "worker.key"
        secret_file.write_new(target, b"k\n", "worker key")
    finally:
        os.umask(old)
    assert mode(target) == 0o600
    assert mode(target.parent) == 0o700
    assert target.read_bytes() == b"k\n"


def test_write_new_never_replaces_or_follows_a_planted_link(tmp_path: Path) -> None:
    existing = tmp_path / "a.key"
    existing.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        secret_file.write_new(existing, b"new", "key")
    assert existing.read_bytes() == b"keep"
    victim = tmp_path / "victim"
    (tmp_path / "planted.key").symlink_to(victim)  # dangling link to where we'd write
    with pytest.raises(FileExistsError):
        secret_file.write_new(tmp_path / "planted.key", b"secret", "key")
    assert not victim.exists()


@posix_only
def test_replace_swaps_in_an_owner_only_file_and_ignores_a_planted_temp_link(
        tmp_path: Path) -> None:
    pin = tmp_path / "coordinator.pub"
    secret_file.write_new(pin, b"old\n", "pin")
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    (tmp_path / "coordinator.pub.tmp").symlink_to(victim)
    secret_file.replace(pin, b"new\n", "pin")
    assert pin.read_bytes() == b"new\n" and mode(pin) == 0o600
    assert victim.read_bytes() == b"untouched"


@posix_only
@pytest.mark.parametrize("bits", [0o640, 0o604, 0o660, 0o606, 0o644])
def test_a_secret_others_can_read_or_change_is_refused_with_the_fix(
        tmp_path: Path, bits: int) -> None:
    key = tmp_path / "worker.key"
    key.write_bytes(b"k")
    key.chmod(bits)
    with pytest.raises(UnsafeFileError, match=r"chmod 600"):
        secret_file.read(key, "worker key", secret=True)
    key.chmod(0o600)
    assert secret_file.read(key, "worker key", secret=True) == b"k"


@posix_only
@pytest.mark.parametrize("bits", [0o620, 0o602, 0o666])
def test_a_pin_others_can_change_is_refused_but_readable_is_fine(
        tmp_path: Path, bits: int) -> None:
    pin = tmp_path / "coordinator.pub"
    pin.write_bytes(b"ab")
    pin.chmod(bits)
    with pytest.raises(UnsafeFileError, match=r"can be changed by other users"):
        secret_file.read(pin, "pin", secret=False)
    pin.chmod(0o644)  # a public key may be readable by everyone
    assert secret_file.read(pin, "pin", secret=False) == b"ab"


@posix_only
@pytest.mark.parametrize("bits", [0o777, 0o1777, 0o775, 0o757])
def test_a_folder_others_can_change_is_refused(tmp_path: Path, bits: int) -> None:
    folder = tmp_path / "shared"
    folder.mkdir()
    key = folder / "worker.key"
    key.write_bytes(b"k")
    key.chmod(0o600)
    folder.chmod(bits)
    with pytest.raises(UnsafeFileError, match=r"chmod 700"):
        secret_file.read(key, "worker key", secret=True)
    with pytest.raises(UnsafeFileError, match=r"chmod 700"):
        secret_file.write_new(folder / "other.key", b"k", "worker key")
    assert not (folder / "other.key").exists()
    folder.chmod(0o755)  # readable by all is fine; only changing it matters
    assert secret_file.read(key, "worker key", secret=True) == b"k"


@posix_only
def test_a_named_pipe_is_refused_at_once_instead_of_hanging(tmp_path: Path) -> None:
    pipe = fifo(tmp_path / "worker.key")
    os.chmod(pipe, 0o600)
    error = finishes(lambda: secret_file.read(pipe, "worker key", secret=True))
    assert isinstance(error, UnsafeFileError) and "not a regular file" in str(error)


def test_folders_devices_and_big_files_are_refused(tmp_path: Path) -> None:
    with pytest.raises(UnsafeFileError, match="not a regular file"):
        secret_file.read(tmp_path, "key", secret=False)
    if Path("/dev/zero").exists():
        error = finishes(lambda: secret_file.read("/dev/zero", "key", secret=False,
                                                  folder=False))
        assert isinstance(error, UnsafeFileError)
    big = tmp_path / "big.key"
    big.write_bytes(b"a" * (secret_file.MAX_KEY_BYTES + 1))
    big.chmod(0o600)
    with pytest.raises(UnsafeFileError, match="too big"):
        secret_file.read(big, "key", secret=True)


def test_missing_file_is_reported_as_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        secret_file.read(tmp_path / "nope", "key", secret=True)


def test_key_text_must_be_plain_ascii(tmp_path: Path) -> None:
    key = tmp_path / "k"
    secret_file.write_new(key, "bnhw_é\n".encode(), "key")
    with pytest.raises(UnsafeFileError, match="plain-text"):
        secret_file.read_text(key, "key", secret=True)


@pytest.mark.skipif(not ROOT, reason="needs root to give a file to another user")
def test_a_file_or_folder_belonging_to_another_user_is_refused(tmp_path: Path) -> None:
    key = tmp_path / "worker.key"
    secret_file.write_new(key, b"k", "worker key")
    os.chown(key, 65534, 65534)
    # Root may read it, but the file isn't ours: someone else controls its contents.
    # (Root itself is allowed to own files; here we ARE root, so use a sub-folder.)
    folder = tmp_path / "theirs"
    folder.mkdir(mode=0o755)
    os.chown(folder, 65534, 65534)
    with pytest.raises(UnsafeFileError, match="belongs to another user"):
        secret_file.check_folder(folder, "worker key")
    with pytest.raises(UnsafeFileError, match="belongs to another user"):
        secret_file.read(key, "worker key", secret=True, folder=False)


# -- finding 1: the ledger was readable by every user --------------------------------------

def add_hidden_test(home: Path) -> str:
    settings = Settings(database_path=home / "data" / "prototype.sqlite3",
                        operator_token=OPERATOR)
    client = TestClient(create_app(settings))
    expected = hashlib.sha256(b"hidden-test-input-7").hexdigest()
    reply = client.post("/v1/challenges", json={"payload": "hidden-test-input-7",
                                                "expected_result": expected},
                        headers={"X-Operator-Token": OPERATOR})
    assert reply.status_code == 200, reply.text
    return expected


@posix_only
def test_a_new_ledger_and_its_side_files_are_owner_only(tmp_path: Path) -> None:
    old = os.umask(0o022)  # the usual umask: SQLite alone made the ledger 0644
    try:
        add_hidden_test(tmp_path)
        ledger = Ledger(tmp_path / "data" / "prototype.sqlite3")
        with ledger._tx() as db:  # holds the database open, so -wal and -shm exist
            db.execute("SELECT count(*) FROM jobs").fetchone()
            sides = [p for p in (tmp_path / "data").iterdir() if p.name.endswith(("-wal",
                                                                                 "-shm"))]
            assert sides, "expected SQLite's -wal/-shm files while open"
            assert all(mode(p) == 0o600 for p in sides), [(p.name, oct(mode(p))) for p in sides]
    finally:
        os.umask(old)
    assert mode(tmp_path / "data") == 0o700
    assert mode(tmp_path / "data" / "prototype.sqlite3") == 0o600


@pytest.mark.skipif(not ROOT or shutil.which("setpriv") is None,
                    reason="needs root and setpriv to act as a second, ordinary user")
def test_another_user_on_the_machine_can_no_longer_copy_the_hidden_answers() -> None:
    """The real thing: as the `nobody` account, try to copy the ledger and read the
    hidden test's answer. Before `secret-file-v1` this printed the answer."""
    # A folder other users can enter (as many home folders are), like a coordinator's.
    base = Path(tempfile.mkdtemp(prefix="bnh-other-user-", dir="/tmp"))
    probe_copy = base.with_name(base.name + "-probe.sqlite3")
    try:
        base.chmod(0o755)
        old = os.umask(0o022)
        try:
            expected = add_hidden_test(base)
        finally:
            os.umask(old)
        probe = ("import shutil, sqlite3, sys\n"
                 "try:\n"
                 "    shutil.copy(sys.argv[1], sys.argv[2])\n"
                 "except OSError as exc:\n"
                 "    print('REFUSED', exc.errno); raise SystemExit(0)\n"
                 "db = sqlite3.connect(sys.argv[2])\n"
                 "print(db.execute(\"select expected_result from jobs where "
                 "job_kind='challenge'\").fetchall())\n")
        out = subprocess.run(
            ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", sys.executable,
             "-I", "-c", probe, str(base / "data" / "prototype.sqlite3"), str(probe_copy)],
            capture_output=True, text=True, timeout=60)
        assert expected not in out.stdout
        assert out.stdout.startswith("REFUSED"), out.stdout + out.stderr
    finally:
        shutil.rmtree(base, ignore_errors=True)
        probe_copy.unlink(missing_ok=True)


@posix_only
def test_an_older_world_readable_ledger_is_refused_with_the_fix(tmp_path: Path) -> None:
    path = tmp_path / "prototype.sqlite3"
    with sqlite3.connect(path) as db:  # as SQLite made it before this pass
        db.execute("CREATE TABLE t (x)")
    path.chmod(0o644)
    with pytest.raises(UnsafeFileError, match=r"can be read by other users; run: chmod 600"):
        Ledger(path)
    path.chmod(0o600)
    Ledger(path)  # fixed: starts and migrates


@posix_only
@pytest.mark.parametrize("side", ["-wal", "-shm", "-journal"])
def test_a_readable_side_file_is_refused_too(tmp_path: Path, side: str) -> None:
    path = tmp_path / "prototype.sqlite3"
    Ledger(path)
    extra = tmp_path / f"prototype.sqlite3{side}"
    extra.write_bytes(b"")
    extra.chmod(0o644)
    with pytest.raises(UnsafeFileError, match=side[1:]):
        Ledger(path)


@posix_only
def test_a_ledger_in_a_folder_others_can_change_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    folder.chmod(0o777)
    with pytest.raises(UnsafeFileError, match=r"chmod 700"):
        Ledger(folder / "prototype.sqlite3")
    assert not (folder / "prototype.sqlite3").exists()  # refused before creating anything


def test_read_only_and_untrusted_opens_are_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "prototype.sqlite3"
    Ledger(path)
    assert Ledger(path, read_only=True).read_only


# -- finding 2: the signing key check looked, then read again by name ----------------------

@posix_only
def test_a_pipe_in_place_of_the_signing_key_no_longer_hangs(tmp_path: Path) -> None:
    pipe = fifo(tmp_path / "coordinator-signing.key")
    os.chmod(pipe, 0o600)
    error = finishes(lambda: ManifestSigner.load_or_create(pipe))
    assert isinstance(error, SigningKeyError) and "not a regular file" in str(error)


@posix_only
def test_a_new_signing_key_folder_is_owner_only(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        ManifestSigner.load_or_create(tmp_path / "keys" / "coordinator-signing.key")
    finally:
        os.umask(old)
    assert mode(tmp_path / "keys") == 0o700
    assert mode(tmp_path / "keys" / "coordinator-signing.key") == 0o600


# -- findings 3 and 4: the worker's key and pins ------------------------------------------

def run(action: Callable[[], Any]) -> str:
    """The worker step must stop cleanly (SystemExit) or return; returns the stop text."""
    error = finishes(action)
    if error is None:
        return ""
    assert isinstance(error, SystemExit), repr(error)
    return str(error.code)


@posix_only
def test_a_worker_key_other_users_can_read_stops_the_worker(tmp_path: Path) -> None:
    key = tmp_path / "worker-rig-1.key"
    key.write_text(GOOD_KEY + "\n")
    key.chmod(0o644)  # e.g. copied from another machine with plain `cp`
    calls: list[str] = []
    client = httpx.Client(base_url="https://coord.test", transport=httpx.MockTransport(
        lambda r: calls.append(r.url.path) or httpx.Response(200, json={})))
    text = run(lambda: demo_worker.load_or_enroll(client, "rig-1", key, None, ["demo:sha256"]))
    assert "can be read by other users" in text and "chmod 600" in text
    assert calls == []  # the key was never sent anywhere


@posix_only
def test_a_pipe_in_place_of_the_worker_key_no_longer_hangs(tmp_path: Path) -> None:
    pipe = fifo(tmp_path / "worker-rig-1.key")
    os.chmod(pipe, 0o600)
    text = run(lambda: demo_worker.load_or_enroll(key_server("00" * 32), "rig-1", pipe, None,
                                                  ["demo:sha256"]))
    assert "not a regular file" in text


def test_a_worker_key_file_that_holds_something_else_stops_the_worker(tmp_path: Path) -> None:
    key = tmp_path / "worker-rig-1.key"
    secret_file.write_new(key, b"-----BEGIN PRIVATE KEY-----\n", "worker key")
    text = run(lambda: demo_worker.load_or_enroll(key_server("00" * 32), "rig-1", key, None,
                                                  ["demo:sha256"]))
    assert "doesn't hold a worker key" in text


@posix_only
def test_an_enrolled_worker_key_is_saved_owner_only_in_an_owner_only_folder(
        tmp_path: Path) -> None:
    key = tmp_path / "data" / "worker-rig-1.key"
    client = httpx.Client(base_url="https://coord.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"worker_id": "rig-1", "worker_key": GOOD_KEY})))
    old = os.umask(0o022)
    try:
        assert demo_worker.load_or_enroll(client, "rig-1", key, "bnhe_code",
                                          ["demo:sha256"]) == GOOD_KEY
    finally:
        os.umask(old)
    assert mode(key) == 0o600 and mode(key.parent) == 0o700
    # and it is read back on the next run
    assert demo_worker.read_worker_key(key) == GOOD_KEY


@posix_only
def test_an_existing_ordinary_data_folder_still_works(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o755)  # what older versions made
    data.chmod(0o755)
    pub = ManifestSigner.load_or_create(tmp_path / "c.key").public_key_hex
    assert demo_worker.load_or_pin_coordinator_key(key_server(pub), data / "coordinator.pub") == pub
    assert demo_worker.load_or_pin_coordinator_key(key_server(pub), data / "coordinator.pub") == pub
    assert mode(data) == 0o755  # an existing folder is checked, never changed


@posix_only
def test_a_pin_swapped_through_a_shared_folder_is_not_trusted(tmp_path: Path) -> None:
    """Before this pass: the pin sat in a folder anyone could write to, another user
    replaced it with their own key, and the worker trusted their coordinator."""
    real = ManifestSigner.load_or_create(tmp_path / "real.key").public_key_hex
    evil = ManifestSigner.load_or_create(tmp_path / "evil.key").public_key_hex
    shared = tmp_path / "shared"
    shared.mkdir()
    pin = shared / "coordinator.pub"
    pin.write_text(real + "\n")
    pin.chmod(0o600)
    shared.chmod(0o777)
    pin.unlink()  # another user can delete a file in a folder anyone can change...
    pin.write_text(evil + "\n")  # ...and put their own in its place
    pin.chmod(0o600)
    text = run(lambda: demo_worker.load_or_pin_coordinator_key(key_server(evil), pin))
    assert "other users can change the folder" in text and "chmod 700" in text


@posix_only
def test_a_pin_others_can_change_stops_the_worker(tmp_path: Path) -> None:
    pub = ManifestSigner.load_or_create(tmp_path / "c.key").public_key_hex
    pin = tmp_path / "coordinator.pub"
    pin.write_text(pub + "\n")
    pin.chmod(0o666)
    text = run(lambda: demo_worker.load_or_pin_coordinator_key(key_server(pub), pin))
    assert "can be changed by other users" in text


@pytest.mark.parametrize("name", ["coordinator.pub", "coordinator-handover.pub"])
def test_a_pipe_or_garbage_in_place_of_a_pin_stops_the_worker(tmp_path: Path,
                                                               name: str) -> None:
    pub = ManifestSigner.load_or_create(tmp_path / "c.key").public_key_hex
    pin = tmp_path / "coordinator.pub"
    target = tmp_path / name
    if POSIX:
        os.chmod(fifo(target), 0o600)
        assert "not a regular file" in run(
            lambda: demo_worker.load_or_pin_coordinator_key(key_server(pub), pin))
        target.unlink()
    secret_file.write_new(target, b"not a key\n", "pin")
    assert "doesn't hold a key" in run(
        lambda: demo_worker.load_or_pin_coordinator_key(key_server(pub), pin))


@posix_only
def test_a_rotated_pin_stays_owner_only_and_ignores_a_planted_temp_link(
        tmp_path: Path) -> None:
    pin = tmp_path / "data" / "coordinator.pub"
    demo_worker.save_key(pin, "aa" * 32)
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (pin.parent / "coordinator.pub.tmp").symlink_to(victim)
    old = os.umask(0o022)
    try:
        demo_worker.replace_key_file(pin, "bb" * 32)
    finally:
        os.umask(old)
    assert pin.read_text() == "bb" * 32 + "\n" and mode(pin) == 0o600
    assert victim.read_text() == "untouched"


@posix_only
def test_first_contact_pins_owner_only(tmp_path: Path) -> None:
    pub = ManifestSigner.load_or_create(tmp_path / "c.key").public_key_hex
    pin = tmp_path / "data" / "coordinator.pub"
    old = os.umask(0o022)
    try:
        assert demo_worker.load_or_pin_coordinator_key(key_server(pub), pin) == pub
    finally:
        os.umask(old)
    assert mode(pin) == 0o600 and mode(pin.parent) == 0o700
