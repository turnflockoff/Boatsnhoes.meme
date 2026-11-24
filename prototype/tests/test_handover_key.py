"""Offline handover key (`handover-key-v1`, threat model #9 follow-up).

A handover signed by the old and new coordinator key lets workers follow planned rotations.
But someone who stole the coordinator's key file can sign such a handover to a key of their
own. With a separate offline handover key, announced by the coordinator and pinned by
workers, every handover must also carry that key's signature, which the thief doesn't have.
"""
from __future__ import annotations

import stat
from pathlib import Path

import httpx
import pytest

from app import key_rotation
from app.config import Settings, get_settings, handover_public_key_env
from app.key_rotation import (RotationError, check_announcement, follow_chain, history_path,
                              make_handover_key, rotate)
from app.main import create_app
from app.signing import ManifestSigner, SigningKeyError, fingerprint
from demo_worker import handover_pin_path, load_or_pin_coordinator_key
from tests.auth_helpers import OPERATOR, WorkerClient

T0 = 1_800_000_000


def make(tmp_path: Path, handover: str | None = None, name: str = "db") -> WorkerClient:
    settings = Settings(database_path=tmp_path / name / "db.sqlite3", operator_token=OPERATOR,
                        handover_public_key=handover)
    return WorkerClient(create_app(settings, clock=lambda: T0))


def key_file(tmp_path: Path, name: str = "db") -> Path:
    return tmp_path / name / "coordinator-signing.key"


def offline_key(tmp_path: Path) -> ManifestSigner:
    return make_handover_key(tmp_path / "usb" / "bnh-handover.key", key_file(tmp_path))


def info(client: WorkerClient) -> dict:
    return client.get("/v1/coordinator/key").json()


def steal_and_hand_over(tmp_path: Path) -> ManifestSigner:
    """What a thief with a copy of the coordinator key file can do: write a handover from
    our key to their own, signed by both (exactly like a genuine rotation, minus the stick)."""
    stolen = ManifestSigner.load(key_file(tmp_path))
    thief = ManifestSigner.load_or_create(tmp_path / "thief" / "coordinator-signing.key")
    statement = {"purpose": key_rotation.ROTATION_PURPOSE, "sequence": 1,
                 "old_public_key": stolen.public_key_hex,
                 "new_public_key": thief.public_key_hex, "rotated_at": T0 + 5}
    entry = statement | {"signatures": {"old": stolen.sign(statement),
                                        "new": thief.sign(statement)}}
    key_rotation._write_history(history_path(tmp_path / "thief" / "coordinator-signing.key"),
                                [entry])
    return thief


def test_make_handover_key_is_private_never_overwritten_and_kept_apart(
        tmp_path: Path) -> None:
    make(tmp_path)  # creates the coordinator key
    out = tmp_path / "usb" / "bnh-handover.key"
    signer = make_handover_key(out, key_file(tmp_path))
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert ManifestSigner.load(out).public_key_hex == signer.public_key_hex
    with pytest.raises(SigningKeyError, match="already exists"):
        make_handover_key(out, key_file(tmp_path))
    assert ManifestSigner.load(out).public_key_hex == signer.public_key_hex  # untouched
    # Next to the coordinator key it would be stolen together with it.
    with pytest.raises(SigningKeyError, match="also holds the coordinator signing key"):
        make_handover_key(key_file(tmp_path).with_name("handover.key"), key_file(tmp_path))
    # Loading never invents a key: a missing file means "plug in the stick".
    with pytest.raises(SigningKeyError, match="does not exist"):
        ManifestSigner.load(tmp_path / "usb" / "missing.key")
    assert not (tmp_path / "usb" / "missing.key").exists()


def test_coordinator_announces_the_handover_key_signed_by_its_own_key(tmp_path: Path) -> None:
    assert info(make(tmp_path))["handover_key"] is None  # off by default
    health = make(tmp_path).get("/v1/operator/health",
                                headers={"X-Operator-Token": OPERATOR}).json()
    assert health["handover_key"] is None
    offline = offline_key(tmp_path)
    client = make(tmp_path, offline.public_key_hex)
    data = info(client)
    assert data["handover_key"]["fingerprint"] == offline.fingerprint
    assert check_announcement(data["handover_key"], data["public_key"]) == \
        offline.public_key_hex
    health = client.get("/v1/operator/health", headers={"X-Operator-Token": OPERATOR}).json()
    assert health["handover_key"] == {"rule": "handover-key-v1",
                                      "fingerprint": offline.fingerprint}
    assert not [w for w in health["warnings"] if "handover" in w.lower()]
    assert "PRIVATE" not in str(data) and "PRIVATE" not in str(health)


def test_announcement_must_be_signed_by_the_trusted_coordinator_key(tmp_path: Path) -> None:
    offline = offline_key(tmp_path)
    data = info(make(tmp_path, offline.public_key_hex))
    coord, announced = data["public_key"], data["handover_key"]
    other = ManifestSigner.load_or_create(tmp_path / "other.key")
    cases = {
        "not signed": announced | {"handover_public_key": other.public_key_hex},
        "wrong purpose": announced | {"purpose": "bnh-reward-report/1"},
        "different coordinator key": announced | {"coordinator_public_key":
                                                  other.public_key_hex},
        "64 hex": announced | {"handover_public_key": "abc"},
        "no handover key announcement": "hello",
    }
    for message, bad in cases.items():
        with pytest.raises(RotationError, match=message):
            check_announcement(bad, coord)
    # A coordinator can't announce its own signing key as the handover key.
    me = ManifestSigner.load(key_file(tmp_path))
    with pytest.raises(RotationError, match="coordinator key itself"):
        check_announcement(key_rotation.announcement(me, me.public_key_hex), coord)


def test_coordinator_refuses_its_own_signing_key_as_the_handover_key(tmp_path: Path) -> None:
    own = info(make(tmp_path))["public_key"]
    with pytest.raises(SigningKeyError, match="separate offline key"):
        make(tmp_path, own)
    rotate(key_file(tmp_path), T0)  # a retired coordinator key doesn't count either
    with pytest.raises(SigningKeyError, match="separate offline key"):
        make(tmp_path, own)


def test_setting_must_be_a_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_HANDOVER_PUBLIC_KEY", "not-a-key")
    with pytest.raises(ValueError, match="COORDINATOR_HANDOVER_PUBLIC_KEY"):
        handover_public_key_env()
    monkeypatch.setenv("COORDINATOR_HANDOVER_PUBLIC_KEY", " " + "AB" * 32 + " ")
    assert handover_public_key_env() == "ab" * 32
    assert get_settings().handover_public_key == "ab" * 32
    monkeypatch.setenv("COORDINATOR_HANDOVER_PUBLIC_KEY", "")
    assert handover_public_key_env() is None


def test_worker_pins_the_announced_handover_key_on_first_contact_and_later(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pin = tmp_path / "worker" / "coordinator.pub"
    load_or_pin_coordinator_key(make(tmp_path), pin)  # no handover key yet
    assert not handover_pin_path(pin).exists()
    offline = offline_key(tmp_path)
    capsys.readouterr()
    load_or_pin_coordinator_key(make(tmp_path, offline.public_key_hex), pin)  # added later
    assert handover_pin_path(pin).read_text().strip() == offline.public_key_hex
    assert stat.S_IMODE(handover_pin_path(pin).stat().st_mode) == 0o600
    assert offline.fingerprint in capsys.readouterr().out
    # A brand-new worker pins both on first contact.
    fresh = tmp_path / "fresh" / "coordinator.pub"
    load_or_pin_coordinator_key(make(tmp_path, offline.public_key_hex), fresh)
    assert handover_pin_path(fresh).read_text().strip() == offline.public_key_hex


def test_worker_ignores_a_tampered_announcement_and_never_replaces_its_pin(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    offline = offline_key(tmp_path)
    client = make(tmp_path, offline.public_key_hex)
    other = ManifestSigner.load_or_create(tmp_path / "other.key")
    real_send = client.send

    def tampered_send(request, **kw):  # a man in the middle swaps the announced key
        response = real_send(request, **kw)
        if request.url.path != "/v1/coordinator/key":
            return response
        response.read()
        body = response.json()
        body["handover_key"]["handover_public_key"] = other.public_key_hex
        return httpx.Response(200, json=body, request=request)

    pin = tmp_path / "worker" / "coordinator.pub"
    client.send = tampered_send  # type: ignore[method-assign]
    load_or_pin_coordinator_key(client, pin)
    assert "ignoring the coordinator's handover key announcement" in capsys.readouterr().out
    assert not handover_pin_path(pin).exists()

    # A genuine pin is never replaced by a different (even validly signed) announcement.
    load_or_pin_coordinator_key(make(tmp_path, offline.public_key_hex), pin)
    capsys.readouterr()
    load_or_pin_coordinator_key(make(tmp_path, other.public_key_hex), pin)
    assert "Keeping the pinned one" in capsys.readouterr().out
    assert handover_pin_path(pin).read_text().strip() == offline.public_key_hex


def test_handover_key_given_by_the_operator_must_match_the_pin(tmp_path: Path) -> None:
    offline = offline_key(tmp_path)
    other = ManifestSigner.load_or_create(tmp_path / "other.key")
    pin = tmp_path / "worker" / "coordinator.pub"
    load_or_pin_coordinator_key(make(tmp_path), pin, handover_given=offline.public_key_hex)
    assert handover_pin_path(pin).read_text().strip() == offline.public_key_hex
    with pytest.raises(SystemExit, match="differs from the one pinned"):
        load_or_pin_coordinator_key(make(tmp_path), pin, handover_given=other.public_key_hex)
    with pytest.raises(SystemExit, match="Bad handover key"):
        load_or_pin_coordinator_key(make(tmp_path), pin, handover_given="xyz")


def test_stolen_coordinator_key_can_no_longer_move_a_worker_that_pinned_the_handover_key(
        tmp_path: Path) -> None:
    offline = offline_key(tmp_path)
    protected = tmp_path / "protected" / "coordinator.pub"
    legacy = tmp_path / "legacy" / "coordinator.pub"
    ours = load_or_pin_coordinator_key(make(tmp_path, offline.public_key_hex), protected)
    load_or_pin_coordinator_key(make(tmp_path), legacy)  # never saw a handover key

    thief = steal_and_hand_over(tmp_path)
    fake = make(tmp_path, name="thief")  # the thief's coordinator serves their own key
    assert info(fake)["public_key"] == thief.public_key_hex

    with pytest.raises(SystemExit, match="not signed by the pinned handover key"):
        load_or_pin_coordinator_key(fake, protected)
    assert protected.read_text().strip() == ours  # pin untouched
    # The old weakness, still there for workers without a handover key (documented).
    assert load_or_pin_coordinator_key(fake, legacy) == thief.public_key_hex


def test_genuine_rotation_with_the_offline_key_is_followed(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pin = tmp_path / "worker" / "coordinator.pub"
    old_pub = load_or_pin_coordinator_key(make(tmp_path), pin)
    rotate(key_file(tmp_path), T0)  # before the handover key existed: no third signature
    offline = offline_key(tmp_path)
    mid = load_or_pin_coordinator_key(make(tmp_path, offline.public_key_hex), pin)
    assert mid != old_pub and handover_pin_path(pin).exists()

    entry = rotate(key_file(tmp_path), T0 + 1, offline, offline.public_key_hex)
    assert entry["handover_fingerprint"] == offline.fingerprint
    capsys.readouterr()
    new_pub = load_or_pin_coordinator_key(make(tmp_path, offline.public_key_hex), pin)
    assert new_pub == entry["new_public_key"] == pin.read_text().strip()
    assert "offline handover key" in capsys.readouterr().out
    # Only the steps after the pinned key need the third signature; the older one is fine.
    chain = info(make(tmp_path, offline.public_key_hex))["rotations"]
    assert follow_chain(chain, mid, new_pub, offline.public_key_hex) == [mid, new_pub]
    with pytest.raises(RotationError, match="handover 1 is not signed by the pinned"):
        follow_chain(chain, old_pub, new_pub, offline.public_key_hex)


def test_rotate_refuses_without_the_announced_handover_key(tmp_path: Path) -> None:
    make(tmp_path)
    offline = offline_key(tmp_path)
    other = make_handover_key(tmp_path / "usb2" / "other.key", key_file(tmp_path))
    before = ManifestSigner.load(key_file(tmp_path)).public_key_hex
    with pytest.raises(SigningKeyError, match="pass --handover-key-file"):
        rotate(key_file(tmp_path), T0, None, offline.public_key_hex)
    with pytest.raises(SigningKeyError, match="but the coordinator announces"):
        rotate(key_file(tmp_path), T0, other, offline.public_key_hex)
    coordinator = ManifestSigner.load(key_file(tmp_path))
    with pytest.raises(SigningKeyError, match="separate key"):
        rotate(key_file(tmp_path), T0, coordinator)
    # Nothing changed after any refusal.
    assert ManifestSigner.load(key_file(tmp_path)).public_key_hex == before
    assert not history_path(key_file(tmp_path)).exists()


def test_signature_by_the_wrong_offline_key_is_refused(tmp_path: Path) -> None:
    old_pub = info(make(tmp_path))["public_key"]
    offline = offline_key(tmp_path)
    impostor = make_handover_key(tmp_path / "usb2" / "imp.key", key_file(tmp_path))
    entry = rotate(key_file(tmp_path), T0, impostor)
    with pytest.raises(RotationError, match="not signed by the pinned handover key"):
        follow_chain([entry], old_pub, entry["new_public_key"], offline.public_key_hex)
    no_sig = dict(entry, signatures={k: v for k, v in entry["signatures"].items()
                                     if k != "handover"})
    with pytest.raises(RotationError, match="not signed by the pinned handover key"):
        follow_chain([no_sig], old_pub, entry["new_public_key"], offline.public_key_hex)
    # A "handover key" that is itself on the chain proves nothing.
    with pytest.raises(RotationError, match="also a coordinator key"):
        follow_chain([entry], old_pub, entry["new_public_key"], entry["new_public_key"])


def test_cli_makes_the_key_and_rotates_with_it_without_printing_private_material(
        tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "coord" / "c.key"
    ManifestSigner.load_or_create(path)
    out = tmp_path / "usb" / "h.key"
    monkeypatch.delenv("COORDINATOR_HANDOVER_PUBLIC_KEY", raising=False)
    assert key_rotation.main(["make-handover-key", "--key-file", str(path)]) == 2
    assert key_rotation.main(["make-handover-key", "--key-file", str(path),
                              "--out", str(out)]) == 0
    text = capsys.readouterr().out
    offline = ManifestSigner.load(out)
    assert f"COORDINATOR_HANDOVER_PUBLIC_KEY={offline.public_key_hex}" in text
    assert "PRIVATE" not in text
    assert key_rotation.main(["make-handover-key", "--key-file", str(path),
                              "--out", str(out)]) == 2  # never overwrites

    monkeypatch.setenv("COORDINATOR_HANDOVER_PUBLIC_KEY", offline.public_key_hex)
    capsys.readouterr()
    assert key_rotation.main(["rotate", "--key-file", str(path)]) == 2
    assert "--handover-key-file" in capsys.readouterr().out
    assert key_rotation.main(["rotate", "--key-file", str(path),
                              "--handover-key-file", str(out)]) == 0
    assert "also signed by the offline handover key" in capsys.readouterr().out
    assert key_rotation.main(["show", "--key-file", str(path)]) == 0
    shown = capsys.readouterr().out
    assert f"+ handover key {offline.fingerprint}" in shown and "PRIVATE" not in shown
    assert fingerprint(offline.public_key_hex) == offline.fingerprint
