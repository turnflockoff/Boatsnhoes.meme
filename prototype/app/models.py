from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

# `input-limits-v1`: worker names and capability names are plain ASCII, so a name can't
# hide a newline, a NUL, a right-to-left override or HTML in logs, reports or pages.
WORKER_ID_PATTERN = r"^[A-Za-z0-9._:-]{3,80}$"
Capability = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9:._-]{0,63}$")]
MAX_CAPABILITIES = 16
# Same bound as app/input_limits.MAX_TIMESTAMP (2^53): any real date fits, SQLite can store it.
MAX_TIMESTAMP = 2 ** 53


class WorkerRegistration(BaseModel):
    worker_id: str = Field(pattern=WORKER_ID_PATTERN)
    capabilities: list[Capability] = Field(default_factory=lambda: ["demo:sha256"],
                                           max_length=MAX_CAPABILITIES)
    # One-time code from the operator. Needed for a new worker; an existing worker
    # updates its capabilities by sending its key instead.
    enrollment_code: str | None = Field(default=None, min_length=16, max_length=200)


class EnrollmentCreate(BaseModel):
    """Operator-only. `owner` names the person or group who runs the rig. Workers with the
    same owner never audit or break ties on each other's results."""
    owner: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    ttl_seconds: int = Field(default=86400, ge=60, le=7 * 86400)


class PauseRequest(BaseModel):
    """Operator-only emergency pause (`pause-v1`, app/incident.py). The reason is kept in
    the audit trail and shown to the operator; it is never public or logged."""
    reason: str = Field(min_length=1, max_length=200)


class JobCreate(BaseModel):
    task_type: str = Field(default="demo:sha256", pattern="^(demo:sha256|llm:generate)$")
    payload: str = Field(min_length=1, max_length=4096)
    # Leave out to get the default: 1, or the task type's number when POINTS_TABLE is set
    # (app/points.py). With a table, any other number is refused.
    reward_points: int | None = Field(default=None, ge=0, le=1000)
    ttl_seconds: int = Field(default=300, ge=10, le=3600)


class ChallengeCreate(JobCreate):
    """A hidden known-answer job. `expected_result` is stored but never sent to workers.

    By default any worker may take it (first-come). To aim it (threat model #1 and #8),
    set at most one of:
      target_worker  only this worker can take it, until the job's own deadline
      target_tier    a random active worker in this trust tier (`audit-tier-v1`:
                     new, suspect or established), worked out when it is handed out
    Workers can't tell an aimed test from normal work either."""
    expected_result: str = Field(min_length=1, max_length=8192)
    target_worker: str | None = Field(default=None, pattern=WORKER_ID_PATTERN)
    target_tier: Literal["new", "suspect", "established"] | None = None

    @model_validator(mode="after")
    def _one_target(self) -> "ChallengeCreate":
        if self.target_worker is not None and self.target_tier is not None:
            raise ValueError("set target_worker or target_tier, not both")
        return self

    def target(self) -> str | None:
        if self.target_worker is not None:
            return f"worker:{self.target_worker}"
        if self.target_tier is not None:
            return f"tier:{self.target_tier}"
        return None


class JobEnvelope(BaseModel):
    job_id: str
    task_type: str
    payload: str
    payload_hash: str
    reward_points: int
    expires_at: int
    # Versioned manifest (see app/schemas.py). Both are covered by the signature.
    schema_version: str
    model_sha256: str | None = None
    signature: str


class ResultSubmission(BaseModel):
    # Optional: the worker is identified by its key. If sent, it must match the key.
    worker_id: str | None = Field(default=None, min_length=3, max_length=80)
    result: str = Field(min_length=1, max_length=8192)
    result_hash: str = Field(pattern="^[0-9a-f]{64}$")
    started_at: int = Field(ge=0, le=MAX_TIMESTAMP)
    completed_at: int = Field(ge=0, le=MAX_TIMESTAMP)
    # Versioned receipt (see app/schemas.py). Optional here so an outdated worker gets a
    # clear "unsupported receipt_version" message instead of a bare validation error.
    receipt_version: str | None = Field(default=None, max_length=32)
    job_schema_version: str | None = Field(default=None, max_length=32)
    payload_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    model_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
