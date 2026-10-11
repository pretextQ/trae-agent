"""Environment-driven configuration. Secrets stay in the process; never in events or logs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MODEL_ID_DEFAULT = "mimo-v2.6-flash"
BASE_URL_DEFAULT = "https://api.xiaomimimo.com/v1"


def _env_float(env: dict, name: str, default: float) -> float:
    raw = env.get(name)
    return default if raw is None or raw == "" else float(raw)


def _env_int(env: dict, name: str, default: int) -> int:
    raw = env.get(name)
    return default if raw is None or raw == "" else int(raw)


def _env_bool(env: dict, name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Runtime limits, model access and pricing. All values come from the environment."""

    model_id: str
    base_url: str
    api_key: str | None
    http_token: str | None
    state_dir: Path
    version: str | None
    eyes_baseline: str | None
    max_concurrency: int
    max_model_calls: int
    max_tool_calls: int
    max_output_tokens: int
    run_budget_rmb: float | None
    thinking: str
    capture_model_io: bool
    request_timeout_seconds: float
    tool_timeout_seconds: float
    price_input_per_m: float
    price_cache_read_per_m: float
    price_output_per_m: float

    @classmethod
    def load(cls, env: dict | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        state_dir = Path(env.get("AGENT_STATE_DIR") or "state").resolve()
        thinking = (env.get("AGENT_THINKING") or "enabled").strip().lower()
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("AGENT_THINKING must be 'enabled' or 'disabled'")
        budget = env.get("AGENT_RUN_BUDGET_RMB")
        concurrency = _env_int(env, "AGENT_MAX_CONCURRENCY", 1)
        if concurrency != 1:
            # Session/environment isolation for parallel tasks is not verified yet.
            raise ValueError("AGENT_MAX_CONCURRENCY must stay 1 until isolation is verified")
        return cls(
            model_id=env.get("AGENT_MODEL_ID") or MODEL_ID_DEFAULT,
            base_url=(env.get("AGENT_MODEL_BASE_URL") or BASE_URL_DEFAULT).rstrip("/"),
            api_key=env.get("AGENT_MODEL_API_KEY") or None,
            http_token=env.get("AGENT_HTTP_TOKEN") or None,
            state_dir=state_dir,
            version=env.get("AGENT_VERSION") or None,
            eyes_baseline=env.get("AGENT_EYES_BASELINE") or None,
            max_concurrency=concurrency,
            max_model_calls=_env_int(env, "AGENT_MAX_MODEL_CALLS", 24),
            max_tool_calls=_env_int(env, "AGENT_MAX_TOOL_CALLS", 48),
            max_output_tokens=_env_int(env, "AGENT_MAX_OUTPUT_TOKENS", 2048),
            run_budget_rmb=float(budget) if budget not in (None, "") else None,
            thinking=thinking,
            capture_model_io=_env_bool(env, "AGENT_CAPTURE_MODEL_IO", False),
            request_timeout_seconds=_env_float(env, "AGENT_REQUEST_TIMEOUT_SECONDS", 120.0),
            tool_timeout_seconds=_env_float(env, "AGENT_TOOL_TIMEOUT_SECONDS", 20.0),
            # Pay-as-you-go list prices for mimo-v2.6-flash, 2026-10-11, RMB per million tokens.
            price_input_per_m=_env_float(env, "AGENT_PRICE_INPUT_PER_M", 1.0),
            price_cache_read_per_m=_env_float(env, "AGENT_PRICE_CACHE_READ_PER_M", 0.02),
            price_output_per_m=_env_float(env, "AGENT_PRICE_OUTPUT_PER_M", 2.0),
        )

    def require_model_access(self) -> str:
        """Paid model calls need a key and an explicit budget; refuse to run otherwise."""
        if not self.api_key:
            raise ValueError("AGENT_MODEL_API_KEY is not configured")
        if self.run_budget_rmb is None or self.run_budget_rmb <= 0:
            raise ValueError("AGENT_RUN_BUDGET_RMB must be a positive number before paid runs")
        return self.api_key

    def require_service_token(self) -> str:
        if not self.http_token or len(self.http_token) < 32:
            raise ValueError("AGENT_HTTP_TOKEN must be an independent token of at least 32 chars")
        return self.http_token
