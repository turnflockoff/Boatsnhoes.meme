"""Task executor interface, independent of any GPU backend.

Every job type plugs in by implementing `TaskExecutor`:
  - validate(payload)  -> parsed spec, or raise TaskError   (coordinator, at job creation)
  - available()        -> (bool, reason)                    (worker, before advertising it)
  - execute(payload)   -> result string                     (worker)
  - verify(payload, result) -> Verdict                      (coordinator, at submission)
  - results_agree(a, b) -> bool                             (coordinator, Phase 4 audits)
  - model_sha256(payload) -> pinned model hash or None      (both: goes into the signed job
                                                             and is echoed in the receipt)
  - recognisable_text(payload) -> the part a rig would remember (coordinator, hidden-test
                                  reuse check; see `input_fingerprint`)

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
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import llm_runner, model_registry


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

    def model_sha256(self, payload: str) -> str | None:
        """sha256 of the pinned model file this payload needs, or None for tasks that use
        no model. Read from *this machine's* registry, so coordinator and worker can compare."""
        return None

    def recognisable_text(self, payload: str) -> str:
        """The part of the input a rig would recognise if it saw it again. Default: all of
        it. Never raises: it is also used on old rows whose payload may not validate today."""
        return payload

    def run_limit_seconds(self, payload: str) -> int:
        """`lease-fit-v1`: the longest an honest worker may spend on this job after claiming
        it (checking files, running, stopping the program), not counting the network.
        0 means the task is near-instant and the plain LEASE_SECONDS lease is enough.
        Never raises: it is also used when a stored job is handed out."""
        return 0


# `lease-fit-v1`: a lease must outlast the job's own time limit. Before this, every lease was
# LEASE_SECONDS (default 60) while an `llm:generate` job may run for up to 300 s, so an honest
# run that took longer than the lease was thrown away, counted against the rig as a lease
# timeout, and after MAX_ATTEMPTS rigs the job failed.
LEASE_RULE = "lease-fit-v1"
LEASE_GRACE_SECONDS = 30            # claim reply, signature check, submit and one retry
MODEL_CHECK_SECONDS_PER_GIB = 30    # worker re-hashes the model file (~34 MB/s, a slow disk)


def lease_needed(task_type: str, payload: str) -> int:
    """Seconds a lease on this job must last: the task's run limit plus LEASE_GRACE_SECONDS,
    or 0 for near-instant tasks (and unknown types). The coordinator leases for
    max(LEASE_SECONDS, this), and refuses a new job whose ttl_seconds is shorter."""
    executor = EXECUTORS.get(task_type)
    limit = executor.run_limit_seconds(payload) if executor is not None else 0
    return limit + LEASE_GRACE_SECONDS if limit > 0 else 0


def _sha(value: str) -> str:
    # "surrogatepass" changes nothing for real text; it only keeps a lone surrogate (which
    # `llm-payload-v2` refuses in new jobs) from raising inside the fingerprint, which is
    # documented as never raising.
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


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


PAYLOAD_RULE = "llm-payload-v2"


class _RepeatedKey(ValueError):
    pass


def _no_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`json.loads` hook: a key written twice is refused. Python keeps the last copy, other
    readers may keep the first, so `{"prompt": "a", "prompt": "b"}` could be one question
    to the coordinator and another to whoever reads the job (same rule as `json_file`)."""
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _RepeatedKey(_short(key))
        out[key] = value
    return out


def _short(text: str, limit: int = 40) -> str:
    """A key named in an error message: printable ASCII only, cut to `limit`. The message
    goes back to the submitter and into the operator's logs."""
    clean = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)
    return clean if len(clean) <= limit else clean[:limit] + "..."


def is_text(value: str) -> bool:
    """True if `value` can be written as UTF-8: no lone surrogates (U+D800-U+DFFF), which
    JSON escapes can produce but no file, database or socket text can hold."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@dataclass(frozen=True)
class InferenceSpec:
    model_id: str
    prompt: str
    max_tokens: int
    seed: int
    timeout_s: int


class LlmGenerateTask(TaskExecutor):
    """Small text generation on a pinned GGUF model via llama.cpp.

    Payload (JSON): {"model_id", "prompt", "max_tokens" (1-256), "seed", "timeout_s" (5-300)},
    no other keys, each at most once, prompt valid text (`llm-payload-v2`).
    Greedy decoding (temperature 0) with a fixed seed, so honest re-runs on the same
    model + runtime should usually match; Phase 4 audits will rely on that.
    Worker config: LLAMA_CLI (path to llama-cli binary) and MODEL_DIR (folder of model files);
    LLM_VK_DEVICE, LLM_GPU_LAYERS and LLM_THREADS (see app/llm_runner.py).
    """
    task_type = "llm:generate"
    MAX_OUTPUT_CHARS = 4000
    MIN_TIMEOUT_S, DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S = 5, 60, 300

    def __init__(self, registry: dict[str, model_registry.ModelEntry] | None = None):
        self._registry = registry

    @property
    def registry(self) -> dict[str, model_registry.ModelEntry]:
        if self._registry is not None:
            return self._registry
        try:
            return model_registry.load()
        except model_registry.RegistryError as exc:
            # A damaged registry refuses jobs with a reason instead of crashing (model-fetch-v1).
            raise TaskError(f"model registry refused: {exc}") from None

    # `llm-payload-v2`: the only keys a payload may have. Anything else used to be ignored
    # without a word, so a typo (`"max_token": 200`) or a setting this task doesn't have
    # (`"temperature": 0.8`) ran with the defaults while the job looked like it asked for more.
    PAYLOAD_KEYS = ("model_id", "prompt", "max_tokens", "seed", "timeout_s")

    def validate(self, payload: str) -> InferenceSpec:
        """Parse and check a payload (`llm-payload-v2`). Raises only TaskError, whatever the
        payload: the worker runs this first, so anything else here would stop the worker."""
        try:
            data = json.loads(payload, object_pairs_hook=_no_repeated_keys)
        except _RepeatedKey as exc:
            raise TaskError(f"payload has the key {exc.args[0]!r} twice") from None
        except (ValueError, TypeError, RecursionError):
            # JSONDecodeError is a ValueError; so is a 5,000-digit number. RecursionError: a
            # payload nested thousands of brackets deep.
            raise TaskError("payload must be JSON") from None
        if not isinstance(data, dict):
            raise TaskError("payload must be a JSON object")
        unknown = sorted(set(data) - set(self.PAYLOAD_KEYS))
        if unknown:
            raise TaskError(f"payload has unknown key {_short(unknown[0])!r} "
                            f"(allowed: {', '.join(self.PAYLOAD_KEYS)})")
        model_id = data.get("model_id")
        entry = self.registry.get(model_id) if isinstance(model_id, str) else None
        if entry is None:
            raise TaskError("unknown model_id (must be listed in models/registry.json)")
        if not entry.pinned:
            raise TaskError(f"model {model_id} is not pinned; run model_registry pin first")
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 2000:
            raise TaskError("prompt must be 1-2000 characters")
        if not is_text(prompt):
            # JSON's "\ud800" escape decodes to half of a character pair, which can't be
            # written as UTF-8. The coordinator used to accept it and sign the job, and every
            # worker that took it crashed writing the prompt file (UnicodeEncodeError).
            raise TaskError("prompt must be valid text (it has half of a character pair, "
                            "a \\ud800-\\udfff escape on its own)")
        max_tokens, seed = data.get("max_tokens", 64), data.get("seed", 0)
        timeout_s = data.get("timeout_s", self.DEFAULT_TIMEOUT_S)
        for name, value, low, high in (("max_tokens", max_tokens, 1, 256), ("seed", seed, 0, 2**31 - 1),
                                       ("timeout_s", timeout_s, self.MIN_TIMEOUT_S,
                                        self.MAX_TIMEOUT_S)):
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise TaskError(f"{name} must be an integer between {low} and {high}")
        return InferenceSpec(model_id, prompt, max_tokens, seed, timeout_s)

    def model_sha256(self, payload: str) -> str | None:
        spec = self.validate(payload)  # raises TaskError if the model isn't pinned here
        return self.registry[spec.model_id].sha256

    def recognisable_text(self, payload: str) -> str:
        """Only the prompt: the same question with another seed, max_tokens, timeout or
        model is still the same question to a rig that has answered it before. Parsed
        without the model registry, so it works on any stored payload."""
        try:
            data = json.loads(payload)
        except (ValueError, TypeError, RecursionError):
            return payload
        prompt = data.get("prompt") if isinstance(data, dict) else None
        return prompt if isinstance(prompt, str) else payload

    def run_limit_seconds(self, payload: str) -> int:
        """`lease-fit-v1`: the job's own `timeout_s` (llama-cli is stopped after it) plus time
        for the worker to re-hash the pinned model file before running it
        (MODEL_CHECK_SECONDS_PER_GIB of its pinned size, rounded up). A payload that doesn't
        parse gets the longest timeout, so a doubt makes the lease longer, never shorter."""
        try:
            data = json.loads(payload)
        except (ValueError, TypeError, RecursionError):
            data = None
        if not isinstance(data, dict):
            return self.MAX_TIMEOUT_S
        timeout_s = data.get("timeout_s", self.DEFAULT_TIMEOUT_S)
        if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) \
                or not self.MIN_TIMEOUT_S <= timeout_s <= self.MAX_TIMEOUT_S:
            timeout_s = self.MAX_TIMEOUT_S
        size = 0
        model_id = data.get("model_id")
        if isinstance(model_id, str):
            try:
                entry = self.registry.get(model_id)
            except TaskError:  # a damaged registry: the job can't run anyway
                entry = None
            size = (entry.size_bytes or 0) if entry is not None else 0
        check = -(-size * MODEL_CHECK_SECONDS_PER_GIB // 2**30)  # ceiling division
        return timeout_s + check

    def _binary(self) -> str | None:
        configured = os.getenv("LLAMA_CLI")
        return configured if configured and Path(configured).is_file() else shutil.which("llama-cli")

    def _model_path(self, entry: model_registry.ModelEntry) -> Path:
        return Path(os.getenv("MODEL_DIR", "./models")) / entry.file_name

    def available(self) -> tuple[bool, str]:
        if not self._binary():
            return False, "llama-cli not found (set LLAMA_CLI)"
        try:
            llm_runner.config_from_env()
        except llm_runner.RunError as exc:
            return False, str(exc)
        try:
            pinned = [m for m in self.registry.values() if m.pinned]
        except TaskError as exc:
            return False, str(exc)
        if not pinned:
            return False, "no pinned models in the registry"
        present = [m for m in pinned if self._model_path(m).is_file()]
        if not present:
            return False, "no pinned model files in MODEL_DIR"
        return True, f"{len(present)} model(s) ready"

    def execute(self, payload: str) -> str:
        """Run the job through `llm_runner` (rule `llm-run-v1`): prompt in a private file,
        a short allow-list environment, one card via LLM_VK_DEVICE, fixed context size,
        bounded output and time. Raises TaskError with a reason that never holds the prompt."""
        spec = self.validate(payload)
        entry = self.registry[spec.model_id]
        path = self._model_path(entry)
        ok, why = model_registry.verify_file(entry, path)
        if not ok:
            raise TaskError(why)
        binary = self._binary()
        if not binary:
            raise TaskError("llama-cli not available")
        try:
            config = llm_runner.config_from_env()
            out = llm_runner.run(binary, path, spec.prompt, max_tokens=spec.max_tokens,
                                 seed=spec.seed, timeout_s=spec.timeout_s, config=config)
        except llm_runner.RunError as exc:
            raise TaskError(str(exc)) from None
        return out.strip()[: self.MAX_OUTPUT_CHARS]

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


# Input fingerprint (`input-fingerprint-v1`, threat model #11). Used to tell whether a new
# hidden test is a near copy of one the rigs have already seen as a test. Two inputs get the
# same fingerprint when their recognisable text differs only in letter case, spacing,
# punctuation or symbols, invisible characters, or Unicode look-alike forms that NFKC folds
# (full-width letters, ligatures). It is deliberately broad: refusing a test that merely
# looks like an old one costs the operator a rewrite, while letting a recognisable one
# through tells a cheating rig which job to answer honestly. It does not catch rewording
# ("Name a ship" vs "Give a ship's name") or look-alike letters from other alphabets.
FINGERPRINT_RULE = "input-fingerprint-v1"


def normalise_text(text: str) -> str:
    """Letters and digits only, after NFKC and case folding. Spaces go too, so "2+2",
    "2 + 2" and "don't"/"dont" fold together; zero-width and other invisible characters
    are not letters, so they can't split a word into a "new" input."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def input_fingerprint(task_type: str, payload: str) -> str:
    """sha256 of the task type plus the normalised recognisable text. Unknown task types
    and text with no letters or digits fall back to the raw text, so they only ever match
    themselves exactly."""
    executor = EXECUTORS.get(task_type)
    text = executor.recognisable_text(payload) if executor is not None else payload
    normal = normalise_text(text)
    key = f"n:{normal}" if normal else f"r:{text}"
    return _sha(f"{FINGERPRINT_RULE}\n{task_type}\n{key}")


def get(task_type: str) -> TaskExecutor:
    try:
        return EXECUTORS[task_type]
    except KeyError as exc:
        raise TaskError(f"unsupported task type {task_type!r}") from exc
