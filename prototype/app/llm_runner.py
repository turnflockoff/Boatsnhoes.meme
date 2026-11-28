"""Run llama.cpp for one `llm:generate` job, with limits (rule `llm-run-v1`).

`LlmGenerateTask.execute` used to call llama-cli directly with the prompt on the command
line and the worker's whole environment. This module is the safer way to start it:

* **The prompt goes in a private file, never on the command line.** Any local user can read
  another process's command line (`ps`, /proc/<pid>/cmdline), and a hidden test's prompt is
  exactly what a cheater wants to recognise. The file lives in a fresh folder only this user
  can open (0700, file 0600) and is deleted when the run ends. A prompt containing a NUL
  character, which can't go on a command line at all, now works too.
* **llama-cli gets a short list of environment variables, not the worker's.** The worker loads
  `.env`, which on the owner's machine may hold `OPERATOR_TOKEN`, `SUBMITTER_TOKEN` or an
  enrollment code. A third-party program has no business seeing them. It gets only PATH, HOME,
  locale and temp settings, and GPU driver knobs (`VK_ICD_FILENAMES`, `VK_DRIVER_FILES`,
  `MESA_*`, `RADV_*`, `GGML_VK_*`). Anything whose name looks like a secret is dropped even
  from that list.
* **One card per worker.** `LLM_VK_DEVICE=1` sets `GGML_VK_VISIBLE_DEVICES=1` for llama-cli,
  so the worker for card 1 can't drift onto card 0. Unset = llama.cpp picks.
* **Fixed, predictable settings:** context size 2048 for every run (so VRAM use on a 4 GB card
  is predictable and every rig runs the same way), `-ngl` from `LLM_GPU_LAYERS` (default 99 =
  everything on the GPU; 0 = CPU only), optional `-t` from `LLM_THREADS`.
* **Bounded:** stdin is closed (so it can never sit waiting for typing), stdout is read up to
  64 KiB and the run is stopped if it prints more, and on timeout the whole process group is
  stopped, not just llama-cli. Its log (stderr) is thrown away, because it can repeat the prompt.

Every failure raises `TaskError` with a short reason that never contains the prompt.

Honest limits: tested only with a fake llama-cli script (no GPU here). The flag names match
llama.cpp's `llama-cli`; newer llama.cpp builds may move plain completion to a separate
`llama-completion` program, which takes the same flags (point `LLAMA_CLI` at it). Phase 3 on
the real rig has to confirm which build works on the RX 470s.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

RULE = "llm-run-v1"
CTX_SIZE = 2048
DEFAULT_GPU_LAYERS = 99
MAX_STDOUT_BYTES = 64 * 1024
MAX_VK_DEVICE = 15
MAX_GPU_LAYERS = 999
MAX_THREADS = 64

_PASS_NAMES = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "XDG_RUNTIME_DIR",
    "VK_ICD_FILENAMES", "VK_DRIVER_FILES",
})
_PASS_PREFIXES = ("MESA_", "RADV_", "GGML_VK_")
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASS|KEY|CREDENTIAL|AUTH|COOKIE|CODE", re.IGNORECASE)


class RunError(ValueError):
    """The run could not be started or did not finish cleanly. The message never holds the
    prompt. `tasks.LlmGenerateTask` turns this into a `TaskError`."""


@dataclass(frozen=True)
class RunnerConfig:
    vk_device: int | None
    gpu_layers: int
    threads: int | None


def _int_setting(env: Mapping[str, str], name: str, low: int, high: int,
                 default: int | None) -> int | None:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not re.fullmatch(r"[0-9]+", raw) or not low <= int(raw) <= high:
        raise RunError(f"{name} must be a whole number from {low} to {high} (got {raw!r})")
    return int(raw)


def config_from_env(env: Mapping[str, str] | None = None) -> RunnerConfig:
    """Read LLM_VK_DEVICE, LLM_GPU_LAYERS and LLM_THREADS. Raises RunError on a bad value, so
    a typo stops the worker from advertising llm:generate instead of running on the wrong card."""
    env = os.environ if env is None else env
    return RunnerConfig(
        vk_device=_int_setting(env, "LLM_VK_DEVICE", 0, MAX_VK_DEVICE, None),
        gpu_layers=_int_setting(env, "LLM_GPU_LAYERS", 0, MAX_GPU_LAYERS, DEFAULT_GPU_LAYERS),
        threads=_int_setting(env, "LLM_THREADS", 1, MAX_THREADS, None),
    )


def child_env(config: RunnerConfig, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The only environment llama-cli sees."""
    env = os.environ if env is None else env
    out = {name: value for name, value in env.items()
           if (name in _PASS_NAMES or name.startswith(_PASS_PREFIXES))
           and not _SECRET_NAME.search(name)}
    if config.vk_device is not None:
        out["GGML_VK_VISIBLE_DEVICES"] = str(config.vk_device)
    return out


def build_command(binary: str, model_path: Path, prompt_file: Path, *, max_tokens: int,
                  seed: int, config: RunnerConfig) -> list[str]:
    cmd = [binary, "-m", str(model_path), "-f", str(prompt_file), "-n", str(max_tokens),
           "-c", str(CTX_SIZE), "-ngl", str(config.gpu_layers), "--temp", "0",
           "--seed", str(seed), "--no-display-prompt", "-no-cnv"]
    if config.threads is not None:
        cmd += ["-t", str(config.threads)]
    return cmd


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Stop llama-cli and anything it started (it runs in its own process group)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def run(binary: str, model_path: Path, prompt: str, *, max_tokens: int, seed: int,
        timeout_s: float, config: RunnerConfig,
        env: Mapping[str, str] | None = None) -> str:
    """Run one generation and return its raw stdout (decoded, not trimmed)."""
    try:
        prompt_bytes = prompt.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate (`llm-payload-v2`): refused at job creation since 0.5.78, but a
        # job queued by an older coordinator must not stop the worker either.
        raise RunError("prompt is not valid text (lone surrogate)") from None
    with tempfile.TemporaryDirectory(prefix="bnh-llm-") as tmp:  # created 0700
        prompt_file = Path(tmp) / "prompt.txt"
        fd = os.open(prompt_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(prompt_bytes)
        cmd = build_command(binary, model_path, prompt_file, max_tokens=max_tokens, seed=seed,
                            config=config)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=child_env(config, env),
                                    cwd=tmp, start_new_session=True)
        except OSError as exc:
            raise RunError(f"could not start llama-cli ({exc.strerror or type(exc).__name__})") \
                from None

        chunks: list[bytes] = []
        too_much = threading.Event()

        def read_output() -> None:
            assert proc.stdout is not None
            size = 0
            while chunk := proc.stdout.read(8192):
                size += len(chunk)
                if size > MAX_STDOUT_BYTES:
                    too_much.set()
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    return
                chunks.append(chunk)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout_s)
            try:  # anything llama-cli started and left behind goes too
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        except subprocess.TimeoutExpired:  # `finally` below stops it
            raise RunError(f"llama-cli took longer than {timeout_s:g} s and was stopped") \
                from None
        finally:
            if proc.poll() is None:  # timed out, or Ctrl-C while waiting
                _stop(proc)
            reader.join(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()
        if too_much.is_set():
            raise RunError(f"llama-cli printed more than {MAX_STDOUT_BYTES // 1024} KiB and "
                           "was stopped")
        if proc.returncode != 0:
            raise RunError(f"llama-cli exited {proc.returncode}")
        return b"".join(chunks).decode("utf-8", errors="replace")
