"""Threat model #9: job manifests signed with the coordinator's Ed25519 key.

Before, every worker held the same HMAC secret, so any worker could sign fake jobs for the
others. Now only the coordinator holds the private key; workers verify with the public key.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import stat
from pathlib import Path

import pytest

from app import config, signing
from app.config import Settings
from app.main import create_app
from app.security import canonical_json
from app.signing import ManifestSigner, SigningKeyError, verify_manifest
from demo_worker import ManifestError, check_manifest, load_or_pin_coordinator_key
from tests.auth_helpers import OPERATOR, WorkerClient, enroll

T0 = 1_800_000_000
SIGNED = ("job_id", "task_type", "payload", "payload_hash", "reward_points", "expires_at")


def make(tmp_path: Path, key_name: str = "db") -> WorkerClient:
    settings = Settings(database_path=tmp_path / key_name / "db.sqlite3",
                        operator_token=OPERATOR)
    return WorkerClient(create_app(settings, clock=lambda: T0))


def claimed_job(client: WorkerClient, worker: str = "wk-1", payload: str = "hi") -> dict:
    enroll(client, worker)
    assert client.post("/v1/jobs", json={"payload": payload}).status_code == 200
    r = client.post("/v1/jobs/next", params={"worker_id": worker})
    assert r.status_code == 200
    return r.json()


def test_coordinator_creates_its_key_once_and_keeps_it_across_restarts(tmp_path: Path) -> None:
    client = make(tmp_path)
    key_file = tmp_path / "db" / "coordinator-signing.key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    info = client.get("/v1/coordinator/key").json()
    assert info["algorithm"] == "ed25519"
    assert re.fullmatch(r"[0-9a-f]{64}", info["public_key"])
    assert re.fullmatch(r"[0-9a-f]{4}(-[0-9a-f]{4}){3}", info["fingerprint"])
    job = claimed_job(client)
    assert job["signature"].startswith("ed25519:")

    restarted = make(tmp_path)  # same data folder: same key, old jobs still verify
    assert restarted.get("/v1/coordinator/key").json() == info
    check_manifest(job, restarted.get("/v1/coordinator/key").json()["public_key"], now=T0)


def test_coordinator_refuses_an_unsafe_or_broken_key_file(tmp_path: Path) -> None:
    make(tmp_path)
    key_file = tmp_path / "db" / "coordinator-signing.key"
    key_file.chmod(0o644)
    with pytest.raises(SigningKeyError, match="chmod 600"):
        make(tmp_path)
    key_file.write_text("not a key")
    key_file.chmod(0o600)
    with pytest.raises(SigningKeyError, match="PEM"):
        make(tmp_path)


def test_malformed_or_legacy_signatures_are_refused_not_crashed(tmp_path: Path) -> None:
    signer = ManifestSigner.load_or_create(tmp_path / "k.key")
    other = ManifestSigner.load_or_create(tmp_path / "other.key")
    fields = {"job_id": "j", "payload": "p", "reward_points": 1}
    good = signer.sign(fields)
    assert verify_manifest(fields, good, signer.public_key_hex)
    legacy_hmac = hmac.new(b"old-shared-secret-xxxxxxxxxxxx", canonical_json(fields),
                           hashlib.sha256).hexdigest()
    for bad in (legacy_hmac, good[len("ed25519:"):], "ed25519:" + "zz" * 64,
                "ed25519:" + good[-100:], "rsa:" + good[8:], "", other.sign(fields)):
        assert not verify_manifest(fields, bad, signer.public_key_hex), bad
    for bad_key in ("", "abc", "zz" * 32, other.public_key_hex):
        assert not verify_manifest(fields, good, bad_key)
    assert "PRIVATE" not in repr(signer) and signer.public_key_hex not in repr(signer)


def test_a_worker_cannot_forge_or_reuse_manifests(tmp_path: Path) -> None:
    client = make(tmp_path)
    pub = client.get("/v1/coordinator/key").json()["public_key"]
    real = claimed_job(client, "wk-1", "first")
    second = claimed_job(client, "wk-2", "second")
    # Everything a worker has: the public key and genuine signed jobs. Its own key can't
    # sign for the coordinator, and a genuine signature doesn't carry over to other fields.
    attacker = ManifestSigner.load_or_create(tmp_path / "attacker.key")
    forged = real | {"reward_points": 999}
    forged["signature"] = attacker.sign({k: forged[k] for k in SIGNED})
    with pytest.raises(ManifestError, match="bad signature"):
        check_manifest(forged, pub, now=T0)
    with pytest.raises(ManifestError, match="bad signature"):
        check_manifest(second | {"signature": real["signature"]}, pub, now=T0)
    check_manifest(real, pub, now=T0)
    check_manifest(second, pub, now=T0)


def test_demo_worker_pins_the_coordinator_key_and_stops_if_it_changes(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    client = make(tmp_path)
    info = client.get("/v1/coordinator/key").json()
    pin = tmp_path / "worker" / "coordinator.pub"
    assert load_or_pin_coordinator_key(client, pin) == info["public_key"]
    assert pin.read_text().strip() == info["public_key"]
    assert stat.S_IMODE(pin.stat().st_mode) == 0o600
    assert info["fingerprint"] in capsys.readouterr().out
    assert load_or_pin_coordinator_key(client, pin) == info["public_key"]  # second run

    impostor = make(tmp_path, "impostor")  # a different coordinator with its own key
    with pytest.raises(SystemExit, match="CHANGED"):
        load_or_pin_coordinator_key(impostor, pin)
    assert pin.read_text().strip() == info["public_key"]  # pin is never overwritten
    # A key the operator handed over directly wins over first-contact pinning.
    assert load_or_pin_coordinator_key(impostor, tmp_path / "x.pub",
                                       info["public_key"].upper()) == info["public_key"]
    assert not (tmp_path / "x.pub").exists()
    with pytest.raises(SystemExit, match="Bad coordinator key"):
        load_or_pin_coordinator_key(impostor, tmp_path / "x.pub", "abc123")


def test_settings_put_the_key_next_to_the_database_and_drop_the_old_secret(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "d" / "db.sqlite3"))
    monkeypatch.delenv("COORDINATOR_SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("PROTOTYPE_SECRET", raising=False)
    assert config.get_settings().resolved_signing_key_path() == \
        tmp_path / "d" / "coordinator-signing.key"
    monkeypatch.setenv("COORDINATOR_SIGNING_KEY_FILE", str(tmp_path / "keys" / "c.key"))
    monkeypatch.setenv("PROTOTYPE_SECRET", "left-over-from-the-hmac-days-123")
    with caplog.at_level(logging.WARNING, logger="bnh.config"):
        assert config.get_settings().resolved_signing_key_path() == tmp_path / "keys" / "c.key"
    assert "PROTOTYPE_SECRET is no longer used" in caplog.text


def test_operator_can_print_the_public_key_from_the_command_line(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "c.key"
    assert signing.main(["--key-file", str(path)]) == 0
    out = capsys.readouterr().out
    signer = ManifestSigner.load_or_create(path)
    assert signer.public_key_hex in out and signer.fingerprint in out
    assert "PRIVATE" not in out
    path.chmod(0o640)
    assert signing.main(["--key-file", str(path)]) == 2
