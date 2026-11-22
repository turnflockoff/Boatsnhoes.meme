from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import model_registry, tasks
from app.config import Settings
from app.ledger import Ledger
from app.main import create_app
from demo_worker import supported_tasks

SECRET = "test-secret-that-is-long-enough-123"
T0 = 1_800_000_000
MODEL_ID = "tiny-test-model"


def sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"models": [
        {"model_id": MODEL_ID, "file_name": "tiny.gguf", "source_url": "https://example.invalid/tiny",
         "license": "Apache-2.0", "sha256": None, "size_bytes": None, "min_vram_mb": 1},
    ]}))
    monkeypatch.setenv("MODEL_REGISTRY", str(reg))
    return reg


@pytest.fixture
def pinned_model(registry: Path, tmp_path: Path) -> Path:
    model = tmp_path / "tiny.gguf"
    model.write_bytes(b"GGUF fake weights for tests")
    model_registry.pin(MODEL_ID, model)
    return model


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(secret=SECRET, database_path=tmp_path / "db.sqlite3",
                        lease_seconds=30, max_attempts=2)
    return TestClient(create_app(settings, clock=lambda: T0))


def llm_payload(**overrides) -> str:
    return json.dumps({"model_id": MODEL_ID, "prompt": "Name a boat.", "max_tokens": 16,
                       "seed": 7, "timeout_s": 30} | overrides)


def test_shipped_registry_has_provenance_and_no_fake_pins() -> None:
    models = model_registry.load(model_registry.DEFAULT_REGISTRY)
    assert models
    for m in models.values():
        assert m.license and m.source_url.startswith("https://")
        # Either honestly unpinned, or a real-looking sha256 recorded by `pin`.
        assert m.sha256 is None or (len(m.sha256) == 64 and int(m.sha256, 16) >= 0)


def test_unpinned_model_cannot_be_used_for_jobs(registry: Path, client: TestClient) -> None:
    r = client.post("/v1/jobs", json={"task_type": "llm:generate", "payload": llm_payload()})
    assert r.status_code == 422 and "not pinned" in r.json()["detail"]


def test_pin_records_exact_hash_and_size(pinned_model: Path) -> None:
    entry = model_registry.load()[MODEL_ID]
    assert entry.pinned
    assert entry.sha256 == hashlib.sha256(pinned_model.read_bytes()).hexdigest()
    assert entry.size_bytes == pinned_model.stat().st_size
    assert model_registry.verify_file(entry, pinned_model) == (True, "ok")


def test_tampered_model_file_is_refused(pinned_model: Path) -> None:
    entry = model_registry.load()[MODEL_ID]
    pinned_model.write_bytes(b"GGUF fake weights for tesTS")  # same size, different bytes
    assert model_registry.verify_file(entry, pinned_model) == (False, "model file sha256 does not match the pin")
    pinned_model.write_bytes(b"shorter")
    assert model_registry.verify_file(entry, pinned_model)[1] == "model file size does not match the pin"


@pytest.mark.parametrize("payload,needle", [
    ("not json", "JSON"),
    ("[1, 2]", "JSON object"),
    (llm_payload(model_id="nope"), "unknown model_id"),
    (llm_payload(max_tokens=999), "max_tokens"),
    (llm_payload(seed=True), "seed"),
    (llm_payload(prompt=""), "prompt"),
    (llm_payload(timeout_s=1), "timeout_s"),
])
def test_bad_inference_payloads_are_rejected(pinned_model: Path, client: TestClient,
                                             payload: str, needle: str) -> None:
    r = client.post("/v1/jobs", json={"task_type": "llm:generate", "payload": payload})
    assert r.status_code == 422 and needle in r.json()["detail"]


def test_unknown_task_type_is_rejected(client: TestClient) -> None:
    r = client.post("/v1/jobs", json={"task_type": "crypto:mine", "payload": "x"})
    assert r.status_code == 422


def test_inference_result_is_accepted_but_flagged_for_audit(pinned_model: Path,
                                                             client: TestClient, tmp_path: Path) -> None:
    client.post("/v1/workers/register", json={"worker_id": "rig-gpu0"})
    job = client.post("/v1/jobs", json={"task_type": "llm:generate", "payload": llm_payload()}).json()
    client.post("/v1/jobs/next", params={"worker_id": "rig-gpu0"})
    text = "The Salty Hoe."
    r = client.post(f"/v1/jobs/{job['job_id']}/submit", json={
        "worker_id": "rig-gpu0", "result": text, "result_hash": sha(text),
        "started_at": T0, "completed_at": T0})
    body = r.json()
    assert r.status_code == 200
    assert body["confidence"] == "schema-only" and body["needs_audit"] is True
    assert body["execution_proof"] is False
    entry = client.get("/v1/ledger").json()["entries"][0]
    assert entry["verifier_method"] == "schema-check" and entry["confidence"] == "schema-only"


def test_empty_inference_output_is_rejected(pinned_model: Path, client: TestClient) -> None:
    client.post("/v1/workers/register", json={"worker_id": "rig-gpu0"})
    job = client.post("/v1/jobs", json={"task_type": "llm:generate", "payload": llm_payload()}).json()
    client.post("/v1/jobs/next", params={"worker_id": "rig-gpu0"})
    r = client.post(f"/v1/jobs/{job['job_id']}/submit", json={
        "worker_id": "rig-gpu0", "result": "   ", "result_hash": sha("   "),
        "started_at": T0, "completed_at": T0})
    assert r.status_code == 422


def test_sha_task_receipt_reports_exact_verification(client: TestClient) -> None:
    client.post("/v1/workers/register", json={"worker_id": "rig-cpu"})
    job = client.post("/v1/jobs", json={"payload": "ahoy"}).json()
    client.post("/v1/jobs/next", params={"worker_id": "rig-cpu"})
    r = client.post(f"/v1/jobs/{job['job_id']}/submit", json={
        "worker_id": "rig-cpu", "result": sha("ahoy"), "result_hash": sha(sha("ahoy")),
        "started_at": T0, "completed_at": T0}).json()
    assert r["confidence"] == "exact" and r["needs_audit"] is False


def test_worker_only_advertises_tasks_it_can_run(monkeypatch: pytest.MonkeyPatch,
                                                  registry: Path) -> None:
    monkeypatch.setenv("LLAMA_CLI", "/nonexistent/llama-cli")
    monkeypatch.setenv("PATH", "")
    ok, why = tasks.LlmGenerateTask().available()
    assert not ok and "llama-cli" in why
    assert supported_tasks() == {"demo:sha256"}


def test_download_requires_explicit_opt_in(registry: Path, tmp_path: Path,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    code = model_registry.main(["download", MODEL_ID, str(tmp_path / "dl")])
    assert code == 2 and "--yes-download" in capsys.readouterr().err
    assert not (tmp_path / "dl").exists()


def test_cli_pin_and_list(registry: Path, tmp_path: Path,
                          capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "tiny.gguf"
    f.write_bytes(b"abc")
    assert model_registry.main(["pin", MODEL_ID, str(f)]) == 0
    assert model_registry.main(["verify", MODEL_ID, str(f)]) == 0
    model_registry.main(["list"])
    assert "PINNED" in capsys.readouterr().out


def test_phase1_database_is_migrated_in_place(tmp_path: Path) -> None:
    db_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE submissions (job_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, "
                   "result_hash TEXT NOT NULL, reward_points INTEGER NOT NULL, "
                   "accepted_at INTEGER NOT NULL)")
    Ledger(db_path)
    with sqlite3.connect(db_path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(submissions)")}
    assert {"verifier_method", "confidence"} <= cols
