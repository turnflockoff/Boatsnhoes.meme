"""`llm-run-v1`: how a worker starts llama-cli for an `llm:generate` job.

Every test uses a small fake llama-cli (a Python script written into tmp_path). Nothing here
runs a model or touches a GPU; it checks what the worker hands the program and how it
reacts when the program misbehaves.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from app import llm_runner, model_registry, tasks
from demo_worker import run_job

MODEL_ID = "tiny-test-model"
PROMPT = "Name a boat. (hidden test 42)"


@pytest.fixture
def pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"models": [
        {"model_id": MODEL_ID, "file_name": "tiny.gguf", "source_url": "https://example.invalid/t",
         "license": "Apache-2.0", "sha256": None, "size_bytes": None, "min_vram_mb": 1}]}))
    monkeypatch.setenv("MODEL_REGISTRY", str(reg))
    models = tmp_path / "models"
    models.mkdir()
    model = models / "tiny.gguf"
    model.write_bytes(b"GGUF fake weights for tests")
    model_registry.pin(MODEL_ID, model)
    monkeypatch.setenv("MODEL_DIR", str(models))
    for name in ("LLM_VK_DEVICE", "LLM_GPU_LAYERS", "LLM_THREADS", "GGML_VK_VISIBLE_DEVICES"):
        monkeypatch.delenv(name, raising=False)
    return model


def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str = "") -> Path:
    """A fake llama-cli that records what it was given into record.json, then runs `body`
    (default: print a short answer). The record path is baked in, because the fake gets
    only the allow-listed environment."""
    record = tmp_path / "record.json"
    script = tmp_path / "llama-cli"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
prompt_file = argv[argv.index("-f") + 1] if "-f" in argv else None
info = {{
    "argv": argv,
    "env": dict(os.environ),
    "stdin": sys.stdin.read() if sys.stdin is not None else None,
    "cwd": os.getcwd(),
    "prompt": open(prompt_file, "rb").read().decode("utf-8") if prompt_file else None,
    "prompt_mode": oct(os.stat(prompt_file).st_mode & 0o777) if prompt_file else None,
    "dir_mode": oct(os.stat(os.path.dirname(prompt_file)).st_mode & 0o777) if prompt_file else None,
    "prompt_file": prompt_file,
}}
open({str(record)!r}, "w").write(json.dumps(info))
{body or 'print("  The Salty Gull  ")'}
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("LLAMA_CLI", str(script))
    return record


def payload(**overrides) -> str:
    return json.dumps({"model_id": MODEL_ID, "prompt": PROMPT, "max_tokens": 16, "seed": 7,
                       "timeout_s": 30} | overrides)


def run_direct(cli: Path, model: Path, timeout_s: float = 10, prompt: str = PROMPT) -> str:
    return llm_runner.run(str(cli), model, prompt, max_tokens=8, seed=1, timeout_s=timeout_s,
                          config=llm_runner.config_from_env({}))


def test_prompt_goes_in_a_private_file_not_the_command_line(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = fake_cli(tmp_path, monkeypatch)
    out = tasks.LlmGenerateTask().execute(payload())
    assert out == "The Salty Gull"
    info = json.loads(record.read_text())
    assert info["prompt"] == PROMPT
    assert all(PROMPT not in arg and "hidden test" not in arg for arg in info["argv"])
    assert info["prompt_mode"] == "0o600" and info["dir_mode"] == "0o700"
    assert info["cwd"] == os.path.dirname(info["prompt_file"])
    assert not os.path.exists(info["prompt_file"])  # deleted after the run
    assert not os.path.exists(os.path.dirname(info["prompt_file"]))


def test_command_has_fixed_settings(pinned: Path, tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    record = fake_cli(tmp_path, monkeypatch)
    tasks.LlmGenerateTask().execute(payload(max_tokens=16, seed=7))
    argv = json.loads(record.read_text())["argv"]

    def flag(name: str) -> str:
        return argv[argv.index(name) + 1]

    assert flag("-m") == str(pinned)
    assert (flag("-n"), flag("-c"), flag("-ngl"), flag("--temp"), flag("--seed")) == \
        ("16", "2048", "99", "0", "7")
    assert "--no-display-prompt" in argv and "-no-cnv" in argv
    assert "-t" not in argv and "-p" not in argv


def test_threads_and_gpu_layers_come_from_worker_settings(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_THREADS", "4")
    monkeypatch.setenv("LLM_GPU_LAYERS", "0")
    tasks.LlmGenerateTask().execute(payload())
    argv = json.loads(record.read_text())["argv"]
    assert argv[argv.index("-t") + 1] == "4" and argv[argv.index("-ngl") + 1] == "0"


def test_secrets_in_the_worker_environment_never_reach_llama_cli(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = fake_cli(tmp_path, monkeypatch)
    secrets = {"OPERATOR_TOKEN": "op-secret", "SUBMITTER_TOKEN": "sub-secret",
               "BNH_ENROLLMENT_CODE": "code-secret", "AWS_SECRET_ACCESS_KEY": "aws",
               "GITHUB_TOKEN": "gh", "MESA_API_KEY": "sneaky", "COORDINATOR_KEY_PATH": "/k",
               "SOMETHING_ELSE": "x"}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MESA_SHADER_CACHE_DIR", "/tmp/mesa")
    monkeypatch.setenv("RADV_PERFTEST", "sam")
    monkeypatch.setenv("VK_ICD_FILENAMES", "/usr/share/vulkan/icd.d/radeon_icd.x86_64.json")
    monkeypatch.setenv("LANG", "C.UTF-8")
    tasks.LlmGenerateTask().execute(payload())
    env = json.loads(record.read_text())["env"]
    for name, value in secrets.items():
        assert name not in env
        assert value not in env.values()
    assert env["MESA_SHADER_CACHE_DIR"] == "/tmp/mesa" and env["RADV_PERFTEST"] == "sam"
    assert env["VK_ICD_FILENAMES"].endswith("radeon_icd.x86_64.json")
    assert env["LANG"] == "C.UTF-8" and env["PATH"] == os.environ["PATH"]
    allowed = llm_runner._PASS_NAMES
    assert all(n in allowed or n.startswith(llm_runner._PASS_PREFIXES) or n == "LC_CTYPE"
               for n in env)  # LC_CTYPE: Python itself may set it in the child


def test_one_card_per_worker(pinned: Path, tmp_path: Path,
                             monkeypatch: pytest.MonkeyPatch) -> None:
    record = fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("GGML_VK_VISIBLE_DEVICES", "0")  # overridden by the worker setting
    monkeypatch.setenv("LLM_VK_DEVICE", "1")
    tasks.LlmGenerateTask().execute(payload())
    assert json.loads(record.read_text())["env"]["GGML_VK_VISIBLE_DEVICES"] == "1"


def test_without_a_card_setting_llama_cpp_picks(pinned: Path, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    record = fake_cli(tmp_path, monkeypatch)
    tasks.LlmGenerateTask().execute(payload())
    assert "GGML_VK_VISIBLE_DEVICES" not in json.loads(record.read_text())["env"]


@pytest.mark.parametrize("name,value", [
    ("LLM_VK_DEVICE", "-1"), ("LLM_VK_DEVICE", "one"), ("LLM_VK_DEVICE", "16"),
    ("LLM_GPU_LAYERS", "1.5"), ("LLM_GPU_LAYERS", "1000"), ("LLM_THREADS", "0"),
    ("LLM_THREADS", "65"), ("LLM_THREADS", "4 "),
])
def test_bad_settings_stop_the_worker_offering_ai_jobs(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
        value: str) -> None:
    record = fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv(name, value)
    if value == "4 ":  # surrounding spaces are fine
        assert tasks.LlmGenerateTask().available()[0]
        return
    ok, why = tasks.LlmGenerateTask().available()
    assert not ok and name in why
    with pytest.raises(tasks.TaskError, match=name):
        tasks.LlmGenerateTask().execute(payload())
    assert not record.exists()


def test_stdin_is_closed(pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if the worker's own input has something waiting (typed text, a pipe), llama-cli
    reads nothing, so it can never sit waiting for input until the timeout."""
    record = fake_cli(tmp_path, monkeypatch)
    read_end, write_end = os.pipe()
    os.write(write_end, b"typed by someone\n")
    os.close(write_end)
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        started = time.monotonic()
        tasks.LlmGenerateTask().execute(payload())
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read_end)
    assert json.loads(record.read_text())["stdin"] == ""
    assert time.monotonic() - started < 10


def test_timeout_stops_llama_cli_and_what_it_started(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "child.pid"
    fake_cli(tmp_path, monkeypatch, body=f"""
import subprocess, time
child = subprocess.Popen(["sleep", "60"])
open({str(pid_file)!r}, "w").write(str(child.pid))
time.sleep(60)
""")
    started = time.monotonic()
    with pytest.raises(llm_runner.RunError, match="longer than 1 s"):
        run_direct(Path(os.environ["LLAMA_CLI"]), pinned, timeout_s=1)
    assert time.monotonic() - started < 10
    child = int(pid_file.read_text())
    for _ in range(50):
        try:
            state = Path(f"/proc/{child}/stat").read_text().split(")")[-1].split()[0]
        except FileNotFoundError:
            break
        if state == "Z":  # killed, waiting to be reaped by init
            break
        time.sleep(0.1)
    else:
        pytest.fail("the program llama-cli started is still running")


def test_timeout_from_a_job_becomes_a_task_error(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cli(tmp_path, monkeypatch, body="import time; time.sleep(60)")
    monkeypatch.setattr(llm_runner, "run", _short_timeout(llm_runner.run))
    with pytest.raises(tasks.TaskError, match="longer than"):
        tasks.LlmGenerateTask().execute(payload(timeout_s=5))


def _short_timeout(real):
    def wrapper(*args, **kwargs):
        kwargs["timeout_s"] = 0.5
        return real(*args, **kwargs)
    return wrapper


def test_failed_run_reports_exit_code_without_the_prompt(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cli(tmp_path, monkeypatch,
             body="sys.stderr.write(open(prompt_file).read()); sys.exit(3)")
    with pytest.raises(tasks.TaskError) as caught:
        tasks.LlmGenerateTask().execute(payload())
    assert str(caught.value) == "llama-cli exited 3"
    assert "hidden test" not in str(caught.value)


def test_runaway_output_is_stopped(pinned: Path, tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cli(tmp_path, monkeypatch, body="""
import time
while True:
    sys.stdout.write("x" * 8192); sys.stdout.flush()
""")
    started = time.monotonic()
    with pytest.raises(tasks.TaskError, match="more than 64 KiB"):
        tasks.LlmGenerateTask().execute(payload())
    assert time.monotonic() - started < 10


def test_long_but_allowed_output_is_trimmed_to_the_limit(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cli(tmp_path, monkeypatch, body='print("\\n  " + "y" * 5000)')
    out = tasks.LlmGenerateTask().execute(payload())
    assert out == "y" * tasks.LlmGenerateTask.MAX_OUTPUT_CHARS


def test_bad_bytes_in_output_do_not_crash(pinned: Path, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cli(tmp_path, monkeypatch, body='sys.stdout.buffer.write(b"ok \\xff\\xfe done")')
    assert tasks.LlmGenerateTask().execute(payload()) == "ok �� done"


def test_prompt_with_a_nul_character_works(pinned: Path, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    record = fake_cli(tmp_path, monkeypatch)
    tasks.LlmGenerateTask().execute(payload(prompt="a\u0000b"))
    assert json.loads(record.read_text())["prompt"] == "a\u0000b"


def test_changed_model_file_is_refused_before_running(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = fake_cli(tmp_path, monkeypatch)
    pinned.write_bytes(b"GGUF fake weights for TESTS")  # same size, different bytes
    with pytest.raises(tasks.TaskError, match="sha256 does not match"):
        tasks.LlmGenerateTask().execute(payload())
    assert not record.exists()


def test_program_that_cannot_start_is_a_task_error(pinned: Path, tmp_path: Path) -> None:
    broken = tmp_path / "not-a-program"
    broken.write_text("just text")
    broken.chmod(0o600)
    with pytest.raises(llm_runner.RunError, match="could not start"):
        run_direct(broken, pinned)


def test_worker_skips_a_job_it_cannot_run_instead_of_crashing(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    fake_cli(tmp_path, monkeypatch, body="sys.exit(2)")
    job = {"job_id": "job-1", "task_type": "llm:generate", "payload": payload()}
    assert run_job(job) is None
    printed = capsys.readouterr().out
    assert "Could not run job job-1: llama-cli exited 2" in printed
    assert "hidden test" not in printed


def test_worker_returns_the_answer_when_the_run_works(
        pinned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cli(tmp_path, monkeypatch)
    job = {"job_id": "job-2", "task_type": "llm:generate", "payload": payload()}
    assert run_job(job) == "The Salty Gull"
