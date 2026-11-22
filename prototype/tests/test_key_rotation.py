"""Planned coordinator key rotation (threat model #9 follow-up).

Workers pin the coordinator's public key and used to stop on ANY change, so the key could
never be replaced without visiting every rig. A rotation writes a handover statement signed
by both the old and the new key; workers follow a valid chain of handovers and refuse
anything else. Jobs queued under the old key are re-signed when claimed.
"""
from __future__ import annotations

import copy
import json
import os
import stat
from pathlib import Path

import pytest

from app import key_rotation
from app.config import Settings
from app.key_rotation import (RotationError, check_history, follow_chain, history_path,
                              pending_path, rotate)
from app.main import create_app
from app.schemas import SIGNED_FIELDS
from app.signing import ManifestSigner, SigningKeyError, fingerprint, verify_manifest
from demo_worker import ManifestError, check_manifest, load_or_pin_coordinator_key
from tests.auth_helpers import OPERATOR, WorkerClient, enroll

T0 = 1_800_000_000


def make(tmp_path: Path, name: str = "db", keys: dict[str, str] | None = None) -> WorkerClient:
    settings = Settings(database_path=tmp_path / name / "db.sqlite3", operator_token=OPERATOR)
    return WorkerClient(create_app(settings, clock=lambda: T0), keys)


def key_file(tmp_path: Path, name: str = "db") -> Path:
    return tmp_path / name / "coordinator-signing.key"


def public_key(client: WorkerClient) -> str:
    return client.get("/v1/coordinator/key").json()["public_key"]


def test_rotation_writes_a_handover_signed_by_both_keys_and_drops_the_old_key(
        tmp_path: Path) -> None:
    client = make(tmp_path)
    old_pub = public_key(client)
    path = key_file(tmp_path)
    old_pem = path.read_bytes()

    entry = rotate(path, T0 + 10)

    assert entry["sequence"] == 1 and entry["rotated_at"] == T0 + 10
    assert entry["old_public_key"] == old_pub
    new_signer = ManifestSigner.load_or_create(path)
    assert entry["new_public_key"] == new_signer.public_key_hex != old_pub
    fields = key_rotation.statement_fields(entry)
    assert verify_manifest(fields, entry["signatures"]["old"], old_pub)
    assert verify_manifest(fields, entry["signatures"]["new"], new_signer.public_key_hex)
    # Old private key is gone, new one is private, nothing half-done is left behind.
    assert path.read_bytes() != old_pem
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not pending_path(path).exists()
    # The history file is public: it holds public keys and signatures, never a private key.
    text = history_path(path).read_text()
    assert "PRIVATE" not in text and old_pem.decode() not in text
    assert json.loads(text)["rotations"] == [entry]


def test_restarted_coordinator_serves_the_new_key_with_its_handover_chain(
        tmp_path: Path) -> None:
    old_pub = public_key(make(tmp_path))
    rotate(key_file(tmp_path), T0)
    rotate(key_file(tmp_path), T0 + 1)
    info = make(tmp_path).get("/v1/coordinator/key").json()
    assert [e["sequence"] for e in info["rotations"]] == [1, 2]
    assert follow_chain(info["rotations"], old_pub, info["public_key"])[0] == old_pub
    health = make(tmp_path).get("/v1/operator/health",
                                headers={"X-Operator-Token": OPERATOR}).json()
    assert health["signing_key_rotations"] == 2


def test_worker_follows_a_signed_handover_and_updates_its_pin(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old_pub = public_key(make(tmp_path))
    pin = tmp_path / "worker" / "coordinator.pub"
    assert load_or_pin_coordinator_key(make(tmp_path), pin) == old_pub
    rotate(key_file(tmp_path), T0)
    rotate(key_file(tmp_path), T0 + 1)  # the worker was offline for two rotations
    client = make(tmp_path)
    new_pub = public_key(client)
    capsys.readouterr()

    assert load_or_pin_coordinator_key(client, pin) == new_pub
    assert pin.read_text().strip() == new_pub
    assert stat.S_IMODE(pin.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "rotated" in out and fingerprint(old_pub) in out and fingerprint(new_pub) in out
    assert not pin.with_name(pin.name + ".tmp").exists()
    assert load_or_pin_coordinator_key(client, pin) == new_pub  # next run: plain pin


def test_job_queued_before_a_rotation_is_re_signed_on_claim(tmp_path: Path) -> None:
    keys: dict[str, str] = {}
    client = make(tmp_path, keys=keys)
    old_pub = public_key(client)
    enroll(client, "wk-1")
    job_id = client.post("/v1/jobs", json={"payload": "queued before rotation"}).json()["job_id"]
    old_signature = client.app.state.ledger.get_job(job_id)["signature"]

    rotate(key_file(tmp_path), T0)
    client = make(tmp_path, keys=keys)
    new_pub = public_key(client)
    job = client.post("/v1/jobs/next", params={"worker_id": "wk-1"}).json()

    assert job["job_id"] == job_id and job["signature"] != old_signature
    check_manifest(job, new_pub, now=T0)  # a worker on the new key accepts it
    assert client.app.state.ledger.get_job(job_id)["signature"] == job["signature"]
    resigned = [e for e in client.app.state.ledger.events(job_id) if e["event"] == "job_resigned"]
    assert len(resigned) == 1
    assert resigned[0]["detail"] == f"{fingerprint(old_pub)} -> {fingerprint(new_pub)}"


def test_a_job_signed_by_no_key_of_ours_is_never_re_signed(tmp_path: Path) -> None:
    """Someone with write access to the database inserts a job signed by their own key.
    The coordinator must not 'bless' it with a fresh signature."""
    client = make(tmp_path)
    enroll(client, "wk-1")
    job_id = client.post("/v1/jobs", json={"payload": "real"}).json()["job_id"]
    ledger = client.app.state.ledger
    attacker = ManifestSigner.load_or_create(tmp_path / "attacker.key")
    row = ledger.get_job(job_id)
    forged = {k: row[k] for k in SIGNED_FIELDS} | {"reward_points": 999}
    with ledger._tx() as db:  # direct database edit, as an attacker would
        db.execute("UPDATE jobs SET reward_points=999, signature=? WHERE job_id=?",
                   (attacker.sign(forged), job_id))
    rotate(key_file(tmp_path), T0)
    client = make(tmp_path, keys=client.worker_keys)
    job = client.post("/v1/jobs/next", params={"worker_id": "wk-1"}).json()
    assert job["signature"] == attacker.sign(forged)
    with pytest.raises(ManifestError, match="bad signature"):
        check_manifest(job, public_key(client), now=T0)
    assert not [e for e in ledger.events(job_id) if e["event"] == "job_resigned"]


def _chain(tmp_path: Path, rotations: int = 1) -> tuple[str, str, list[dict]]:
    old_pub = public_key(make(tmp_path))
    for i in range(rotations):
        rotate(key_file(tmp_path), T0 + i)
    info = make(tmp_path).get("/v1/coordinator/key").json()
    return old_pub, info["public_key"], info["rotations"]


def test_forged_or_broken_handovers_are_refused(tmp_path: Path) -> None:
    old_pub, new_pub, chain = _chain(tmp_path, 2)
    follow_chain(chain, old_pub, new_pub)  # the genuine chain works
    attacker = ManifestSigner.load_or_create(tmp_path / "attacker.key")

    # An impostor without the old private key hands over to its own key.
    fake = {"purpose": key_rotation.ROTATION_PURPOSE, "sequence": 1,
            "old_public_key": old_pub, "new_public_key": attacker.public_key_hex,
            "rotated_at": T0}
    forged = fake | {"signatures": {"old": attacker.sign(fake), "new": attacker.sign(fake)}}
    with pytest.raises(RotationError, match="not signed by the old key"):
        follow_chain([forged], old_pub, attacker.public_key_hex)

    def broken(mutate) -> list[dict]:
        c = copy.deepcopy(chain)
        mutate(c)
        return c

    cases = {
        "not signed by the old key": lambda c: c[1].update(new_public_key=attacker.public_key_hex),
        "not signed by the new key": lambda c: c[0]["signatures"].update(
            new=c[0]["signatures"]["old"]),
        "numbered": lambda c: c[1].update(sequence=5),
        "wrong purpose": lambda c: c[0].update(purpose="bnh-reward-report/1"),
        "does not start from": lambda c: c[1].update(old_public_key=attacker.public_key_hex),
        "reuses an earlier key": lambda c: c[1].update(new_public_key=old_pub),
        "missing": lambda c: c[0].pop("signatures"),
        "not an object": lambda c: c.__setitem__(0, "hello"),
    }
    for message, mutate in cases.items():
        with pytest.raises(RotationError, match=message):
            check_history(broken(mutate))
    # A chain can't be walked backwards, and an unrelated pin finds no start.
    with pytest.raises(RotationError, match="no handover starts"):
        follow_chain(chain, new_pub, old_pub)
    with pytest.raises(RotationError, match="no handover starts"):
        follow_chain(chain, attacker.public_key_hex, new_pub)
    with pytest.raises(RotationError, match="end on"):
        follow_chain(chain[:1], old_pub, new_pub)


def test_worker_refuses_a_key_change_without_a_valid_handover(tmp_path: Path) -> None:
    old_pub = public_key(make(tmp_path))
    pin = tmp_path / "worker" / "coordinator.pub"
    load_or_pin_coordinator_key(make(tmp_path), pin)
    impostor = make(tmp_path, "impostor")  # own key, no handover from ours
    with pytest.raises(SystemExit, match="without a valid signed handover"):
        load_or_pin_coordinator_key(impostor, pin)
    # Even with our real public history copied in, the impostor's key isn't on the chain.
    rotate(key_file(tmp_path), T0)
    real_history = history_path(key_file(tmp_path)).read_text()
    history_path(key_file(tmp_path, "impostor")).write_text(real_history)
    with pytest.raises(SigningKeyError, match="doesn't match its handover history"):
        make(tmp_path, "impostor")
    assert pin.read_text().strip() == old_pub  # pin untouched after every refusal


def test_interrupted_rotation_is_detected_and_finished_not_repeated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_pub = public_key(make(tmp_path))
    path = key_file(tmp_path)
    real_replace = os.replace

    def crash_on_key_swap(src, dst):  # power cut right after the handover was saved
        if Path(dst) == path:
            raise OSError("simulated crash")
        return real_replace(src, dst)

    monkeypatch.setattr(key_rotation.os, "replace", crash_on_key_swap)
    with pytest.raises(OSError):
        rotate(path, T0)
    monkeypatch.setattr(key_rotation.os, "replace", real_replace)
    assert pending_path(path).exists()
    with pytest.raises(SigningKeyError, match="interrupted"):
        make(tmp_path)  # the coordinator won't start on a half-done rotation

    entry = rotate(path, T0 + 5)  # re-running finishes the same handover
    assert entry["sequence"] == 1 and entry["rotated_at"] == T0
    assert len(key_rotation.load_history(history_path(path))) == 1
    assert follow_chain([entry], old_pub, public_key(make(tmp_path)))


def test_crash_before_the_handover_is_saved_leaves_the_old_key_in_force(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_pub = public_key(make(tmp_path))
    path = key_file(tmp_path)

    def crash(*_: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(key_rotation, "_write_history", crash)
    with pytest.raises(OSError):
        rotate(path, T0)
    monkeypatch.undo()
    assert pending_path(path).exists()
    assert public_key(make(tmp_path)) == old_pub  # nothing changed for workers
    entry = rotate(path, T0 + 1)  # stale pending key is thrown away, a fresh rotation runs
    assert entry["old_public_key"] == old_pub and entry["sequence"] == 1
    assert not pending_path(path).exists()


def test_cli_rotates_and_shows_without_printing_private_material(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "c.key"
    assert key_rotation.main(["rotate", "--key-file", str(path)]) == 2  # no key yet
    ManifestSigner.load_or_create(path)
    capsys.readouterr()
    assert key_rotation.main(["show", "--key-file", str(path)]) == 0
    assert "no rotations yet" in capsys.readouterr().out
    assert key_rotation.main(["rotate", "--key-file", str(path)]) == 0
    out = capsys.readouterr().out
    signer = ManifestSigner.load_or_create(path)
    assert signer.public_key_hex in out and "PRIVATE" not in out
    assert key_rotation.main(["show", "--key-file", str(path)]) == 0
    assert f"-> {signer.fingerprint}" in capsys.readouterr().out
