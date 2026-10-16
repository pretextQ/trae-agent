"""Authorized offline diagnostics/reconciliation checks. No sockets, model or historical data."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from pydantic import ValidationError

from trae_agent.budget import BudgetLedger
from trae_agent.config import Config
from trae_agent.model_client import ModelClient, ModelError
from trae_agent.reconcile import Evidence, reconcile

SECRET = "offline-secret-no-real-credential"
IDENTITY = {"run_id": str(uuid4()), "attempt_id": str(uuid4()), "idempotency_key_sha256": "a" * 64}


def config(root):
    return Config.load(
        {"AGENT_STATE_DIR": str(root), "AGENT_MODEL_API_KEY": SECRET, "AGENT_RUN_BUDGET_RMB": "1"}
    )


def model(config, status=500, *, usage=None, bad_id=False):
    real_client = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        body = {"error": {"code": 503, "message": SECRET}}
        if status == 200:
            body = {
                "id": "response-offline",
                "choices": [{"finish_reason": "stop", "message": {"content": "offline"}}],
                "usage": usage,
            }
        return httpx.Response(
            status,
            json=body,
            headers={
                "x-request-id": SECRET if bad_id else "provider-offline",
                "set-cookie": SECRET,
                "authorization": SECRET,
            },
        )

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    ledger = BudgetLedger(config)
    with patch("trae_agent.model_client.httpx.Client", client):
        try:
            response = ModelClient(config, ledger, [], identity=IDENTITY).complete(
                [{"role": "user", "content": "sensitive-request-not-for-ledger"}], 1, call_index=7
            )
            assert response.diagnostics["response_id"] == "response-offline"
        except ModelError as error:
            assert error.code in {"model_http_error", "usage_invalid", "usage_missing"}, error.code
            assert SECRET not in str(error) + json.dumps(error.diagnostics)
    assert len(calls) == 1
    text = ledger.path.read_text()
    assert SECRET not in text and "sensitive-request-not-for-ledger" not in text
    call_id = ledger.extra["pending_call_id"] or next(iter(ledger.extra["requests"]))
    return ledger, call_id


def proof(ledger, call_id, **changes):
    data = {
        "format_version": 1,
        "evidence_id": str(uuid4()),
        "operation": "observe",
        "call_id": call_id,
        "identity": ledger.request(call_id)["identity"],
        "source": "supplier_record",
        "source_reference": str(uuid4()),
        "evidence_sha256": "b" * 64,
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_request_id": "provider-offline",
        "prices": ledger.request(call_id)["prices"],
    }
    return Evidence.model_validate({**data, **changes})


def rejects(code, function):
    try:
        function()
    except ModelError as error:
        assert error.code == code, (error.code, code)
    else:
        raise AssertionError("expected " + code)


def cli(config, evidence):
    path = config.state_dir / "evidence.json"
    path.write_text(evidence.model_dump_json())
    env = {**os.environ, "AGENT_STATE_DIR": str(config.state_dir), "AGENT_RUN_BUDGET_RMB": "1"}
    env.pop("AGENT_MODEL_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-m", "trae_agent.reconcile", "--evidence", str(path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )


def verify_current(root):
    cfg = config(root)
    ledger, call_id = model(cfg)
    pending = ledger.snapshot()["reserved_rmb"]
    assert pending > 0 and not ledger.stop_confirmed(IDENTITY["run_id"])
    before = ledger.path.read_bytes()
    for name in IDENTITY:
        wrong = {
            **ledger.request(call_id)["identity"],
            name: "0" * 64 if name == "idempotency_key_sha256" else str(uuid4()),
        }
        rejects(
            "identity_mismatch",
            lambda wrong=wrong: reconcile(cfg, proof(ledger, call_id, identity=wrong)),
        )
        assert ledger.path.read_bytes() == before
    rejects(
        "identity_mismatch",
        lambda: reconcile(cfg, proof(ledger, call_id, provider_request_id="other")),
    )
    observe = proof(ledger, call_id)
    receipt = reconcile(cfg, observe)
    assert receipt["pending_rmb"] == pending and not receipt["stopped_confirmed"]
    rejects("spend_unconfirmed", lambda: BudgetLedger(cfg).reserve({}))
    stop = proof(ledger, call_id, stopped_confirmed=True)
    receipt = reconcile(cfg, stop)
    assert receipt["pending_rmb"] == pending and receipt["stopped_confirmed"]
    data = ledger.path.read_bytes()
    assert reconcile(cfg, stop) == receipt and ledger.path.read_bytes() == data
    conflict = stop.model_copy(update={"stopped_confirmed": False})
    rejects("evidence_conflict", lambda: reconcile(cfg, conflict))
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "prompt_tokens_details": {"cached_tokens": 4},
    }
    settle = proof(
        ledger, call_id, operation="settle", billing="charged", amount_rmb="0.00001008", usage=usage
    )
    wrong = settle.model_copy(update={"evidence_id": uuid4(), "amount_rmb": settle.amount_rmb * 2})
    rejects("billing_conflict", lambda: reconcile(cfg, wrong))
    receipt = reconcile(cfg, settle)
    assert receipt["pending_rmb"] == 0 and receipt["spent_rmb"] == 0.00001008
    reopened = BudgetLedger(cfg)
    assert reopened.snapshot()["calls"] == 1 and reopened.snapshot()["cached_tokens"] == 4
    data = ledger.path.read_bytes()
    result = cli(cfg, settle)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == receipt and ledger.path.read_bytes() == data
    rejects(
        "evidence_conflict",
        lambda: reconcile(cfg, settle.model_copy(update={"evidence_id": uuid4()})),
    )
    audit = reopened.extra["audit"][str(settle.evidence_id)]
    assert audit["before"]["pending_rmb"] == pending and audit["after"]["pending_rmb"] == 0
    assert audit["after"]["request"]["identity"] == settle.identity.model_dump()
    assert root.stat().st_mode & 0o077 == 0 and ledger.path.stat().st_mode & 0o077 == 0
    import fcntl

    with (root / "protocol.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = cli(cfg, settle)
        assert result.returncode == 2 and json.loads(result.stdout)["error"] == "service_active"
    root.chmod(0o755)
    result = cli(cfg, settle)
    assert result.returncode == 2 and json.loads(result.stdout)["error"] == "permission_denied"
    root.chmod(0o700)
    print(
        json.dumps({"scenario": "identity-stop-billing-audit-restart-permissions", "passed": True})
    )


def verify_separation(root):
    cfg = config(root)
    ledger, call_id = model(cfg)
    zero = proof(ledger, call_id, operation="settle", billing="not_charged", amount_rmb=0)
    receipt = reconcile(cfg, zero)
    assert receipt["pending_rmb"] == 0 and not receipt["stopped_confirmed"]
    assert not BudgetLedger(cfg).stop_confirmed(IDENTITY["run_id"])
    rejects("spend_unconfirmed", lambda: BudgetLedger(cfg).reserve({}))
    for invalid in (
        {"operation": "settle"},
        {"operation": "settle", "billing": "not_charged", "amount_rmb": 1},
        {"source": "operator_review", "stopped_confirmed": True},
    ):
        try:
            proof(ledger, call_id, **invalid)
        except ValidationError:
            pass
        else:
            raise AssertionError("insufficient supplier evidence accepted")
    print(json.dumps({"scenario": "bill-does-not-prove-stop", "passed": True}))


def verify_completion(root):
    cfg = config(root)
    ledger, _ = model(cfg, 200)
    assert ledger.stop_confirmed(IDENTITY["run_id"])
    assert (
        ledger.snapshot()["reserved_rmb"] > 0 and ledger.snapshot()["unconfirmed_usage_calls"] == 1
    )
    rejects("spend_unconfirmed", lambda: ledger.reserve({}))
    print(
        json.dumps(
            {"scenario": "valid-completion-without-usage-is-stopped-unbilled", "passed": True}
        )
    )


def verify_success(root):
    cfg = config(root)
    ledger, call_id = model(cfg, 200, usage={"prompt_tokens": 10, "completion_tokens": 2})
    assert ledger.snapshot()["spent_rmb"] == 0.000014
    assert ledger.snapshot()["reserved_rmb"] == 0 and ledger.snapshot()["calls"] == 1
    assert ledger.stop_confirmed(IDENTITY["run_id"])
    rejects("request_identity_conflict", lambda: ledger.record({}, call_id))
    data = json.loads(ledger.path.read_text())
    data["format_version"] = 999
    ledger.path.write_text(json.dumps(data))
    rejects("ledger_unreadable", lambda: BudgetLedger(cfg))
    print(json.dumps({"scenario": "normal-usage-and-unsupported-version", "passed": True}))


def verify_legacy(root):
    cfg = config(root)
    path = root / "budget_ledger.json"
    old = {
        "spent_rmb": 0.01,
        "calls": 1,
        "prompt_tokens": 1,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "unconfirmed_usage_calls": 0,
        "extra": {"pending_rmb": 0.02},
    }
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    ledger = BudgetLedger(cfg)
    assert ledger.snapshot()["format_version"] == 1 and path.read_bytes() == original
    rejects("spend_unconfirmed", lambda: ledger.reserve({}))
    key, task_id = "offline-original-key", str(uuid4())
    identity = {
        **IDENTITY,
        "idempotency_key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "call_index": 7,
    }
    protocol = root / "protocol.sqlite3"
    with sqlite3.connect(protocol) as db:
        db.executescript(
            "CREATE TABLE runs(key TEXT, task TEXT, snapshot TEXT);"
            "CREATE TABLE events(run TEXT, sequence INTEGER, event TEXT);"
        )
        task = {
            "protocol_version": "1.0",
            "attempt_id": identity["attempt_id"],
            "idempotency_key": key,
            "task_id": task_id,
            "deadline": datetime.now(UTC).isoformat(),
            "input": {},
            "traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01",
        }
        snapshot = {
            "protocol_version": "1.0",
            "revision": 3,
            "observed_at": datetime.now(UTC).isoformat(),
            "run_id": identity["run_id"],
            "attempt_id": identity["attempt_id"],
            "task_id": task_id,
            "status": "unknown",
            "result": {
                "protocol_version": "1.0",
                "run_id": identity["run_id"],
                "status": "unknown",
                "output": None,
                "error": {"code": "model_http_error", "message": "offline error"},
                "stopped_confirmed": False,
                "completed_at": None,
                "cleanup_status": "unknown",
                "evidence": {
                    "status": "partial",
                    "dropped_events": 0,
                    "details": {"budget_reservation_rmb": 0.02},
                },
            },
        }
        db.execute("INSERT INTO runs VALUES(?,?,?)", (key, json.dumps(task), json.dumps(snapshot)))
        for i, (kind, data) in enumerate(
            (
                ("task.started", {"model_id": cfg.model_id}),
                ("model.request", {"call_index": 7}),
                ("model.error", {"call_index": 7}),
            )
        ):
            db.execute(
                "INSERT INTO events VALUES(?,?,?)",
                (
                    identity["run_id"],
                    i,
                    json.dumps(
                        {
                            "type": "custom.trae_agent." + kind,
                            "data": data,
                            "occurred_at": datetime.now(UTC).isoformat(),
                        }
                    ),
                ),
            )
    proto_bytes = protocol.read_bytes()
    evidence = Evidence.model_validate(
        {
            "format_version": 1,
            "evidence_id": str(uuid4()),
            "operation": "bind_legacy",
            "call_id": str(uuid4()),
            "identity": identity,
            "source": "operator_review",
            "source_reference": str(uuid4()),
            "evidence_sha256": "b" * 64,
            "observed_at": datetime.now(UTC).isoformat(),
            "ledger_sha256": hashlib.sha256(original).hexdigest(),
            "protocol_sha256": hashlib.sha256(proto_bytes).hexdigest(),
            "prices": {"input": "1", "cache": "0.02", "output": "2"},
        }
    )
    bad = evidence.model_copy(deep=True)
    bad.identity.call_index = 8
    rejects("identity_mismatch", lambda: reconcile(cfg, bad))
    assert path.read_bytes() == original
    receipt = reconcile(cfg, evidence)
    assert receipt["pending_rmb"] == 0.02 and receipt["spent_rmb"] == 0.01
    assert reconcile(cfg, evidence) == receipt
    assert protocol.read_bytes() == proto_bytes
    zero = Evidence.model_validate(
        {
            **evidence.model_dump(),
            "evidence_id": uuid4(),
            "operation": "settle",
            "source": "supplier_record",
            "billing": "not_charged",
            "amount_rmb": 0,
            "provider_request_id": "provider-legacy",
        }
    )
    settled = reconcile(cfg, zero)
    assert (
        settled["spent_rmb"] == 0.01
        and settled["pending_rmb"] == 0
        and not settled["stopped_confirmed"]
    )
    assert protocol.read_bytes() == proto_bytes
    print(
        json.dumps(
            {"scenario": "legacy-explicit-binding-preserves-totals-and-protocol", "passed": True}
        )
    )


def main():
    for verify in (
        verify_current,
        verify_separation,
        verify_completion,
        verify_success,
        verify_legacy,
    ):
        with tempfile.TemporaryDirectory(prefix="offline-recovery-") as directory:
            verify(Path(directory))
    with tempfile.TemporaryDirectory(prefix="offline-redaction-") as directory:
        cfg = config(Path(directory))
        ledger, call_id = model(cfg, bad_id=True)
        assert SECRET not in ledger.path.read_text()
        missing = proof(ledger, call_id, provider_request_id=None, stopped_confirmed=True)
        rejects("identity_mismatch", lambda: reconcile(cfg, missing))
        confirmed = missing.model_copy(update={"provider_request_id_unavailable_confirmed": True})
        receipt = reconcile(cfg, confirmed)
        assert receipt["stopped_confirmed"] and receipt["pending_rmb"] > 0
        print(
            json.dumps(
                {"scenario": "supplier-confirms-no-id-with-original-identity-proof", "passed": True}
            )
        )
    print(json.dumps({"verification": "complete"}), flush=True)


if __name__ == "__main__":
    main()
