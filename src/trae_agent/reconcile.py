"""Offline operator evidence import. Never calls a model or mutates protocol state."""

import argparse
import copy
import hashlib
import json
import os
import sqlite3
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from trae_agent.budget import BudgetLedger, fingerprint, timestamp, usage_cost
from trae_agent.config import Config
from trae_agent.model_errors import ModelError


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    attempt_id: str | None = Field(pattern=r"^[0-9a-f-]{36}$")
    idempotency_key_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    call_index: int = Field(ge=0)


class Prices(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    input: Decimal = Field(ge=0)
    cache: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)


class CachedUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cached_tokens: int = Field(default=0, ge=0)


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    prompt_tokens_details: CachedUsage = Field(default_factory=CachedUsage)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    format_version: Literal[1]
    evidence_id: UUID
    operation: Literal["observe", "settle", "bind_legacy"]
    call_id: UUID
    identity: Identity
    source: Literal["supplier_record", "operator_review"]
    source_reference: UUID
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: AwareDatetime
    provider_request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,200}$")
    provider_request_id_unavailable_confirmed: bool = False
    stopped_confirmed: bool = False
    billing: Literal["unconfirmed", "charged", "not_charged"] = "unconfirmed"
    amount_rmb: Decimal | None = Field(default=None, ge=0)
    usage: TokenUsage | None = None
    prices: Prices | None = None
    ledger_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    protocol_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def facts_required(self):
        if self.operation == "settle":
            if (
                self.source != "supplier_record"
                or self.billing == "unconfirmed"
                or self.amount_rmb is None
            ):
                raise ValueError("settlement requires supplier billing evidence")
            if self.billing == "charged" and (self.usage is None or self.prices is None):
                raise ValueError("charged settlement requires usage and price evidence")
            if self.billing == "not_charged" and (self.amount_rmb != 0 or self.usage is not None):
                raise ValueError("zero-charge confirmation must be explicit")
        elif self.billing != "unconfirmed" or self.amount_rmb is not None or self.usage is not None:
            raise ValueError("only settlement imports billing facts")
        if self.stopped_confirmed and self.source != "supplier_record":
            raise ValueError("stop confirmation requires supplier evidence")
        if self.provider_request_id_unavailable_confirmed and (
            self.provider_request_id is not None or self.source != "supplier_record"
        ):
            raise ValueError("missing supplier ID requires an explicit supplier record")
        if self.operation == "bind_legacy" and (
            not self.ledger_sha256
            or not self.protocol_sha256
            or self.stopped_confirmed
            or self.prices is None
        ):
            raise ValueError(
                "legacy binding requires both original file digests and cannot assert stop"
            )
        return self


@contextmanager
def operator_access(config):
    import fcntl

    info = config.state_dir.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ModelError("permission_denied", "Use the state owner and a private state directory")
    with ExitStack() as stack:
        # Refuse live HTTP service and direct-task ownership, independently of remote stop.
        for name in ("protocol.lock", "direct.lock"):
            lock = stack.enter_context((config.state_dir / name).open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ModelError(
                    "service_active", "Stop local writers before evidence import"
                ) from None
        yield


def bind_legacy(ledger, proof):
    from eyes.contracts.agent_protocol import AgentRun, AgentTask

    if (
        ledger.extra.get("pending_call_id")
        or not ledger.extra.get("pending_rmb")
        or ledger.data["unconfirmed_usage_calls"]
        or ledger.extra.get("requests")
        or hashlib.sha256(ledger.path.read_bytes()).hexdigest() != proof.ledger_sha256
    ):
        raise ModelError("legacy_conflict", "Legacy reservation is ambiguous or has changed")
    protocol = ledger.config.state_dir / "protocol.sqlite3"
    if any(
        p.exists() and p.stat().st_size
        for p in (
            Path(str(protocol) + "-wal"),
            Path(str(protocol) + "-journal"),
        )
    ):
        raise ModelError("protocol_uncheckpointed", "Protocol must be a clean, stopped snapshot")
    if hashlib.sha256(protocol.read_bytes()).hexdigest() != proof.protocol_sha256:
        raise ModelError("identity_mismatch", "Protocol evidence has changed")
    with sqlite3.connect(protocol.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        rows = db.execute("SELECT key, task, snapshot FROM runs").fetchall()
        unresolved = []
        for key, task, snapshot in rows:
            task = AgentTask.model_validate_json(task).model_dump(mode="json")
            snapshot = AgentRun.model_validate_json(snapshot).model_dump(mode="json")
            if snapshot["status"] == "unknown" and snapshot["result"]["evidence"]["details"].get(
                "budget_reservation_rmb"
            ):
                unresolved.append((key, task, snapshot))
        if len(unresolved) != 1:
            raise ModelError(
                "legacy_conflict", "Exactly one attributable legacy reservation is required"
            )
        key, task, snapshot = unresolved[0]
        expected = proof.identity
        if (
            expected.run_id != snapshot["run_id"]
            or expected.attempt_id != snapshot["attempt_id"]
            or expected.attempt_id != task["attempt_id"]
            or expected.idempotency_key_sha256 != hashlib.sha256(key.encode()).hexdigest()
            or task["idempotency_key"] != key
            or task["task_id"] != snapshot["task_id"]
            or snapshot["result"]["evidence"]["details"]["budget_reservation_rmb"]
            != ledger.extra["pending_rmb"]
        ):
            raise ModelError("identity_mismatch", "Legacy identity differs from original protocol")
        events = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT event FROM events WHERE run=? ORDER BY sequence", (expected.run_id,)
            )
        ]
        requests = [e for e in events if e["type"].endswith(".model.request")]
        errors = [e for e in events if e["type"].endswith(".model.error")]
        responses = [e for e in events if e["type"].endswith(".model.response")]
        starts = [e for e in events if e["type"].endswith(".task.started")]
        if (
            not requests
            or not errors
            or requests[-1]["data"]["call_index"] != expected.call_index
            or errors[-1]["data"]["call_index"] != expected.call_index
            or any(e["data"]["call_index"] == expected.call_index for e in responses)
            or sum(e["data"]["call_index"] == expected.call_index for e in requests) != 1
            or len(starts) != 1
            or starts[0]["data"]["model_id"] != ledger.config.model_id
        ):
            raise ModelError(
                "identity_mismatch", "Unconfirmed original call is not uniquely identified"
            )
    ledger.extra.setdefault("requests", {})[str(proof.call_id)] = {
        "identity": proof.identity.model_dump(),
        "payload_sha256": None,
        "key_sha256": None,
        "model": ledger.config.model_id,
        "prices": proof.prices.model_dump(mode="json"),
        "reserved_rmb": ledger.extra["pending_rmb"],
        "started_at": requests[-1]["occurred_at"],
        "stopped_confirmed": False,
        "billing": "unconfirmed",
        "usage_recorded": False,
        "diagnostics": {},
        "legacy_binding": proof.model_dump(mode="json"),
    }
    ledger.extra["pending_call_id"] = str(proof.call_id)


def reconcile(config, proof):
    with operator_access(config):
        ledger = BudgetLedger(config)
        with ledger.transaction():
            key, call_id = str(proof.evidence_id), str(proof.call_id)
            payload = proof.model_dump(mode="json")
            digest = fingerprint(payload)
            previous = ledger.extra.get("audit", {}).get(key)
            if previous:
                if previous["digest"] != digest:
                    raise ModelError(
                        "evidence_conflict", "Evidence identifier has different content"
                    )
                return previous["receipt"]
            before = {
                k: ledger.data[k]
                for k in (
                    "spent_rmb",
                    "calls",
                    "unconfirmed_usage_calls",
                    "prompt_tokens",
                    "cached_tokens",
                    "completion_tokens",
                )
            }
            before["pending_rmb"] = ledger.extra.get("pending_rmb", 0)
            before["request"] = copy.deepcopy(ledger.extra.get("requests", {}).get(call_id))
            if proof.operation == "bind_legacy":
                bind_legacy(ledger, proof)
            request = ledger.request(call_id)
            if request["identity"] != proof.identity.model_dump():
                raise ModelError("identity_mismatch", "Evidence differs from the original request")
            ids = {
                request["diagnostics"][k]
                for k in ("x_request_id", "request_id", "response_id", "reconciled_request_id")
                if request["diagnostics"].get(k)
            }
            if ids and proof.provider_request_id not in ids:
                raise ModelError("identity_mismatch", "Supplier request identity differs")
            if (proof.stopped_confirmed or proof.operation == "settle") and not (
                proof.provider_request_id or proof.provider_request_id_unavailable_confirmed
            ):
                raise ModelError("identity_mismatch", "Supplier request identity is required")
            if proof.provider_request_id and any(
                secret and secret in proof.provider_request_id
                for secret in (config.api_key, config.http_token)
            ):
                raise ModelError("invalid_evidence", "Credential cannot be a request identifier")
            if proof.stopped_confirmed or proof.operation == "settle":
                if proof.provider_request_id:
                    request["diagnostics"]["reconciled_request_id"] = proof.provider_request_id
                else:
                    request["supplier_id_unavailable_evidence"] = payload
            if proof.operation == "settle":
                if proof.prices and any(
                    Decimal(str(request["prices"][name])) != value
                    for name, value in proof.prices.model_dump().items()
                ):
                    raise ModelError(
                        "billing_conflict", "Supplier price basis differs from frozen rates"
                    )
                if request.get("external_settlement"):
                    raise ModelError(
                        "evidence_conflict", "Request already has an external settlement"
                    )
                amount, prompt, cached, completion = Decimal(0), 0, 0, 0
                if proof.billing == "charged":
                    amount, prompt, cached, completion = usage_cost(
                        proof.usage.model_dump(), request["prices"]
                    )
                    if amount != proof.amount_rmb:
                        raise ModelError(
                            "billing_conflict", "Usage, frozen rates and amount disagree"
                        )
                old = Decimal(request.get("cost_rmb", "0"))
                if request["billing"] != "usage":
                    if ledger.extra.get("pending_call_id") != call_id:
                        raise ModelError(
                            "evidence_conflict", "Reservation belongs to another request"
                        )
                    for name, value in (
                        ("prompt_tokens", prompt),
                        ("cached_tokens", cached),
                        ("completion_tokens", completion),
                    ):
                        ledger.data[name] += value
                    ledger.data["calls"] += int(not request["usage_recorded"])
                    ledger.extra.update(pending_rmb=0.0, pending_call_id=None)
                elif amount != old:
                    raise ModelError(
                        "billing_conflict", "Original usage conflicts with external settlement"
                    )
                ledger.data["spent_rmb"] = float(
                    Decimal(str(ledger.data["spent_rmb"])) + amount - old
                )
                if request.get("unconfirmed_counter"):
                    ledger.data["unconfirmed_usage_calls"] -= 1
                    request["unconfirmed_counter"] = False
                request.update(billing=proof.billing, cost_rmb=str(amount), external_settlement=key)
            request["stopped_confirmed"] = request["stopped_confirmed"] or proof.stopped_confirmed
            receipt = {
                "evidence_id": key,
                "call_id": call_id,
                "operation": proof.operation,
                "stopped_confirmed": request["stopped_confirmed"],
                "billing": request["billing"],
                "pending_rmb": ledger.extra.get("pending_rmb", 0),
                "spent_rmb": ledger.data["spent_rmb"],
            }
            ledger.extra.setdefault("audit", {})[key] = {
                "digest": digest,
                "evidence": payload,
                "actor_uid": os.geteuid(),
                "recorded_at": timestamp(),
                "before": before,
                "after": {
                    **{k: ledger.data[k] for k in before if k in ledger.data},
                    "pending_rmb": receipt["pending_rmb"],
                    "request": copy.deepcopy(request),
                },
                "receipt": receipt,
            }
            ledger.persist()
            return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.evidence.stat().st_size > 65536:
            raise ModelError("invalid_evidence", "Evidence metadata is too large")
        proof = Evidence.model_validate_json(args.evidence.read_bytes())
        print(json.dumps(reconcile(Config.load(), proof), allow_nan=False))
    except (ModelError, OSError, ValueError, ValidationError, sqlite3.Error) as error:
        print(
            json.dumps(
                {"error": error.code if isinstance(error, ModelError) else "invalid_evidence"}
            )
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
