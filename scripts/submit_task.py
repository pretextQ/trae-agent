"""Operator client for the trae-agent protocol service.

Submits a real dataset case (or a replay of an existing idempotency key), polls
to a terminal state, downloads artifacts with SHA-256 verification and writes a
run report next to the artifacts. This is operations tooling, not a test.

Usage from the repository root:
  python scripts/submit_task.py --dataset D:\\projects\\eyes\\integrations\\deta\\coding-smoke.jsonl --case fix-mean
  python scripts/submit_task.py --dataset ... --case fix-mean --replay-key <key>   # replay, no new run
"""  # noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
POLL_SECONDS = 2.0


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def traceparent() -> str:
    return f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"


def load_case(dataset: Path, case_id: str) -> dict:
    for line in dataset.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        if case.get("case_id") == case_id:
            return case
    raise SystemExit(f"case {case_id!r} not found in {dataset}")


def submit(client: httpx.Client, base_url: str, task: dict) -> dict:
    response = client.post(f"{base_url}/agent/v1/runs", json=task)
    if response.status_code not in (200, 202):
        raise SystemExit(f"submit failed: HTTP {response.status_code} {response.text[:500]}")
    return response.json()


def poll_terminal(client: httpx.Client, base_url: str, run_id: str, deadline_epoch: float):
    while time.time() < deadline_epoch:
        response = client.get(f"{base_url}/agent/v1/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
        if run["result"] is not None:
            return run
        time.sleep(POLL_SECONDS)
    raise SystemExit("polling exceeded local wall clock; inspect the service manually")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:19070")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "state" / "downloads")
    parser.add_argument("--deadline-seconds", type=int, default=600)
    parser.add_argument("--replay-key", default=None, help="Reuse this idempotency key")
    parser.add_argument("--task-id", default=None, help="Reuse this task UUID (new attempt)")
    args = parser.parse_args()

    env = load_env(REPO_ROOT / ".env")
    token = env.get("AGENT_HTTP_TOKEN")
    if not token:
        raise SystemExit("AGENT_HTTP_TOKEN missing from .env")

    case = load_case(args.dataset, args.case)
    attempt_id = str(uuid.uuid4())
    idempotency_key = args.replay_key or f"case-{args.case}-{attempt_id[:8]}"
    task = {
        "protocol_version": "1.0",
        "task_id": args.task_id or str(uuid.uuid4()),
        "attempt_id": attempt_id,
        "idempotency_key": idempotency_key,
        "deadline": deadline_iso(args.deadline_seconds),
        "input": {"prompt": case["input"]["prompt"], "files": case["input"].get("files", {})},
        "resources": [],
        "traceparent": traceparent(),
    }

    out_dir = args.out_dir / idempotency_key
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(headers=headers, timeout=30) as client:
        run = submit(client, args.base_url, task)
        replayed = None
        if run["status"] in ("accepted", "queued"):
            print(f"accepted run_id={run['run_id']} status={run['status']}")
        else:
            replayed = run
            print(f"replayed run_id={run['run_id']} status={run['status']}")
        if replayed is None or replayed["result"] is None:
            final_run = poll_terminal(client, args.base_url, run["run_id"], time.time() + 900)
        else:
            final_run = replayed
        result = final_run["result"]
        result_response = client.get(f"{args.base_url}/agent/v1/runs/{run['run_id']}/result")
        if result_response.status_code == 200:
            result = result_response.json()

        print(f"status={result['status']} cleanup={result['cleanup_status']}")
        if result.get("error"):
            print(f"error: {result['error']}")

        artifacts = []
        for artifact in result.get("artifacts", []):
            download = client.get(
                f"{args.base_url}/agent/v1/runs/{run['run_id']}/artifacts/{artifact['artifact_id']}"
            )
            download.raise_for_status()
            data = download.content
            actual_sha = hashlib.sha256(data).hexdigest()
            verified = (
                actual_sha == artifact["sha256"] and len(data) == artifact["size_bytes"]
            )
            if not verified:
                print(f"VERIFY FAILED: {artifact['name']} size/sha mismatch")
            target = out_dir / artifact["name"]
            target.write_bytes(data)
            artifacts.append(
                {
                    "name": artifact["name"],
                    "size_bytes": len(data),
                    "sha256": actual_sha,
                    "verified": verified,
                }
            )
            print(f"artifact {artifact['name']}: {len(data)} bytes sha256 ok={verified}")

        events = client.get(
            f"{args.base_url}/agent/v1/runs/{run['run_id']}/events?limit=200"
        )
        event_items = events.json()["items"] if events.status_code == 200 else []

    report = {
        "case_id": args.case,
        "idempotency_key": idempotency_key,
        "run_id": run["run_id"],
        "status": result["status"],
        "error": result.get("error"),
        "cleanup_status": result["cleanup_status"],
        "evidence": result.get("evidence"),
        "output": result.get("output"),
        "artifacts": artifacts,
        "event_count": len(event_items),
        "events": event_items,
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {report_path}")


def deadline_iso(seconds: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


if __name__ == "__main__":
    sys.exit(main())
