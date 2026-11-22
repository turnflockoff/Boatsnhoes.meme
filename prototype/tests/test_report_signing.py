"""Signed reward reports (threat model #10): the coordinator signs a closed period's report
with its Ed25519 key so anyone holding only the public key and the JSON file can check that
the coordinator issued it and that nobody edited it afterwards."""
from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import rewards
from app.signing import ManifestSigner, verify_manifest
from tests.test_audits import OP, T0, make
from tests.test_rewards import sha_job


def signed(client, **params) -> dict:
    r = client.get("/v1/rewards/report", params={"sign": "true", **params}, headers=OP)
    assert r.status_code == 200, r.text
    return r.json()


def closed_period(client, clock) -> dict:
    sha_job(client, "wk-a", points=4)
    clock.t += 200  # the period [0, T0+100) is now in the past
    return signed(client, until=T0 + 100)


def test_signed_report_verifies_with_only_the_public_key(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    rep = closed_period(client, clock)
    signer = client.app.state.signer
    sig = rep["signature"]
    assert sig["algorithm"] == "ed25519"
    assert sig["public_key"] == signer.public_key_hex
    assert sig["fingerprint"] == signer.fingerprint
    assert sig["value"].startswith("ed25519:")
    # Offline check: no database, just the file and the trusted key.
    assert rewards.check_signature(rep, signer.public_key_hex) == []
    # Full check against the ledger also passes, and the hash ignores the signature block.
    assert rewards.verify_report(rep, ledger, signer.public_key_hex) == []
    assert rep["report_sha256"] == rewards.report_hash(rep)


def test_unsigned_report_by_default(tmp_path: Path) -> None:
    client, clock, _ = make(tmp_path)
    sha_job(client, "wk-a")
    rep = client.get("/v1/rewards/report", headers=OP).json()
    assert "signature" not in rep
    assert rewards.check_signature(rep) == ["report is not signed"]


def test_open_period_is_never_signed(tmp_path: Path) -> None:
    client, clock, _ = make(tmp_path)
    sha_job(client, "wk-a")
    # Default `until` is in the future, so the period is still open.
    r = client.get("/v1/rewards/report", params={"sign": "true"}, headers=OP)
    assert r.status_code == 422 and "still open" in r.json()["detail"]
    r = client.get("/v1/rewards/report", params={"sign": "true", "until": T0 + 100},
                   headers=OP)
    assert r.status_code == 422  # clock is at T0, period ends later


def test_report_failing_its_own_checks_is_not_signed(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    rep = rewards.build_report(ledger, 0, T0 + 100)
    rep["checks"]["ok"] = False
    rep["report_sha256"] = rewards.report_hash(rep)
    try:
        rewards.sign_report(rep, client.app.state.signer, now=T0 + 1000)
    except rewards.ReportError as exc:
        assert "fails its own checks" in str(exc)
    else:
        raise AssertionError("signed a report that failed its checks")


def test_edited_report_breaks_the_signature_even_if_rehashed(tmp_path: Path) -> None:
    client, clock, _ = make(tmp_path)
    rep = closed_period(client, clock)
    key = client.app.state.signer.public_key_hex
    edited = json.loads(json.dumps(rep))
    edited["workers"][0]["payable_points"] = 9000
    problems = rewards.check_signature(edited, key)
    assert any("was edited" in p for p in problems)
    # A forger who also recomputes report_sha256 still can't redo the signature.
    edited["report_sha256"] = rewards.report_hash(edited)
    assert "signature does not match the report" in rewards.check_signature(edited, key)


def test_report_signed_by_another_key_is_refused(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    rep = rewards.build_report(ledger, 0, T0 + 100)
    impostor = ManifestSigner(Ed25519PrivateKey.generate())
    forged = rewards.sign_report(rep, impostor, now=T0 + 1000)
    real_key = client.app.state.signer.public_key_hex
    # Self-consistent (signed by the key it names) but not by the coordinator we trust.
    assert rewards.check_signature(forged) == []
    problems = rewards.check_signature(forged, real_key)
    assert any("different key" in p and impostor.fingerprint in p for p in problems)
    # Swapping in the real public key doesn't help: the signature no longer matches.
    forged["signature"]["public_key"] = real_key
    assert "signature does not match the report" in rewards.check_signature(forged, real_key)


def test_report_signature_is_not_a_job_signature(tmp_path: Path) -> None:
    """Domain separation: the same key signs jobs and reports, so a report signature must
    never verify as a job manifest (and its signed message names its purpose)."""
    client, clock, _ = make(tmp_path)
    rep = closed_period(client, clock)
    fields = rewards.signed_fields(rep)
    assert fields["purpose"] == rewards.SIGNATURE_PURPOSE
    key = client.app.state.signer.public_key_hex
    assert verify_manifest(fields, rep["signature"]["value"], key)
    job_like = {k: v for k, v in fields.items() if k != "purpose"}
    assert not verify_manifest(job_like, rep["signature"]["value"], key)


def test_verify_requires_signature_when_a_key_is_given(tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    sha_job(client, "wk-a")
    rep = rewards.build_report(ledger, 0, T0 + 100)
    key = client.app.state.signer.public_key_hex
    assert rewards.verify_report(rep, ledger) == []
    assert "report is not signed" in rewards.verify_report(rep, ledger, key)


def test_cli_sign_and_offline_verify(tmp_path: Path, capsys) -> None:
    client, clock, ledger = make(tmp_path)
    sha_job(client, "wk-a")
    key_file = client.app.state.settings.resolved_signing_key_path()
    public = client.app.state.signer.public_key_hex
    out = tmp_path / "signed.json"
    # Default --until for a signed report is "now", so the period is closed.
    assert rewards.main(["report", "--db", str(ledger.path), "--sign", "--key-file",
                         str(key_file), "--out", str(out)]) == 0
    capsys.readouterr()
    # Anyone with the file and the public key can check it, no database needed.
    assert rewards.main(["verify", str(out), "--no-db", "--public-key", public]) == 0
    text = capsys.readouterr().out
    assert "signed by" in text and client.app.state.signer.fingerprint in text
    assert "OK" in text
    # Without a trusted key the CLI still checks, but tells you to compare the fingerprint.
    assert rewards.main(["verify", str(out), "--no-db"]) == 0
    assert "compare" in capsys.readouterr().out.lower()
    # Full check with the database too.
    assert rewards.main(["verify", str(out), "--db", str(ledger.path),
                         "--public-key", public]) == 0
    # A signed report for a period that ends in the future is refused.
    assert rewards.main(["report", "--db", str(ledger.path), "--sign", "--key-file",
                         str(key_file), "--until", "99999999999"]) == 2
    # An edited file fails the offline check.
    data = json.loads(out.read_text())
    data["totals"]["payable_points"] += 1
    out.write_text(json.dumps(data))
    assert rewards.main(["verify", str(out), "--no-db", "--public-key", public]) == 1


def test_cli_key_from_environment(tmp_path: Path, capsys, monkeypatch) -> None:
    client, clock, ledger = make(tmp_path)
    rep = closed_period(client, clock)
    out = tmp_path / "r.json"
    out.write_text(json.dumps(rep))
    monkeypatch.setenv("BNH_COORDINATOR_KEY", client.app.state.signer.public_key_hex)
    assert rewards.main(["verify", str(out), "--no-db"]) == 0
    other = ManifestSigner(Ed25519PrivateKey.generate()).public_key_hex
    monkeypatch.setenv("BNH_COORDINATOR_KEY", other)
    assert rewards.main(["verify", str(out), "--no-db"]) == 1
    assert os.environ["BNH_COORDINATOR_KEY"] == other


def test_cli_sign_never_creates_a_new_key(tmp_path: Path, capsys, monkeypatch) -> None:
    """Signing with a missing key file must fail, not quietly mint a key nobody trusts."""
    client, clock, ledger = make(tmp_path)
    sha_job(client, "wk-a")
    missing = tmp_path / "nope" / "coordinator-signing.key"
    assert rewards.main(["report", "--db", str(ledger.path), "--sign",
                         "--key-file", str(missing)]) == 2
    assert not missing.exists()
    assert "not found" in capsys.readouterr().err


def test_env_key_does_not_break_unsigned_ledger_checks(tmp_path: Path, monkeypatch) -> None:
    client, clock, ledger = make(tmp_path)
    sha_job(client, "wk-a")
    out = tmp_path / "plain.json"
    assert rewards.main(["report", "--db", str(ledger.path), "--until", str(T0 + 100),
                         "--out", str(out)]) == 0
    monkeypatch.setenv("BNH_COORDINATOR_KEY", client.app.state.signer.public_key_hex)
    assert rewards.main(["verify", str(out), "--db", str(ledger.path)]) == 0
    # An explicit --public-key, though, is a demand for a signature.
    assert rewards.main(["verify", str(out), "--db", str(ledger.path), "--public-key",
                         client.app.state.signer.public_key_hex]) == 1
