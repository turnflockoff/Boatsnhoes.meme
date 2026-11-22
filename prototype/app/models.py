from __future__ import annotations

from pydantic import BaseModel, Field


class WorkerRegistration(BaseModel):
    worker_id: str = Field(min_length=3, max_length=80)
    capabilities: list[str] = Field(default_factory=lambda: ["demo:sha256"])
    # One-time code from the operator. Needed for a new worker; an existing worker
    # updates its capabilities by sending its key instead.
    enrollment_code: str | None = Field(default=None, min_length=16, max_length=200)


class EnrollmentCreate(BaseModel):
    """Operator-only. `owner` names the person or group who runs the rig. Workers with the
    same owner never audit or break ties on each other's results."""
    owner: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    ttl_seconds: int = Field(default=86400, ge=60, le=7 * 86400)


class JobCreate(BaseModel):
    task_type: str = Field(default="demo:sha256", pattern="^(demo:sha256|llm:generate)$")
    payload: str = Field(min_length=1, max_length=4096)
    reward_points: int = Field(default=1, ge=0, le=1000)
    ttl_seconds: int = Field(default=300, ge=10, le=3600)


class ChallengeCreate(JobCreate):
    """A hidden known-answer job. `expected_result` is stored but never sent to workers."""
    expected_result: str = Field(min_length=1, max_length=8192)


class JobEnvelope(BaseModel):
    job_id: str
    task_type: str
    payload: str
    payload_hash: str
    reward_points: int
    expires_at: int
    signature: str


class ResultSubmission(BaseModel):
    # Optional: the worker is identified by its key. If sent, it must match the key.
    worker_id: str | None = Field(default=None, min_length=3, max_length=80)
    result: str = Field(min_length=1, max_length=8192)
    result_hash: str = Field(pattern="^[0-9a-f]{64}$")
    started_at: int
    completed_at: int
