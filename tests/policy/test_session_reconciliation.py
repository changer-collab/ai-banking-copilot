from __future__ import annotations

import pytest

from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.receipt_store import (
    ReceiptReadError,
    ReceiptRecord,
    StoreReceiptReader,
)
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.gateway.transfer_store import due_execution_key
from ai_banking_copilot.policy.session_recovery import (
    InFlightNode,
    ReconcileStatus,
    business_key_for,
    reconcile,
)
from ai_banking_copilot.tools.registry import ToolCall


class _Audit:
    def __init__(self, events, valid=True):
        self._events = events
        self._valid = valid

    def verify_chain(self):
        return self._valid

    def events(self):
        return tuple(self._events)


class _Receipts:
    def __init__(self, record):
        self.record = record

    def by_key(self, key):
        return self.record if self.record and self.record.idempotency_key == key else None


def test_reconcile_verified_and_action_mapping() -> None:
    node = InFlightNode("n1", "req1", "req1", "transfer.create", "m1")
    receipt = ReceiptRecord("req1", "transfer", "settled", ("txn:1",), ())
    event = type("Event", (), {"kind": "execution", "payload": {
        "action": "transfer.create", "idempotency_key": "req1",
        "record_ids": ["txn:1"], "frozen_hash": "m1", "status": "settled",
    }})()
    result = reconcile(node, _Receipts(receipt), _Audit([event]))
    assert result.status is ReconcileStatus.VERIFIED


def test_reconcile_missing_audit_is_unverified_and_conflict_is_manual() -> None:
    node = InFlightNode("n1", "req1", "req1", "transfer.create", "m1")
    receipt = ReceiptRecord("req1", "transfer", "settled", ("txn:1",), ())
    assert reconcile(
        node, _Receipts(receipt), _Audit([])
    ).status is ReconcileStatus.COMMITTED_UNVERIFIED
    bad = type(
        "Event",
        (),
        {
            "kind": "execution",
            "payload": {
                "idempotency_key": "req1",
                "action": "transfer.create",
                "record_ids": ["txn:2"],
                "frozen_hash": "m1",
            },
        },
    )()
    assert reconcile(
        node, _Receipts(receipt), _Audit([bad])
    ).status is ReconcileStatus.MANUAL_CONFLICT


def test_reconcile_chain_failure_is_manual() -> None:
    node = InFlightNode("n1", "req1", "req1", "merchant.order", "m1")
    assert reconcile(
        node, _Receipts(None), _Audit([], valid=False)
    ).status is ReconcileStatus.MANUAL_CONFLICT


def test_reconcile_no_receipt_no_audit_is_not_committed() -> None:
    node = InFlightNode("n1", "req1", "req1", "transfer.create", "m1")
    assert reconcile(
        node, _Receipts(None), _Audit([])
    ).status is ReconcileStatus.NOT_COMMITTED


def test_reconcile_audit_without_receipt_is_manual() -> None:
    node = InFlightNode("n1", "req1", "req1", "transfer.create", "m1")
    event = type("Event", (), {"kind": "execution", "payload": {
        "idempotency_key": "req1",
    }})()
    assert reconcile(
        node, _Receipts(None), _Audit([event])
    ).status is ReconcileStatus.MANUAL_CONFLICT


def test_due_business_keys_use_schedule_and_occasion_identity() -> None:
    transfer = ToolCall("transfer.create", {"schedule_id": "sched-1"})
    merchant = ToolCall("merchant.order", {"occasion_id": "occasion-1"})
    assert business_key_for(transfer, "req", "u1") == due_execution_key(
        "transfer", "u1", "sched-1"
    )
    assert business_key_for(merchant, "req", "u1") == due_execution_key(
        "place_merchant_order", "u1", "occasion-1"
    )


def test_due_business_keys_unwrap_values() -> None:
    for action, parameter, primitive in (
        ("transfer.create", "schedule_id", "transfer"),
        ("merchant.order", "occasion_id", "place_merchant_order"),
    ):
        call = ToolCall(action, {parameter: Value("ref-1", Trust.TRUSTED)})
        assert business_key_for(call, "request", "u1") == due_execution_key(
            primitive, "u1", "ref-1"
        )


def test_receipt_missing_table_becomes_manual_conflict(tmp_path) -> None:
    store = Store(tmp_path / "missing.sqlite")
    reader = StoreReceiptReader(store)
    with pytest.raises(ReceiptReadError, match="^receipt_read_failed$"):
        reader.by_key("req1")
    node = InFlightNode("n1", "req1", "req1", "transfer.create", "m1")
    result = reconcile(node, reader, _Audit([]))
    assert result.status is ReconcileStatus.MANUAL_CONFLICT
    assert result.reason == "evidence_read_failed"
    store.close()


def test_reconcile_does_not_expose_evidence_error_detail() -> None:
    class BrokenReader:
        def by_key(self, key):
            raise ReceiptReadError("database-secret-path")

    node = InFlightNode("n1", "req1", "req1", "transfer.create", "m1")
    result = reconcile(node, BrokenReader(), _Audit([]))
    assert result.status is ReconcileStatus.MANUAL_CONFLICT
    assert result.reason == "evidence_read_failed"
