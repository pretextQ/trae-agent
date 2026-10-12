"""Bind trae-agent to the Eyes external Agent protocol v1.

The durable protocol shell (persistence, idempotency, snapshots, events,
artifacts endpoints) is Eyes' reference service `eyes.agent_service.http.create_app`;
this module only supplies the real backend: input validation, the model/tool
loop, artifact capture, cleanup and honest result construction. It runs only on
POSIX (the shell uses process file locks) - deploy with deploy/compose.yaml.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trae_agent.config import Config
from trae_agent.loop import AgentLoop
from trae_agent.model_client import BudgetLedger, ModelClient
from trae_agent.runtime import RunRuntime
from trae_agent.tools import TOOL_SCHEMAS
from trae_agent.workspace import Workspace, sha256_hex

MAX_ERROR_MESSAGE = 500
MEDIA_TYPES = {".py": "text/x-python", ".txt": "text/plain", ".json": "application/json"}


class CodingInput(BaseModel):
    """Business input shared with the existing coding dataset; nothing Eyes-private."""

    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=100_000)
    files: dict[str, str] = Field(default_factory=dict, max_length=50)

    @field_validator("files")
    @classmethod
    def safe_relative_names(cls, value: dict[str, str]) -> dict[str, str]:
        from trae_agent.workspace import WorkspaceError, check_logical_name

        for name, content in value.items():
            try:
                check_logical_name(name)
            except WorkspaceError as error:
                raise ValueError(str(error)) from None
            if len(content.encode("utf-8")) > 1_048_576:
                raise ValueError(f"initial file too large: {name}")
        return value


def _redact(value, secrets: tuple[str, ...]):
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    return value


def app():
    """Uvicorn factory: build the protocol service from environment configuration."""
    config = Config.load()
    bearer_token = config.require_service_token()

    from eyes.agent_service.http import create_app
    from eyes.contracts.agent_protocol import AgentCapabilities

    import trae_agent

    agent_version = config.version or f"{trae_agent.VERSION}+unknown"

    ledger = BudgetLedger(config)

    class RunCapture:
        """Per-run event/artifact capture with failure accounting and redaction."""

        def __init__(self, shell, secrets: tuple[str, ...]) -> None:
            self.shell = shell
            self.secrets = secrets
            self.capture_failures = 0

        def event(self, kind: str, data: dict) -> None:
            try:
                self.shell.event("custom.trae_agent." + kind, _redact(data, self.secrets))
            except Exception:
                self.capture_failures += 1

        def artifact(self, name: str, data: bytes, media_type: str):
            try:
                return self.shell.artifact(name, data, media_type)
            except Exception:
                self.capture_failures += 1
                return None

    def execute(task, shell) -> dict:
        workspace = Workspace.create(config.state_dir, str(task.attempt_id))
        runtime = RunRuntime(config.tool_timeout_seconds)
        capture = RunCapture(shell, (config.api_key or "",))
        client = ModelClient(config, ledger, TOOL_SCHEMAS)
        loop = AgentLoop(config, client, workspace, runtime, capture.event)
        started = datetime.now(UTC)
        try:
            materialized = workspace.materialize_input(task.input.get("files") or {})
            outcome = loop.run(
                task.input["prompt"],
                task.input.get("files") or {},
                materialized,
                task.deadline,
            )
        finally:
            # End local tools, but a lost model response does not prove that the
            # vendor operation stopped. Its durable reservation is checked below.
            runtime.kill_all()

        budget_snapshot = ledger.snapshot()
        model_stop_confirmed = not (
            budget_snapshot["reserved_rmb"] or budget_snapshot["unconfirmed_usage_calls"]
        )
        if not model_stop_confirmed:
            capture.event(
                "task.stop_unconfirmed",
                {"reason": "model request or spend remains unconfirmed"},
            )

        artifacts = []
        manifest = []
        for name, data in workspace.collect_artifacts():
            manifest.append({"name": name, "size_bytes": len(data), "sha256": sha256_hex(data)})
            media_type = MEDIA_TYPES.get(Path(name).suffix, "text/plain; charset=utf-8")
            published = capture.artifact(name, data, media_type)
            if published is not None:
                artifacts.append(published)
            capture.event(
                "artifact.published",
                {
                    "name": name,
                    "size_bytes": len(data),
                    "sha256": sha256_hex(data),
                    "published": published is not None,
                },
            )
        # Preserve the original workspace for investigation of unknown work.
        cleanup_ok = workspace.cleanup() if model_stop_confirmed else None

        cost = {
            "model_calls": runtime.model_calls,
            "tool_calls": runtime.tool_calls,
            "prompt_tokens": runtime.model_prompt_tokens,
            "completion_tokens": runtime.model_completion_tokens,
            "cost_rmb": round(runtime.cost_rmb, 6),
            "budget_total_rmb": ledger.snapshot()["spent_rmb"],
        }
        output = {
            "summary": outcome.summary,
            "files": manifest,
            "usage": cost,
        }
        status = outcome.status if model_stop_confirmed else "unknown"
        error = None
        if status != "succeeded":
            error = {
                "code": outcome.error_code or "model_stop_unconfirmed",
                "message": (outcome.error_message or "Model operation stop is unconfirmed")[
                    :MAX_ERROR_MESSAGE
                ],
            }
        return {
            "protocol_version": "1.0",
            "run_id": str(shell.run_id),
            "status": status,
            "output": output,
            "error": error,
            "stopped_confirmed": model_stop_confirmed,
            "completed_at": datetime.now(UTC).isoformat() if model_stop_confirmed else None,
            "cleanup_status": "unknown"
            if cleanup_ok is None
            else "succeeded"
            if cleanup_ok
            else "failed",
            "evidence": {
                "status": "sealed"
                if capture.capture_failures == 0 and model_stop_confirmed
                else "partial",
                "dropped_events": 0,
                "details": {
                    "capture_failures": capture.capture_failures,
                    "eyes_baseline": config.eyes_baseline,
                    "agent_version": agent_version,
                    "capture_started_at": started.isoformat(),
                    "usage": cost,
                    "model_stop_confirmed": model_stop_confirmed,
                    "budget_reservation_rmb": budget_snapshot["reserved_rmb"],
                },
            },
            "artifacts": [
                {
                    "artifact_id": str(artifact.artifact_id),
                    "name": artifact.name,
                    "media_type": artifact.media_type,
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                }
                for artifact in artifacts
            ],
        }

    capabilities = AgentCapabilities(
        protocol_version="1.0",
        agent_id="trae-agent",
        agent_version=agent_version,
        input_schema=CodingInput.model_json_schema(),
        max_concurrency=config.max_concurrency,
        # Session isolation: separate workspace, message history, runtime and
        # subprocess set per task. Environment isolation is NOT claimed: tool
        # subprocesses share this container's kernel with the service (documented
        # trusted-code assumption, no per-task OS sandbox yet).
        session_isolation=True,
        environment_isolation=False,
        cancellation=False,
        resources=False,
        events=True,
        artifacts=True,
        terminal_retention_seconds=86400,
    )

    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return create_app(
        database=config.state_dir / "protocol.sqlite3",
        bearer_token=bearer_token,
        capabilities=capabilities,
        execute=execute,
        validate_input=CodingInput.model_validate,
    )
