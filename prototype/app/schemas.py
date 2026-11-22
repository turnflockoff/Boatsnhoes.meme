"""Versioned job and receipt schemas (Track E: schema, input hash, model hash, job version).

Every job manifest the coordinator signs carries:
  schema_version  which manifest layout this is ("bnh-job/1"). A worker refuses any version
                  it doesn't know instead of guessing what the fields mean.
  model_sha256    for model tasks (llm:generate), the sha256 of the exact model file the job
                  was created for, copied from the pinned registry at creation time. Audits
                  and tie-breakers copy it from the original job, so a later re-pin never
                  changes what an existing job means. None for tasks without a model.

Every receipt a worker sends back carries:
  receipt_version     which receipt layout this is ("bnh-receipt/1")
  job_schema_version  the job version the worker ran it as
  payload_hash        sha256 of the input the worker actually ran
  model_sha256        the model file the worker checked before running (None if no model)

The coordinator refuses a receipt whose versions are unknown or whose input/model hashes
don't match the job. This catches honest mistakes (wrong or stale model file, an old worker,
an answer meant for a different job). It is NOT proof: a dishonest worker can copy the
expected values into its receipt without running anything. Audits are what catch that.
"""
from __future__ import annotations

from typing import Any

from .security import sha256_text

JOB_SCHEMA_VERSION = "bnh-job/1"
RECEIPT_SCHEMA_VERSION = "bnh-receipt/1"

# Jobs stored before versioning existed (signed without these fields). Workers refuse them.
LEGACY_JOB_SCHEMA_VERSION = "bnh-job/0"

# Fields covered by the coordinator's Ed25519 signature. Workers must verify exactly these.
SIGNED_FIELDS = ("job_id", "task_type", "payload", "payload_hash", "reward_points",
                 "expires_at", "schema_version", "model_sha256")

# Receipt fields that must equal the job's own values.
RECEIPT_MATCH_FIELDS = (("job_schema_version", "schema_version"),
                        ("payload_hash", "payload_hash"),
                        ("model_sha256", "model_sha256"))


def receipt_problem(receipt: dict[str, Any], job: dict[str, Any]) -> str | None:
    """Return why `receipt` doesn't belong to `job`, or None if it matches."""
    if receipt.get("receipt_version") != RECEIPT_SCHEMA_VERSION:
        return (f"unsupported receipt_version {receipt.get('receipt_version')!r}; "
                f"this coordinator expects {RECEIPT_SCHEMA_VERSION}")
    for receipt_key, job_key in RECEIPT_MATCH_FIELDS:
        if receipt.get(receipt_key) != job.get(job_key):
            return f"{receipt_key} does not match the job"
    return None


def receipt_fields(job: dict[str, Any], model_sha256: str | None) -> dict[str, Any]:
    """What an honest worker adds to its result: the versions and hashes it actually used.
    `model_sha256` must be the hash of the model file the worker checked, not just a copy
    of the job's value."""
    return {"receipt_version": RECEIPT_SCHEMA_VERSION,
            "job_schema_version": job["schema_version"],
            "payload_hash": sha256_text(job["payload"]),  # hashed here, not copied
            "model_sha256": model_sha256}
