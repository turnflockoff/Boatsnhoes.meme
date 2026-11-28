"""`llm:generate` payloads the coordinator accepted but no worker could run (`llm-payload-v2`).

Code audit, twenty-sixth pass (0.5.78). Found on the old tree:
  * A prompt with a lone surrogate (JSON escape "\\ud800" on its own) passed `validate`, the
    coordinator queued and signed the job (200), and every worker that took it crashed with
    UnicodeEncodeError while writing the prompt file: one bad job stopped rig after rig.
  * Unknown keys were ignored without a word, so `"max_token": 200` (typo) or
    `"temperature": 0.8` (a setting this task doesn't have) ran with the defaults.
  * A key written twice (`"prompt": "a", "prompt": "b"`) was accepted; Python keeps the last
    copy, other readers the first.
  * `validate` let RecursionError (deeply nested payload) and ValueError (a 5,000-digit
    number) escape instead of TaskError; the worker runs `validate` first, so a job like that
    would stop it too.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import demo_worker
from app import challenge_pool, llm_runner, tasks
from tests.auth_helpers import OP
from tests.test_tasks import MODEL_ID, client, llm_payload, pinned_model, registry  # noqa: F401

LONE = "\ud800"
TASK = tasks.EXECUTORS["llm:generate"]


def post_job(client: TestClient, payload: str, route: str = "/v1/jobs"):
    body = {"task_type": "llm:generate", "payload": payload, "ttl_seconds": 600}
    if route == "/v1/challenges":
        body["expected_result"] = "The Salty Hoe"
    return client.post(route, json=body, headers=OP)


def with_repeated_prompt() -> str:
    return ('{"model_id": "%s", "prompt": "Name a boat.", "prompt": "Name a ship.", '
            '"max_tokens": 16, "seed": 7, "timeout_s": 30}' % MODEL_ID)


BAD = {
    "lone surrogate in the prompt": (llm_payload(prompt="Name a boat " + LONE), "valid text"),
    "low surrogate on its own": (llm_payload(prompt="\udc80 boat"), "valid text"),
    "typo'd setting": (llm_payload(max_token=200), "unknown key 'max_token'"),
    "setting this task doesn't have": (llm_payload(temperature=0.8), "unknown key 'temperature'"),
    "key written twice": (with_repeated_prompt(), "key 'prompt' twice"),
}


@pytest.mark.parametrize("route", ["/v1/jobs", "/v1/challenges"])
@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_payloads_are_refused_before_anything_is_signed(
        pinned_model: Path, client: TestClient, name: str, route: str) -> None:
    payload, needle = BAD[name]
    r = post_job(client, payload, route)
    assert r.status_code == 422, r.text
    assert needle in r.json()["detail"]
    assert client.post("/v1/jobs/next", params={"worker_id": "nobody"}).status_code != 200


def test_unknown_key_message_lists_the_allowed_keys(pinned_model: Path) -> None:
    with pytest.raises(tasks.TaskError) as err:
        TASK.validate(llm_payload(n_gpu_layers=99))
    assert "allowed: model_id, prompt, max_tokens, seed, timeout_s" in str(err.value)


def test_key_names_in_messages_are_printable_and_short(pinned_model: Path) -> None:
    for payload in (llm_payload(**{"\x1b[2J" + "x" * 500: 1}),
                    llm_payload(**{"bóat⛵‮": 1}),                # non-ASCII, right-to-left mark
                    '{"\\u001b]0;pwned\\u0007": 1, "\\u001b]0;pwned\\u0007": 2}'):
        with pytest.raises(tasks.TaskError) as err:
            TASK.validate(payload)
        message = str(err.value)
        assert all(32 <= ord(ch) < 127 for ch in message), message
        assert len(message) < 160


@pytest.mark.parametrize("payload", [
    "[" * 4000,                                         # nested far past Python's limit
    '{"a":' * 3000 + "1" + "}" * 3000,
    '{"model_id": "x", "prompt": "hi", "seed": 1' + "0" * 5000 + "}",   # 5,001 digits
    "\ud800",
    "",
    '"just a string"',
    "null",
])
def test_validate_raises_only_task_error(pinned_model: Path, payload: str) -> None:
    with pytest.raises(tasks.TaskError):
        TASK.validate(payload)
    TASK.recognisable_text(payload)  # documented as never raising
    TASK.run_limit_seconds(payload)
    tasks.input_fingerprint("llm:generate", payload)


@pytest.mark.parametrize("payload", [
    llm_payload(),
    json.dumps({"model_id": MODEL_ID, "prompt": "Name a boat."}),           # defaults
    llm_payload(prompt="Nomme un bateau, s'il te plaît ⛵ 🚤"),               # real pairs
    json.dumps({"model_id": MODEL_ID, "prompt": "⛵ 🚤 ünïcödé"}, ensure_ascii=False),
    llm_payload(prompt="line one\nline two\ttabbed"),
    json.dumps({"timeout_s": 30, "seed": 7, "prompt": "Name a boat.", "model_id": MODEL_ID,
                "max_tokens": 16}),                                          # any key order
])
def test_good_payloads_still_pass(pinned_model: Path, client: TestClient, payload: str) -> None:
    TASK.validate(payload)
    assert post_job(client, payload).status_code == 200


# -- the worker ------------------------------------------------------------------------------

@pytest.fixture
def worker_env(pinned_model: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cli = tmp_path / "llama-cli"
    cli.write_text("#!/bin/sh\necho 'The Salty Hoe'\n")
    cli.chmod(0o755)
    monkeypatch.setenv("LLAMA_CLI", str(cli))
    monkeypatch.setenv("MODEL_DIR", str(pinned_model.parent))
    for name in ("LLM_VK_DEVICE", "LLM_GPU_LAYERS", "LLM_THREADS", "GGML_VK_VISIBLE_DEVICES"):
        monkeypatch.delenv(name, raising=False)
    return cli


def job(payload: str) -> dict:
    return {"job_id": "job-1", "task_type": "llm:generate", "payload": payload}


def test_worker_runs_a_good_job(worker_env: Path) -> None:
    assert demo_worker.run_job(job(llm_payload())) == "The Salty Hoe"


@pytest.mark.parametrize("name", sorted(BAD))
def test_worker_skips_a_job_from_an_older_coordinator_instead_of_crashing(
        worker_env: Path, name: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A job queued before this rule (or by an older coordinator) is skipped with a reason:
    the lease runs out and the job goes back to the queue. The worker keeps going."""
    assert demo_worker.run_job(job(BAD[name][0])) is None
    out = capsys.readouterr().out
    assert "Could not run job" in out and "Name a" not in out  # the prompt is never printed


@pytest.mark.parametrize("name", sorted(BAD))
def test_worker_refuses_the_manifest_with_a_reason(worker_env: Path, name: str) -> None:
    with pytest.raises(demo_worker.ManifestError):
        demo_worker.local_model_sha256(job(BAD[name][0]))


def test_runner_refuses_an_unwritable_prompt_itself(worker_env: Path, pinned_model: Path) -> None:
    """Second line of defence: llm_runner never lets UnicodeEncodeError out."""
    with pytest.raises(llm_runner.RunError, match="not valid text"):
        llm_runner.run(str(worker_env), pinned_model, "boat " + LONE, max_tokens=8, seed=1,
                       timeout_s=5, config=llm_runner.config_from_env())


# -- the private hidden-test pool --------------------------------------------------------

@pytest.mark.parametrize("task_type,payload,expected,needle", [
    ("llm:generate", llm_payload(prompt="boat " + LONE), "The Salty Hoe", "prompt must be valid text"),
    ("demo:sha256", "boat " + LONE, "0" * 64, "payload must be valid text"),
    ("llm:generate", llm_payload(), "The Salty " + LONE, "expected_result must be valid text"),
    ("llm:generate", llm_payload(seed_=1), "The Salty Hoe", "unknown key"),
])
def test_pool_refuses_tests_the_coordinator_would_refuse(
        pinned_model: Path, task_type: str, payload: str, expected: str, needle: str) -> None:
    """`issue` crosses a test off before sending it, so one the coordinator refuses is used
    up for nothing. `add` now refuses it first."""
    reason = challenge_pool.check_test(task_type, payload, expected)
    assert reason is not None and needle in reason


def test_pool_still_takes_a_good_test(pinned_model: Path) -> None:
    assert challenge_pool.check_test("llm:generate", llm_payload(prompt="Name a ship ⛵"),
                                     "The Salty Hoe") is None
