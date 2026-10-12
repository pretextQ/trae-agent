"""Optional durable passive observation; no experiments, Runner or task execution."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from eyes.contracts.observation import ObservationBatch, ObservationEvent

logger = logging.getLogger(__name__)
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 4096
MAX_EVENT_BYTES = 48 * 1024


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {
            k: "[redacted]"
            if k.lower()
            in {
                "api_key",
                "authorization",
                "password",
                "token",
                "secret",
                "access_token",
                "refresh_token",
            }
            else redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    return value


class Exporter:
    """One process owns a bounded spool; failed telemetry never retries a task."""

    def __init__(self, directory: Path, url: str, token: str):
        import fcntl

        if not url.startswith(("http://", "https://")) or not token:
            raise ValueError("Observation URL and source token required")
        self.url = url.rstrip("/")
        self.token = token
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file_lock = (directory / "exporter.lock").open("a")
        fcntl.flock(self.file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(directory / "spool.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA max_page_count=2048")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS config (identity TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS batches "
            "(id INTEGER PRIMARY KEY, payload TEXT, rejected INTEGER DEFAULT 0)"
        )
        identity = hashlib.sha256((self.url + "\n" + token).encode()).hexdigest()
        prior = self.db.execute("SELECT identity FROM config").fetchone()
        if prior and prior[0] != identity:
            self.db.close()
            self.file_lock.close()
            raise ValueError("Observation spool belongs to another source/configuration")
        if not prior:
            self.db.execute("INSERT INTO config VALUES (?)", (identity,))
        self.db.commit()
        os.chmod(directory / "spool.sqlite3", 0o600)
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.active_run = None
        self.thread = threading.Thread(target=self._send, name="eyes-observation", daemon=True)
        self.thread.start()

    def enqueue(self, payload: dict, terminal: bool = False) -> bool:
        # Validate exactly the same explicit contract before durable publication.
        data = ObservationBatch.model_validate(payload).model_dump_json()
        with self.lock:
            rows, size = self.db.execute(
                "SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) FROM batches"
            ).fetchone()
            if rows >= MAX_ROWS - (0 if terminal else 1) or size + len(data.encode()) > (
                MAX_BYTES - (0 if terminal else MAX_EVENT_BYTES)
            ):
                return False
            self.db.execute("INSERT INTO batches(payload) VALUES (?)", (data,))
            self.db.commit()
        self.wakeup.set()
        return True

    def status(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT rejected,count(*) FROM batches GROUP BY rejected"
            ).fetchall()
        return {"pending": dict(rows).get(0, 0), "rejected": dict(rows).get(1, 0)}

    def _send(self):
        next_heartbeat = 0.0
        with httpx.Client(
            timeout=2, trust_env=False, headers={"Authorization": "Bearer " + self.token}
        ) as client:
            while not self.stop.is_set():
                try:
                    with self.lock:
                        row = self.db.execute(
                            "SELECT id,payload FROM batches WHERE rejected=0 ORDER BY id LIMIT 1"
                        ).fetchone()
                    if row:
                        response = client.post(self.url + "/events", json=json.loads(row[1]))
                        accepted = (
                            set(response.json().get("accepted", []))
                            if response.is_success
                            else set()
                        )
                        expected = {e["event_id"] for e in json.loads(row[1])["events"]}
                        with self.lock:
                            if response.is_success and expected <= accepted:
                                self.db.execute("DELETE FROM batches WHERE id=?", (row[0],))
                            elif 400 <= response.status_code < 500 and response.status_code not in (
                                408,
                                425,
                                429,
                            ):
                                self.db.execute(
                                    "UPDATE batches SET rejected=1 WHERE id=?", (row[0],)
                                )
                                logger.warning(
                                    "observation_rejected status=%s", response.status_code
                                )
                            else:
                                raise RuntimeError("observation_retry")
                            self.db.commit()
                        continue
                    if self.active_run and time.monotonic() >= next_heartbeat:
                        client.post(self.url + "/heartbeat", params={"run_id": self.active_run})
                        next_heartbeat = time.monotonic() + 5
                    self.wakeup.wait(0.5)
                except Exception as error:
                    logger.warning("observation_delivery_pending type=%s", type(error).__name__)
                    self.stop.wait(2)
                self.wakeup.clear()

    def close(self, seconds: float = 2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and self.status()["pending"]:
            self.wakeup.set()
            time.sleep(0.05)
        self.stop.set()
        self.wakeup.set()
        self.thread.join(timeout=3)
        # Avoid closing a connection under a still-running network request.
        if not self.thread.is_alive():
            self.db.close()
            self.file_lock.close()


class RunObservation:
    def __init__(self, exporter, run_id, session_id, model, capture_body, secrets):
        self.exporter = exporter
        self.metadata = {
            "schema_version": "1.0",
            "run_id": run_id,
            "session_id": session_id,
            "agent": "trae-agent",
            "model": model,
            "capture_body": capture_body,
        }
        self.secrets = secrets
        self.sequence = 0
        self.dropped = 0
        self.turn = 0
        self.trace = uuid4().hex
        self.span = None
        self.turn_open = False
        self.model_started = None
        self.tool_started = {}

    def emit(self, kind, data, *, terminal=False, tool_call_id=None):
        self.sequence += 1
        try:
            clean = redact(data, self.secrets)
            event = {
                "schema_version": "1.0",
                "event_id": str(uuid4()),
                "sequence": self.sequence,
                "occurred_at": datetime.now(UTC).isoformat(),
                "type": kind,
                "turn": self.turn or None,
                "tool_call_id": tool_call_id,
                "trace_id": self.trace,
                "span_id": self.span,
                "truncated": False,
                "data": clean,
            }
            if len(json.dumps(event, ensure_ascii=False).encode()) > MAX_EVENT_BYTES:
                # Keep identity, lifecycle and useful metadata; never silently truncate.
                event["data"] = {
                    k: v
                    for k, v in clean.items()
                    if k in {"status", "dropped_events", "name", "usage", "error", "duration_ms"}
                }
                event["data"]["body_omitted"] = "event byte limit"
                event["truncated"] = True
            ObservationEvent.model_validate(event)
            if not self.exporter.enqueue({**self.metadata, "events": [event]}, terminal):
                self.dropped += 1
                logger.warning("observation_buffer_full")
        except Exception as error:
            self.dropped += 1
            logger.warning("observation_capture_failure type=%s", type(error).__name__)

    def boundary(self, kind, data):
        """Receive live boundaries from the existing loop, never historical HTTP events."""
        if kind == "model.input":
            # This input belongs to the next model attempt; start follows separately.
            self.next_input = data
        elif kind == "model.request":
            if self.turn_open:
                self.emit("turn_end", {})
            self.turn += 1
            self.turn_open = True
            self.span = uuid4().hex[:16]
            self.emit("turn_start", {})
            self.model_started = time.monotonic()
            self.emit("model_start", {**data, "boundary": "ModelClient.complete attempt"})
            if hasattr(self, "next_input"):
                self.emit("model_input", {"request": self.next_input["messages"]})
                del self.next_input
        elif kind in ("model.response", "model.error"):
            elapsed = (time.monotonic() - self.model_started) * 1000 if self.model_started else None
            usage = (
                {
                    "input_tokens": data["prompt_tokens"],
                    "output_tokens": data["completion_tokens"],
                    "total_tokens": data["prompt_tokens"] + data["completion_tokens"],
                }
                if kind == "model.response"
                else None
            )
            self.emit(
                "model_end",
                {
                    **data,
                    "duration_ms": elapsed,
                    "usage": usage,
                    **({"status": "ERROR", "error": data["code"]} if kind == "model.error" else {}),
                },
            )
            if kind == "model.response" and self.metadata["capture_body"]:
                self.emit(
                    "message_end",
                    {"text": data.get("content"), "finish_reason": data["finish_reason"]},
                )
        elif kind == "tool.call":
            self.tool_started[data["tool_call_id"]] = time.monotonic()
            self.emit("tool_start", data, tool_call_id=data["tool_call_id"])
        elif kind == "tool.result":
            began = self.tool_started.pop(data["tool_call_id"], None)
            self.emit(
                "tool_end",
                {
                    **data,
                    **({"output": data["result"]} if "result" in data else {}),
                    "status": "success" if data["ok"] else "error",
                    "duration_ms": (time.monotonic() - began) * 1000 if began else None,
                },
                tool_call_id=data["tool_call_id"],
            )

    def finish(self, status, **data):
        if self.turn_open:
            self.emit("turn_end", {})
        self.emit(
            "run_end", {**data, "status": status, "dropped_events": self.dropped}, terminal=True
        )
