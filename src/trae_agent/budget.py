"""Versioned per-request reservations and audit in one locked atomic private file."""

import hashlib
import json
import math
import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from trae_agent.model_errors import BudgetExhausted, ModelError

MAX_REQUEST_BYTES = 128_000


def timestamp():
    return datetime.now(UTC).isoformat()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def usage_cost(usage, prices):
    if usage is None:
        raise ModelError("usage_missing", "Vendor response has no usage; spend is unconfirmed")
    if not isinstance(usage, dict) or any(
        type(usage.get(k)) is not int or usage[k] < 0
        for k in ("prompt_tokens", "completion_tokens")
    ):
        raise ModelError("usage_invalid", "Vendor token usage is missing or invalid")
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens", 0) if isinstance(details, dict) else None
    if type(cached) is not int or not 0 <= cached <= usage["prompt_tokens"]:
        raise ModelError("usage_invalid", "Vendor cached token usage is invalid")
    prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
    cost = (
        Decimal(prompt - cached) * Decimal(str(prices["input"]))
        + Decimal(cached) * Decimal(str(prices["cache"]))
        + Decimal(completion) * Decimal(str(prices["output"]))
    ) / 1_000_000
    return cost, prompt, cached, completion


class BudgetLedger:
    def __init__(self, config):
        self.config, self.path = config, config.state_dir / "budget_ledger.json"
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("format_version", 1) not in {1, 2}:
                raise ValueError("unsupported version")
            value = self.data["spent_rmb"]
            if type(value) not in {float, int} or not math.isfinite(value) or value < 0:
                raise ValueError("invalid money")
            for name in (
                "calls",
                "prompt_tokens",
                "cached_tokens",
                "completion_tokens",
                "unconfirmed_usage_calls",
            ):
                if type(self.data[name]) is not int or self.data[name] < 0:
                    raise ValueError("invalid counter")
            extra = self.data["extra"]
            if not isinstance(extra, dict):
                raise ValueError("invalid ledger extras")
            pending = extra.get("pending_rmb", 0)
            if type(pending) not in {float, int} or not math.isfinite(pending) or pending < 0:
                raise ValueError("invalid pending")
            if not isinstance(extra.get("requests", {}), dict) or not isinstance(
                extra.get("audit", {}), dict
            ):
                raise ValueError("invalid request/audit")
            requests = extra.get("requests", {})
            for record in requests.values():
                if (
                    not isinstance(record, dict)
                    or not isinstance(record.get("identity"), dict)
                    or type(record.get("stopped_confirmed")) is not bool
                    or record.get("billing")
                    not in {"unconfirmed", "usage", "charged", "not_charged"}
                ):
                    raise ValueError("invalid request record")
            call_id = extra.get("pending_call_id")
            if call_id and (
                call_id not in requests or requests[call_id]["reserved_rmb"] != pending
            ):
                raise ValueError("reservation identity mismatch")
        except FileNotFoundError:
            self.data = {
                "format_version": 2,
                "spent_rmb": 0.0,
                "calls": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "completion_tokens": 0,
                "unconfirmed_usage_calls": 0,
                "extra": {},
            }
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise ModelError(
                "ledger_unreadable", "Refusing calls with unreadable ledger"
            ) from error

    @contextmanager
    def transaction(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock, self.path.with_suffix(".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._load()
            yield

    def persist(self):
        self.data["format_version"] = 2
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            os.chmod(tmp, 0o600)
            json.dump(self.data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @property
    def extra(self):
        return self.data["extra"]

    def reserve(self, payload, identity=None, api_key=None):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ModelError("request_size_limit", "Model request exceeds text-only byte bound")
        prices = {
            "input": self.config.price_input_per_m,
            "cache": self.config.price_cache_read_per_m,
            "output": self.config.price_output_per_m,
        }
        if any(not math.isfinite(v) or v < 0 for v in prices.values()):
            raise ModelError("price_invalid", "Prices must be finite and nonnegative")
        allowance = (
            (4 * len(encoded) + 4096) * prices["input"]
            + self.config.max_output_tokens * prices["output"]
        ) / 1_000_000
        with self.transaction():
            budget = self.config.run_budget_rmb
            if budget is None or not math.isfinite(budget) or budget <= 0:
                raise ModelError("budget_unconfigured", "A finite positive budget is required")
            if (
                self.data["unconfirmed_usage_calls"]
                or self.extra.get("pending_rmb")
                or any(not r["stopped_confirmed"] for r in self.extra.get("requests", {}).values())
            ):
                raise ModelError("spend_unconfirmed", "Previous request or spend is unconfirmed")
            if self.data["spent_rmb"] + allowance > budget:
                raise BudgetExhausted(self.data["spent_rmb"] + allowance, budget)
            call_id = str(uuid4())
            self.extra.setdefault("requests", {})[call_id] = {
                "identity": identity or {},
                "payload_sha256": fingerprint(payload),
                "key_sha256": hashlib.sha256(api_key.encode()).hexdigest() if api_key else None,
                "model": self.config.model_id,
                "prices": prices,
                "reserved_rmb": allowance,
                "started_at": timestamp(),
                "stopped_confirmed": False,
                "billing": "unconfirmed",
                "usage_recorded": False,
                "diagnostics": {},
            }
            self.extra.update(pending_rmb=allowance, pending_call_id=call_id)
            self.persist()
            return call_id

    def request(self, call_id):
        request = self.extra.get("requests", {}).get(call_id)
        if request is None:
            raise ModelError("request_identity_conflict", "Request identity is unavailable")
        return request

    def observe(self, call_id, diagnostics, *, stopped=False):
        with self.transaction():
            request = self.request(call_id)
            request["diagnostics"].update(diagnostics)
            request["observed_at"] = timestamp()
            request["stopped_confirmed"] = request["stopped_confirmed"] or stopped
            self.persist()

    def record(self, usage, call_id=None):
        with self.transaction():
            call_id = call_id or self.extra.get("pending_call_id")
            request = self.request(call_id)
            if request["usage_recorded"] or self.extra.get("pending_call_id") != call_id:
                raise ModelError("request_identity_conflict", "Request usage already recorded")
            request["usage_recorded"] = True
            self.data["calls"] += 1
            try:
                cost, prompt, cached, completion = usage_cost(usage, request["prices"])
            except ModelError:
                request["unconfirmed_counter"] = True
                self.data["unconfirmed_usage_calls"] += 1
                self.persist()
                raise
            request.update(
                billing="usage",
                usage={
                    "prompt_tokens": prompt,
                    "cached_tokens": cached,
                    "completion_tokens": completion,
                },
                cost_rmb=str(cost),
            )
            if cost > Decimal(str(request["reserved_rmb"])):
                request["unconfirmed_counter"] = True
                self.data["unconfirmed_usage_calls"] += 1
            self.data["spent_rmb"] = float(Decimal(str(self.data["spent_rmb"])) + cost)
            for name, value in (
                ("prompt_tokens", prompt),
                ("cached_tokens", cached),
                ("completion_tokens", completion),
            ):
                self.data[name] += value
            self.extra.update(pending_rmb=0.0, pending_call_id=None)
            self.persist()
            return float(cost)

    def stop_confirmed(self, run_id):
        with self.transaction():
            if not self.extra.get("requests") and (
                self.extra.get("pending_rmb") or self.data["unconfirmed_usage_calls"]
            ):
                return False
            return all(
                r["stopped_confirmed"]
                for r in self.extra.get("requests", {}).values()
                if r["identity"].get("run_id") == run_id
            )

    def snapshot(self):
        with self.transaction():
            return {
                **{
                    name: self.data[name]
                    for name in (
                        "spent_rmb",
                        "calls",
                        "prompt_tokens",
                        "cached_tokens",
                        "completion_tokens",
                        "unconfirmed_usage_calls",
                    )
                },
                "reserved_rmb": self.extra.get("pending_rmb", 0.0),
                "format_version": self.data.get("format_version", 1),
            }
