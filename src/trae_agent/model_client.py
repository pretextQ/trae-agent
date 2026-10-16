"""Non-streaming chat client with request identities and bounded safe diagnostics."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import httpx

from trae_agent.budget import MAX_REQUEST_BYTES as MAX_REQUEST_BYTES
from trae_agent.budget import BudgetLedger as BudgetLedger
from trae_agent.config import Config
from trae_agent.model_errors import BudgetExhausted as BudgetExhausted
from trae_agent.model_errors import ModelError as ModelError


def safe_id(value, secret):
    secrets = (secret,) if isinstance(secret, str) else secret or ()
    if (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value)
        and all(not item or item not in value for item in secrets)
    ):
        return value
    return None


def diagnostics(response, secret):
    result = {"http_status": response.status_code}
    for header in ("x-request-id", "request-id"):
        value = safe_id(response.headers.get(header), secret)
        if value:
            result[header.replace("-", "_")] = value
    return result


@dataclass
class ModelResponse:
    message: dict
    finish_reason: str | None
    usage: dict | None
    tool_calls: list[dict]
    latency_ms: int
    cost_rmb: float = 0.0
    diagnostics: dict = field(default_factory=dict)


class ModelClient:
    def __init__(
        self, config: Config, ledger: BudgetLedger, tool_schemas: list[dict], *, identity=None
    ):
        self.config, self.ledger, self.tool_schemas = config, ledger, tool_schemas
        self.identity = identity or {}

    def complete(
        self, messages: list[dict], timeout_seconds: float, *, call_index=None
    ) -> ModelResponse:
        api_key = self.config.require_model_access()
        payload = {
            "model": self.config.model_id,
            "messages": messages,
            "tools": self.tool_schemas,
            "max_completion_tokens": self.config.max_output_tokens,
            "thinking": {"type": self.config.thinking},
            "stream": False,
        }
        call_id = self.ledger.reserve(payload, {**self.identity, "call_index": call_index}, api_key)
        started, details = time.monotonic(), {"call_id": call_id}
        try:
            data, observed = self._post(
                payload, {"Authorization": f"Bearer {api_key}"}, timeout_seconds
            )
            details.update(observed)
            self.ledger.observe(call_id, details)
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ModelError("model_bad_response", "Chat completion returned no choices")
            choice = choices[0]
            message, finish_reason = choice.get("message") or {}, choice.get("finish_reason")
            if (
                not isinstance(message, dict)
                or not isinstance(finish_reason, str)
                or finish_reason
                not in {
                    "stop",
                    "length",
                    "tool_calls",
                    "content_filter",
                    "repetition_truncation",
                }
            ):
                raise ModelError(
                    "model_bad_response", "Chat completion has no recognized completion"
                )
            self.ledger.observe(call_id, details, stopped=True)
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            cost = self.ledger.record(usage, call_id)
        except ModelError as error:
            details.update(error.diagnostics)
            self.ledger.observe(call_id, details)
            raise ModelError(error.code, error.message, details) from None
        tool_calls = message.get("tool_calls") or []
        return ModelResponse(
            message=message,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=[tc for tc in tool_calls if isinstance(tc, dict)]
            if isinstance(tool_calls, list)
            else [],
            latency_ms=int((time.monotonic() - started) * 1000),
            cost_rmb=cost,
            diagnostics=details,
        )

    def _post(self, payload: dict, headers: dict, timeout_seconds: float):
        try:
            with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
                response = client.post(
                    f"{self.config.base_url}/chat/completions", json=payload, headers=headers
                )
        except httpx.TransportError as error:
            raise ModelError("model_network_error", type(error).__name__) from None
        secrets = (self.config.api_key, self.config.http_token)
        details = diagnostics(response, secrets)
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            response_id = safe_id(data.get("id"), secrets)
            if response_id:
                details["response_id"] = response_id
            error = data.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                code = str(code) if type(code) is int else code
                if safe_id(code, secrets):
                    details["provider_error_code"] = code
        if response.status_code >= 400:
            code = (
                "model_rate_limited"
                if response.status_code == 429
                else "model_auth_error"
                if response.status_code in {401, 403}
                else "model_http_error"
            )
            raise ModelError(
                code, f"Model API HTTP {response.status_code}; outcome unconfirmed", details
            )
        if not isinstance(data, dict):
            raise ModelError("model_bad_json", "Model API returned no JSON object", details)
        return data, details
