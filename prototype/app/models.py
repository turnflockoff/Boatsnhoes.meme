from __future__ import annotations

from pydantic import BaseModel, Field


class WorkerRegistration(BaseModel):
    worker_id: str = Field(min_length=3, max_length=80)
    capabilities: list[str] = Field(default_factory=lambda: ["demo:sha256"])


class JobCreate(BaseModel):
    task_type: str = Field(default="demo:sha256", pattern="^demo:sha256$")
    payload: str = Field(min_length=1, max_length=4096)
    reward_points: int = Field(default=1, ge=0, le=1000)
    ttl_seconds: int = Field(default=300, ge=10, le=3600)


class JobEnvelope(BaseModel):
    job_id: str
    task_type: str
    payload: str
    payload_hash: str
    reward_points: int
    expires_at: int
    signature: str


class ResultSubmission(BaseModel):
    worker_id: str = Field(min_length=3, max_length=80)
    result: str = Field(min_length=1, max_length=8192)
    result_hash: str = Field(pattern="^[0-9a-f]{64}$")
    started_at: int
    completed_at: int
