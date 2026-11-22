"""Task executor interface, independent of any GPU backend.

Every job type plugs in by implementing `TaskExecutor`:
  - validate(payload)  -> parsed spec, or raise TaskError   (coordinator, at job creation)
  - available()        -> (bool, reason)                    (worker, before advertising it)
  - execute(payload)   -> result string                     (worker)
  - verify(payload, result) -> Verdict                      (coordinator, at submission)
  - results_agree(a, b) -> bool                             (coordinator, Phase 4 audits)

`Verdict.confidence` is explicit about how much a check proves:
  "exact"          the coordinator recomputed / fully checked the answer
  "schema-only"    the result is well-formed, but correctness is NOT proven; it needs an
                   audit (independent re-run) before it should count for anything real
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import model_registry


class TaskError(ValueError):
    """The payload is invalid for this task type."""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    method: str
    confidence: str  # "exact" | "schema-only"
    reason: str = ""

    @property
    def needs_audit(self) -> bool:
        return self.confidence != "exact"


class TaskExecutor:
    task_type: str = ""

    def validate(self, payload: str) -> Any:
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    def execute(self, payload: str) -> str:
        raise NotImplementedError

    def verify(self, payload: str, result: str) -> Verdict:
        raise NotImplementedError

    def results_agree(self, a: str, b: str) -> bool:
        """Tolerance rule for comparing two independent runs. Default: byte-exact."""
        return a == b


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Sha256Task(TaskExecutor):
    """Deterministic CPU fixture used by tests and the demo. Not useful work."""
    task_type = "demo:sha256"

    def validate(self, payload: str) -> str:
        if not payload:
            raise TaskError("payload must not be empty")
        return payload

    def execute(self, payload: str) -> str:
        return _sha(payload)

    def verify(self, payload: str, result: str) -> Verdict:
        ok = result == _sha(payload)
        return Verdict(ok, "deterministic-recompute", "exact", "" if ok else "wrong answer")


@dataclass(frozen=True)
class InferenceSpec:
    model_id: str
    prompt: str
    max_tokens: int
    seed: int
    timeout_s: int


class LlmGenerateTask(TaskExecutor):
    """Small text generation on a pinned GGUF model via llama.cpp.

    Payload (JSON): {"model_id", "prompt", "max_tokens" (1-256), "seed", "timeout_s" (5-300)}.
    Greedy decoding (temperature 0) with a fixed seed, so honest re-runs on the same
    model + runtime should usually match; Phase 4 audits will rely on that.
    Worker config: LLAMA_CLI (path to llama-cli binary) and MODEL_DIR (folder of model files).
    """
    task_type = "llm:generate"
    MAX_OUTPUT_CHARS = 4000

    def __init__(self, registry: dict[str, model_registry.ModelEntry] | None = None):
        self._registry = registry

    @property
    def registry(self) -> dict[str, model_registry.ModelEntry]:
        return self._registry if self._registry is not None else model_registry.load()

    def validate(self, payload: str) -> InferenceSpec:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TaskError("payload must be JSON") from exc
        if not isinstance(data, dict):
            raise TaskError("payload must be a JSON object")
        model_id = data.get("model_id")
        entry = self.registry.get(model_id) if isinstance(model_id, str) else None
        if entry is None:
            raise TaskError("unknown model_id (must be listed in models/registry.json)")
        if not entry.pinned:
            raise TaskError(f"model {model_id} is not pinned; run model_registry pin first")
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 2000:
            raise TaskError("prompt must be 1-2000 characters")
        max_tokens, seed, timeout_s = data.get("max_tokens", 64), data.get("seed", 0), data.get("timeout_s", 60)
        for name, value, low, high in (("max_tokens", max_tokens, 1, 256), ("seed", seed, 0, 2**31 - 1),
                                       ("timeout_s", timeout_s, 5, 300)):
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise TaskError(f"{name} must be an integer between {low} and {high}")
        return InferenceSpec(model_id, prompt, max_tokens, seed, timeout_s)

    def _binary(self) -> str | None:
        configured = os.getenv("LLAMA_CLI")
        return configured if configured and Path(configured).is_file() else shutil.which("llama-cli")

    def _model_path(self, entry: model_registry.ModelEntry) -> Path:
        return Path(os.getenv("MODEL_DIR", "./models")) / entry.file_name

    def available(self) -> tuple[bool, str]:
        if not self._binary():
            return False, "llama-cli not found (set LLAMA_CLI)"
        pinned = [m for m in self.registry.values() if m.pinned]
        if not pinned:
            return False, "no pinned models in the registry"
        present = [m for m in pinned if self._model_path(m).is_file()]
        if not present:
            return False, "no pinned model files in MODEL_DIR"
        return True, f"{len(present)} model(s) ready"

    def execute(self, payload: str) -> str:  # pragma: no cover - needs real hardware/runtime
        spec = self.validate(payload)
        entry = self.registry[spec.model_id]
        path = self._model_path(entry)
        ok, why = model_registry.verify_file(entry, path)
        if not ok:
            raise TaskError(why)
        binary = self._binary()
        if not binary:
            raise TaskError("llama-cli not available")
        proc = subprocess.run(
            [binary, "-m", str(path), "-p", spec.prompt, "-n", str(spec.max_tokens),
             "--temp", "0", "--seed", str(spec.seed), "--no-display-prompt", "-no-cnv"],
            capture_output=True, text=True, timeout=spec.timeout_s, check=False)
        if proc.returncode != 0:
            raise TaskError(f"llama-cli exited {proc.returncode}")
        return proc.stdout.strip()[: self.MAX_OUTPUT_CHARS]

    def verify(self, payload: str, result: str) -> Verdict:
        if not result.strip():
            return Verdict(False, "schema-check", "schema-only", "empty output")
        if len(result) > self.MAX_OUTPUT_CHARS:
            return Verdict(False, "schema-check", "schema-only", "output too long")
        # Well-formed, but this does NOT prove the model ran. Phase 4 re-run audits do that.
        return Verdict(True, "schema-check", "schema-only")

    def results_agree(self, a: str, b: str) -> bool:
        """Greedy decoding with a fixed seed should repeat token-for-token, but runtimes can
        differ in spacing/newlines. Tolerance: equal after collapsing whitespace. Any other
        difference (a single word or letter case) is a disagreement."""
        return " ".join(a.split()) == " ".join(b.split())


EXECUTORS: dict[str, TaskExecutor] = {t.task_type: t for t in (Sha256Task(), LlmGenerateTask())}


def get(task_type: str) -> TaskExecutor:
    try:
        return EXECUTORS[task_type]
    except KeyError as exc:
        raise TaskError(f"unsupported task type {task_type!r}") from exc
