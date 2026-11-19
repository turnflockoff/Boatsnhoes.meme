from __future__ import annotations

import argparse
import hashlib
import time

import httpx


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local deterministic demo worker")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--worker-id", default="demo-worker-01")
    parser.add_argument("--once", action="store_true", help="claim at most one job")
    args = parser.parse_args()

    with httpx.Client(base_url=args.url, timeout=10) as client:
        client.post("/v1/workers/register", json={
            "worker_id": args.worker_id, "capabilities": ["demo:sha256"]
        }).raise_for_status()
        while True:
            response = client.post("/v1/jobs/next", params={"worker_id": args.worker_id})
            if response.status_code == 404:
                print("No queued jobs.")
                return
            response.raise_for_status()
            job = response.json()
            started = int(time.time())
            result = digest(job["payload"])
            completed = int(time.time())
            submission = {
                "worker_id": args.worker_id,
                "result": result,
                "result_hash": digest(result),
                "started_at": started,
                "completed_at": completed
            }
            accepted = client.post(f"/v1/jobs/{job['job_id']}/submit", json=submission)
            accepted.raise_for_status()
            print(accepted.json())
            if args.once:
                return


if __name__ == "__main__":
    main()
