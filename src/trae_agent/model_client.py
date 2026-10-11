"""MiMo OpenAI-compatible chat client: non-streaming, bounded retries, shared budget ledger.

Per DEVELOPMENT.md: first version is non-streaming; truncation, 429, network
errors and missing usage are recorded, never papered over. When the vendor
usage is missing, no further paid calls are made because spend cannot be
confirmed. The ledger is process-global and persisted, so concurrent runs and
service restarts share one budget account.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

import httpx

from trae_agent.config import Config

MAX_ERROR_BODY = 2000
TRANSPORT_RETRIES = 2


class ModelError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BudgetExhausted(ModelError):
    def __init__(self, spent: float, budget: float) -> None:
        super().__init__(
            "budget_exhausted",
            f"Run model budget reached: spent {spent:.4f} of {budget:.4f} RMB",
        )


@dataclass
class ModelResponse:
    message: dict
    finish_reason: str | None
    usage: dict | None
    tool_calls: list[dict]
    latency_ms: int
    cost_rmb: float = 0.0


@dataclass
class LedgerState:
    spent_rmb: float = 0.0
    calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    unconfirmed_usage_calls: int = 0
    extra: dict = field(default_factory=dict)


class BudgetLedger:
    """Thread-safe cumulative model spend across all runs of this service process."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.path = config.state_dir / "budget_ledger.json"
        self._lock = threading.Lock()
        self.state = LedgerState()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.state = LedgerState(
                spent_rmb=float(data.get("spent_rmb", 0.0)),
                calls=int(data.get("calls", 0)),
                prompt_tokens=int(data.get("prompt_tokens", 0)),
                cached_tokens=int(data.get("cached_tokens", 0)),
                completion_tokens=int(data.get("completion_tokens", 0)),
                unconfirmed_usage_calls=int(data.get("unconfirmed_usage_calls", 0)),
                extra=dict(data.get("extra", {})),
            )
        except (OSError, ValueError):
            self.state = LedgerState()

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "spent_rmb": self.state.spent_rmb,
                    "calls": self.state.calls,
                    "prompt_tokens": self.state.prompt_tokens,
                    "cached_tokens": self.state.cached_tokens,
                    "completion_tokens": self.state.completion_tokens,
                    "unconfirmed_usage_calls": self.state.unconfirmed_usage_calls,
                    "extra": self.state.extra,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def ensure_can_spend(self) -> None:
        with self._lock:
            if self.config.run_budget_rmb is None:
                raise ModelError("budget_unconfigured", "AGENT_RUN_BUDGET_RMB is not set")
            if self.state.spent_rmb >= self.config.run_budget_rmb:
                raise BudgetExhausted(self.state.spent_rmb, self.config.run_budget_rmb)

    def record(self, usage: dict | None) -> float:
        """Add one call's usage; returns the incremental cost in RMB."""
        with self._lock:
            self.state.calls += 1
            if not isinstance(usage, dict):
                # Spend cannot be confirmed; policy is to stop further paid calls.
                self.state.unconfirmed_usage_calls += 1
                self._persist_locked()
                return 0.0
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            details = usage.get("prompt_tokens_details") or {}
            cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
            uncached = max(0, prompt - cached)
            cost = (
                uncached / 1_000_000 * self.config.price_input_per_m
                + cached / 1_000_000 * self.config.price_cache_read_per_m
                + completion / 1_000_000 * self.config.price_output_per_m
            )
            self.state.spent_rmb += cost
            self.state.prompt_tokens += prompt
            self.state.cached_tokens += cached
            self.state.completion_tokens += completion
            self._persist_locked()
            return cost

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "spent_rmb": round(self.state.spent_rmb, 6),
                "calls": self.state.calls,
                "prompt_tokens": self.state.prompt_tokens,
                "cached_tokens": self.state.cached_tokens,
                "completion_tokens": self.state.completion_tokens,
                "unconfirmed_usage_calls": self.state.unconfirmed_usage_calls,
            }


class ModelClient:
    def __init__(self, config: Config, ledger: BudgetLedger, tool_schemas: list[dict]) -> None:
        self.config = config
        self.ledger = ledger
        self.tool_schemas = tool_schemas

    def complete(self, messages: list[dict], timeout_seconds: float) -> ModelResponse:
        api_key = self.config.require_model_access()
        self.ledger.ensure_can_spend()
        payload = {
            "model": self.config.model_id,
            "messages": messages,
            "tools": self.tool_schemas,
            "max_completion_tokens": self.config.max_output_tokens,
            "thinking": {"type": self.config.thinking},
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        started = time.monotonic()
        data = self._post(payload, headers, timeout_seconds)
        latency_ms = int((time.monotonic() - started) * 1000)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError("model_bad_response", "chat completion returned no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        cost = self.ledger.record(usage)
        if usage is None:
            raise ModelError(
                "usage_missing",
                "Vendor response had no usage object; spend cannot be confirmed, "
                "stopping paid calls per budget policy",
            )
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            tool_calls = []
        return ModelResponse(
            message=message,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=[tc for tc in tool_calls if isinstance(tc, dict)],
            latency_ms=latency_ms,
            cost_rmb=cost,
        )

    def _post(self, payload: dict, headers: dict, timeout_seconds: float) -> dict:
        url = f"{self.config.base_url}/chat/completions"
        delays = [1.0, 2.0]
        last_error: Exception | None = None
        for attempt in range(TRANSPORT_RETRIES + 1):
            try:
                with httpx.Client(timeout=timeout_seconds) as client:
                    response = client.post(url, json=payload, headers=headers)
                break
            except httpx.TransportError as error:
                # No bytes consumed on the vendor side for a failed transport; retrying
                # does not double-charge. Anything after a response is never retried.
                last_error = error
                if attempt < TRANSPORT_RETRIES:
                    time.sleep(delays[min(attempt, len(delays) - 1)])
        else:
            raise ModelError("model_network_error", str(last_error))
        if response.status_code == 429:
            raise ModelError(
                "model_rate_limited", "Model API returned 429; not retrying paid calls"
            )
        if response.status_code in {401, 403}:
            raise ModelError(
                "model_auth_error", f"Model API rejected credentials ({response.status_code})"
            )
        if response.status_code >= 400:
            body = response.text[:MAX_ERROR_BODY]
            raise ModelError(
                "model_http_error", f"Model API HTTP {response.status_code}: {body}"
            )
        try:
            return response.json()
        except ValueError as error:
            raise ModelError(
                "model_bad_json", f"Model API returned non-JSON body: {error}"
            ) from None
