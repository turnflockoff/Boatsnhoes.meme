"""Test plumbing for per-worker keys.

Every worker now needs a one-time enrollment code from the operator and then sends its own
key on every request. Most tests are about jobs, audits and rewards, not about auth, so
`WorkerClient` remembers each worker's key and attaches it whenever a request names that
worker. The auth rules themselves are tested without this helper in test_worker_auth.py.
"""
from __future__ import annotations

import random
from typing import Any, Sequence

from fastapi.testclient import TestClient

from app import workers

OPERATOR = "operator-token-that-is-long-enough-456"
OP = {"X-Operator-Token": OPERATOR}


class FirstPickRng(random.Random):
    """Seeded rng whose auditor pick is always the alphabetically first eligible worker.
    Tests that aren't about auditor assignment use it so "who audits" is spelled out by
    worker names (wk-b before wk-c) instead of depending on a random seed. Audit sampling
    (`random()`) still comes from the seed. test_assignment.py tests the real random pick."""

    def choice(self, seq: Sequence[Any]) -> Any:
        return sorted(seq)[0]


class WorkerClient(TestClient):
    def __init__(self, app: Any, keys: dict[str, str] | None = None) -> None:
        super().__init__(app)
        self.worker_keys: dict[str, str] = {} if keys is None else keys

    def request(self, method: str, url: Any, **kwargs: Any):  # type: ignore[override]
        params = kwargs.get("params") or {}
        body = kwargs.get("json") if isinstance(kwargs.get("json"), dict) else {}
        worker_id = params.get("worker_id") if isinstance(params, dict) else None
        worker_id = worker_id or body.get("worker_id")
        headers = dict(kwargs.pop("headers", None) or {})
        if worker_id in self.worker_keys and "Authorization" not in headers \
                and "/v1/workers/register" not in str(url):
            headers["Authorization"] = f"Bearer {self.worker_keys[worker_id]}"
        return super().request(method, url, headers=headers, **kwargs)


def enrollment_code(client: TestClient, owner: str, ttl_seconds: int = 86400) -> str:
    r = client.post("/v1/enrollments", json={"owner": owner, "ttl_seconds": ttl_seconds},
                    headers=OP)
    assert r.status_code == 200, r.text
    return r.json()["enrollment_code"]


def enroll(client: WorkerClient, worker_id: str, owner: str | None = None,
           capabilities: list[str] | None = None) -> str:
    """Register a worker the real way (operator code, then register) and remember its key.
    Each worker gets its own owner unless one is given. If the app has no operator token,
    the code is made straight in the database, like `python -m app.workers enroll`."""
    owner = owner or f"owner-{worker_id}"
    if client.app.state.settings.operator_token:
        code = enrollment_code(client, owner)
    else:
        ledger = client.app.state.ledger
        code, _ = workers.issue_code(ledger, owner, 3600, client.app.state.now())
    body: dict[str, Any] = {"worker_id": worker_id, "enrollment_code": code}
    if capabilities is not None:
        body["capabilities"] = capabilities
    r = client.post("/v1/workers/register", json=body)
    assert r.status_code == 200, r.text
    key = r.json()["worker_key"]
    client.worker_keys[worker_id] = key
    return key
