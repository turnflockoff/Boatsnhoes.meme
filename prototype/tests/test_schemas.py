"""Track E: versioned job/receipt schemas with input-hash and model-hash checks.

Jobs carry a signed `schema_version` and (for model tasks) the pinned `model_sha256`.
Receipts echo the versions and hashes the worker used; the coordinator refuses mismatches.
These catch honest mistakes, not cheaters: a dishonest worker can copy the expected values.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app import model_registry, schemas
from app.config import Settings
from app.ledger import Ledger
from app.main import create_app
from app.rewards import build_report
from app.signing import ManifestSigner
from demo_worker import ManifestError, check_manifest
from tests.auth_helpers import flat_audit_rate, OPERATOR, FirstPickRng, WorkerClient, enroll, receipt

T0 = 1_800_000_000
MODEL_ID = "tiny-test-model"


def sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"models": [
        {"model_id": MODEL_ID, "file_name": "tiny.gguf", "source_url": "https://example.invalid/t",
         "license": "Apache-2.0", "sha256": None, "size_bytes": None, "min_vram_mb": 1}]}))
    monkeypatch.setenv("MODEL_REGISTRY", str(reg))
    return reg


@pytest.fixture
def model(registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pinned fake model, plus a fake llama-cli so the worker advertises llm:generate.
    Nothing here runs inference; it only lets check_manifest reach the model-pin check."""
    models = tmp_path / "models"
    models.mkdir()
    f = models / "tiny.gguf"
    f.write_bytes(b"GGUF fake weights for tests")
    model_registry.pin(MODEL_ID, f)
    cli = tmp_path / "llama-cli"
    cli.write_text("#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("LLAMA_CLI", str(cli))
    monkeypatch.setenv("MODEL_DIR", str(models))
    return f


def make(tmp_path: Path, audit_rate: float = 0.0) -> WorkerClient:
    settings = Settings(database_path=tmp_path / "db.sqlite3", lease_seconds=30,
                        operator_token=OPERATOR, audit_rate=audit_rate,
                        **flat_audit_rate(audit_rate))
    client = WorkerClient(create_app(settings, clock=lambda: T0, rng=FirstPickRng(1)))
    for w in ("wk-a", "wk-b"):
        enroll(client, w)
    return client


def llm_payload() -> str:
    return json.dumps({"model_id": MODEL_ID, "prompt": "Name a boat.", "max_tokens": 16,
                       "seed": 7, "timeout_s": 30})


def claimed(client: WorkerClient, worker: str = "wk-a", **job) -> dict:
    assert client.post("/v1/jobs", json={"payload": "ahoy"} | job).status_code == 200
    r = client.post("/v1/jobs/next", params={"worker_id": worker})
    assert r.status_code == 200, r.text
    return r.json()


def submit(client: WorkerClient, job: dict, result: str, worker: str = "wk-a", **override):
    body = {"worker_id": worker, "result": result, "result_hash": sha(result),
            "started_at": T0, "completed_at": T0} | receipt(client, job["job_id"]) | override
    return client.post(f"/v1/jobs/{job['job_id']}/submit", json=body)


# -- job manifests ----------------------------------------------------------------------

def test_manifest_carries_signed_version_and_model_hash(tmp_path: Path) -> None:
    client = make(tmp_path)
    pub = client.get("/v1/coordinator/key").json()["public_key"]
    job = claimed(client)
    assert job["schema_version"] == schemas.JOB_SCHEMA_VERSION == "bnh-job/1"
    assert job["model_sha256"] is None  # demo:sha256 uses no model
    check_manifest(job, pub, now=T0)
    # Both new fields are covered by the signature: changing either breaks it.
    with pytest.raises(ManifestError, match="bad signature"):
        check_manifest(job | {"model_sha256": "a" * 64}, pub, now=T0)
    health = client.get("/health").json()
    assert health["job_schema"] == "bnh-job/1" and health["receipt_schema"] == "bnh-receipt/1"


def test_worker_refuses_job_versions_it_does_not_know(tmp_path: Path) -> None:
    client = make(tmp_path)
    pub = client.get("/v1/coordinator/key").json()["public_key"]
    job = claimed(client)
    # Even a correctly signed manifest is refused if its layout is newer than the worker.
    signer = ManifestSigner.load_or_create(tmp_path / "coordinator-signing.key")
    future = job | {"schema_version": "bnh-job/2"}
    future["signature"] = signer.sign({k: future[k] for k in schemas.SIGNED_FIELDS})
    with pytest.raises(ManifestError, match="unsupported job schema_version 'bnh-job/2'"):
        check_manifest(future, pub, now=T0)
    with pytest.raises(ManifestError, match="unsupported job schema_version None"):
        check_manifest({k: v for k, v in job.items() if k != "schema_version"}, pub, now=T0)


def test_model_job_pins_the_model_hash_and_worker_checks_its_own_pin(
        model: Path, registry: Path, tmp_path: Path) -> None:
    client = make(tmp_path)
    pub = client.get("/v1/coordinator/key").json()["public_key"]
    job = claimed(client, task_type="llm:generate", payload=llm_payload())
    assert job["model_sha256"] == model_registry.file_sha256(model)
    check_manifest(job, pub, now=T0)  # same pin on both sides
    # The worker's registry was pinned to a different file: refuse before running anything.
    other = tmp_path / "other.gguf"
    other.write_bytes(b"different weights")
    model_registry.pin(MODEL_ID, other, replace=True)
    with pytest.raises(ManifestError, match="model pin mismatch"):
        check_manifest(job, pub, now=T0)
    # The worker never pinned the model at all.
    registry.write_text(json.dumps({"models": [
        {"model_id": MODEL_ID, "file_name": "tiny.gguf", "source_url": "https://example.invalid/t",
         "license": "Apache-2.0", "sha256": None, "size_bytes": None, "min_vram_mb": 1}]}))
    with pytest.raises(ManifestError):
        check_manifest(job, pub, now=T0)


def test_audit_reruns_keep_the_original_model_hash_after_a_repin(
        model: Path, tmp_path: Path) -> None:
    client = make(tmp_path, audit_rate=1.0)
    ledger: Ledger = client.app.state.ledger
    original = claimed(client, task_type="llm:generate", payload=llm_payload())
    pinned_then = original["model_sha256"]
    # The operator re-pins the registry before the audit runs.
    other = tmp_path / "newer.gguf"
    other.write_bytes(b"newer weights")
    model_registry.pin(MODEL_ID, other, replace=True)
    assert submit(client, original, "The Salty Hoe").status_code == 200
    audit = client.post("/v1/jobs/next", params={"worker_id": "wk-b"}).json()
    assert audit["payload"] == original["payload"]
    assert audit["model_sha256"] == pinned_then != model_registry.file_sha256(other)
    assert ledger.check_consistency() == []
    # Tamper with the audit's model hash: the consistency check notices.
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE jobs SET model_sha256=? WHERE job_id=?", ("b" * 64, audit["job_id"]))
    assert any("doesn't match the original" in p for p in ledger.check_consistency())


# -- receipts ---------------------------------------------------------------------------

def test_receipt_must_match_the_job(tmp_path: Path) -> None:
    client = make(tmp_path)
    ledger: Ledger = client.app.state.ledger
    job = claimed(client)
    answer = sha("ahoy")
    cases = {
        "receipt_version": ({"receipt_version": None}, "unsupported receipt_version"),
        "old receipt": ({"receipt_version": "bnh-receipt/0"}, "unsupported receipt_version"),
        "job version": ({"job_schema_version": "bnh-job/0"}, "job_schema_version"),
        "input hash": ({"payload_hash": sha("a different input")}, "payload_hash"),
        "model hash": ({"model_sha256": "c" * 64}, "model_sha256"),
    }
    for name, (override, why) in cases.items():
        r = submit(client, job, answer, **override)
        assert r.status_code == 422, name
        assert why in r.json()["detail"], name
        assert ledger.get_job(job["job_id"])["status"] == "leased", name  # lease untouched
    # A receipt with every field right is accepted; the bad tries show up as activity.
    ok = submit(client, job, answer)
    assert ok.status_code == 200 and ok.json()["status"] == "accepted"
    mismatches = [e for e in ledger.events(job["job_id"]) if e["event"] == "receipt_mismatch"]
    assert len(mismatches) == 3  # version-only problems are refused before the job lookup
    report = build_report(ledger, 0, T0 + 1)
    worker = next(w for w in report["workers"] if w["worker_id"] == "wk-a")
    assert worker["activity"]["bad_submissions"] == 3
    assert ledger.check_consistency() == []


def test_model_receipt_must_name_the_pinned_model(model: Path, tmp_path: Path) -> None:
    client = make(tmp_path)
    job = claimed(client, task_type="llm:generate", payload=llm_payload())
    # A worker that ran no model (or says so) is refused for a model job.
    assert submit(client, job, "The Salty Hoe", model_sha256=None).status_code == 422
    assert submit(client, job, "The Salty Hoe").status_code == 200


def test_receipt_helper_hashes_the_payload_it_was_given() -> None:
    job = {"schema_version": "bnh-job/1", "payload": "ahoy", "payload_hash": "f" * 64}
    fields = schemas.receipt_fields(job, None)
    assert fields["payload_hash"] == sha("ahoy")  # computed, not copied from the job
    assert schemas.receipt_problem(fields, job) == "payload_hash does not match the job"


# -- older databases --------------------------------------------------------------------

def test_jobs_from_before_versioning_are_marked_legacy_and_refused(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_type TEXT NOT NULL,
                payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                reward_points INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                signature TEXT NOT NULL, status TEXT NOT NULL, worker_id TEXT,
                lease_expires_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL);
        """)
        db.execute("INSERT INTO jobs VALUES ('old','demo:sha256','ahoy',?,1,?,'ed25519:00',"
                   "'queued',NULL,NULL,0,3,?,?)", (sha("ahoy"), T0 + 600, T0, T0))
    settings = Settings(database_path=path, operator_token=OPERATOR)
    client = WorkerClient(create_app(settings, clock=lambda: T0))
    enroll(client, "wk-a")
    assert client.app.state.ledger.get_job("old")["schema_version"] == "bnh-job/0"
    job = client.post("/v1/jobs/next", params={"worker_id": "wk-a"}).json()
    pub = client.get("/v1/coordinator/key").json()["public_key"]
    with pytest.raises(ManifestError, match="'bnh-job/0'"):
        check_manifest(job, pub, now=T0)
    assert client.app.state.ledger.check_consistency() == []
