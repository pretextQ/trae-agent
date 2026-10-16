"""Direct user task entry point; passive Eyes observation is optional."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from trae_agent.config import Config
from trae_agent.loop import AgentLoop
from trae_agent.model_client import BudgetLedger, ModelClient
from trae_agent.runtime import RunRuntime
from trae_agent.tools import TOOL_SCHEMAS
from trae_agent.workspace import Workspace, sha256_hex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument(
        "--files-json", type=Path, help="Optional real initial filename/text mapping"
    )
    parser.add_argument("--session", default="default", help="Stable user session ID")
    parser.add_argument("--deadline-seconds", type=int, default=180)
    parser.add_argument(
        "--flush-only", action="store_true", help="Deliver retained telemetry; no task/model"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = Config.load()
    exporter = observation = None
    try:
        from trae_agent.observation import Exporter, RunObservation

        if os.environ.get("EYES_OBSERVATION_URL"):
            exporter = Exporter(
                Path(
                    os.environ.get("EYES_EXPORTER_STATE_DIR", str(config.state_dir / "observation"))
                ),
                os.environ["EYES_OBSERVATION_URL"],
                os.environ.get("EYES_OBSERVATION_TOKEN", ""),
            )
    except Exception as error:
        logging.warning("observation_disabled type=%s", type(error).__name__)
    if args.flush_only:
        if exporter:
            try:
                exporter.close(15)
            finally:
                print(json.dumps({"mode": "flush_only", "task_executed": False}))
            return
        raise SystemExit("Observation exporter not configured or unavailable")
    if not args.prompt_file:
        parser.error("--prompt-file required for a direct task")
    prompt = args.prompt_file.read_text(encoding="utf-8")
    initial = json.loads(args.files_json.read_text(encoding="utf-8")) if args.files_json else {}
    if not prompt.strip() or not isinstance(initial, dict):
        parser.error("Nonempty prompt and filename/text object required")
    import fcntl

    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    task_lock = (config.state_dir / "direct.lock").open("a")
    fcntl.flock(task_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    run_id = str(uuid4())
    workspace = Workspace.create(config.state_dir, run_id)
    runtime = RunRuntime(config.tool_timeout_seconds)
    if exporter:
        observation = RunObservation(
            exporter,
            run_id,
            args.session,
            config.model_id,
            config.capture_model_io,
            (config.api_key or "", exporter.token, config.http_token or ""),
        )
        exporter.active_run = run_id
        observation.emit("run_start", {"prompt": prompt} if config.capture_model_io else {})
        observation.emit(
            "input",
            {"prompt": prompt, "files": initial}
            if config.capture_model_io
            else {"initial_files": list(initial)},
        )

    def emit(kind, data):
        if observation:
            try:
                observation.boundary(kind, data)
            except Exception as error:
                observation.dropped += 1
                logging.warning("observation_boundary_failure type=%s", type(error).__name__)

    try:
        files = workspace.materialize_input(initial)
        ledger = BudgetLedger(config)
        loop = AgentLoop(
            config,
            ModelClient(
                config,
                ledger,
                TOOL_SCHEMAS,
                identity={
                    "run_id": run_id,
                    "attempt_id": None,
                    "idempotency_key_sha256": None,
                },
            ),
            workspace,
            runtime,
            emit,
        )
        outcome = loop.run(
            prompt, initial, files, datetime.now(UTC) + timedelta(seconds=args.deadline_seconds)
        )
        runtime.kill_all()
        # Keep actual direct-task files in this private workspace, no protocol or scorer needed.
        artifacts = [
            {"name": name, "size": len(data), "sha256": sha256_hex(data)}
            for name, data in workspace.collect_artifacts()
        ]
        result = {
            "run_id": run_id,
            "session_id": args.session,
            "status": outcome.status,
            "summary": outcome.summary,
            "error_code": outcome.error_code,
            "files": artifacts,
            "workspace": str(workspace.root),
            "usage": ledger.snapshot(),
        }
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        result_path = config.state_dir / ("result-" + run_id + ".json")
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(result_path, 0o600)
        if observation:
            limited = outcome.error_code and (
                "limit" in outcome.error_code or "budget" in outcome.error_code
            )
            observation.finish(
                "completed"
                if outcome.status == "succeeded"
                else "limited"
                if limited
                else "failed",
                error=outcome.error_code,
                model_calls=runtime.model_calls,
                tool_calls=runtime.tool_calls,
                files=artifacts,
            )
        print(json.dumps(result, ensure_ascii=False))
        if outcome.status != "succeeded":
            raise SystemExit(1)
    except BaseException as error:
        runtime.kill_all()
        if observation and not isinstance(error, SystemExit):
            observation.finish("failed", error=type(error).__name__)
        raise
    finally:
        runtime.kill_all()
        if exporter:
            exporter.active_run = None
            try:
                exporter.close()
            except Exception as error:
                logging.warning("observation_close_failure type=%s", type(error).__name__)


if __name__ == "__main__":
    main()
