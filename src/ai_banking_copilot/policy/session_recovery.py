"""恢复时只读对账；不执行 PEP，也不追加或伪造业务成功审计。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol

from ai_banking_copilot.contracts.plan import PlanNode
from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.receipt_store import ReceiptReader, ReceiptRecord
from ai_banking_copilot.gateway.transfer_store import due_execution_key
from ai_banking_copilot.policy.audit import KIND_EXECUTION, AuditEvent
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ToolCall

_PRIMITIVES = {
    "transfer.create": "transfer", "transfer.schedule": "transfer",
    "transfer.book": "book_transfer", "transfer.request_collection": "request_collection",
    "subscription.cancel": "cancel_subscription", "subscription.sign": "sign_subscription",
    "funds.freeze": "freeze_funds", "funds.release": "unfreeze_funds",
    "wealth.subscribe": "subscribe_product", "wealth.redeem": "redeem_product",
    "card.lock": "set_card_status", "card.unlock": "set_card_status",
    "card.report_lost": "set_card_status", "card.apply_virtual": "apply_virtual_card",
    "card.set_limit": "set_card_limit", "merchant.order": "place_merchant_order",
}


@dataclass(frozen=True)
class InFlightNode:
    node_id: str
    request_key: str
    business_key: str
    action: str
    manifest_hash: str


class ReconcileStatus(StrEnum):
    NOT_COMMITTED = "not_committed"
    COMMITTED_UNVERIFIED = "committed_unverified"
    VERIFIED = "verified"
    MANUAL_CONFLICT = "manual_conflict"


@dataclass(frozen=True)
class ReconcileResult:
    status: ReconcileStatus
    receipt: ReceiptRecord | None = None
    audit_ids: tuple[str, ...] = ()
    reason: str = ""

    @property
    def record_ids(self) -> tuple[str, ...]:
        return self.receipt.record_ids if self.receipt is not None else ()


class RecoveryAudit(Protocol):
    def verify_chain(self) -> bool: ...

    def events(self) -> tuple[AuditEvent, ...]: ...


def business_key_for(call: ToolCall, request_key: str, user_id: str) -> str:
    if call.action in {"transfer.create", "transfer.schedule"}:
        reference = call.params.get("schedule_id")
        reference = _raw(reference)
        if isinstance(reference, str) and reference:
            return due_execution_key("transfer", user_id, reference)
    if call.action == "merchant.order":
        reference = call.params.get("occasion_id")
        reference = _raw(reference)
        if isinstance(reference, str) and reference:
            return due_execution_key("place_merchant_order", user_id, reference)
    return request_key


def _raw(value: object) -> object:
    if isinstance(value, Slot):
        value = value.value
    return value.raw if isinstance(value, Value) else value


def call_for_node(node: PlanNode, user_id: str) -> ToolCall:
    """按目录恢复执行调用；保留已有 taint，日期只作确定性 ISO 转换。"""
    spec = build_default_registry().get(node.action)
    params: dict[str, object] = {}
    for param in spec.params:
        if param.name not in node.params:
            if param.required and param.slot is SlotKind.CONTROL and param.name == "user_id":
                params[param.name] = Value(user_id, Trust.TRUSTED)
            continue
        value = node.params[param.name]
        trust = Trust.TRUSTED if param.slot is SlotKind.CONTROL else Trust.UNVERIFIED
        if isinstance(value, Slot):
            value = value.value
        if isinstance(value, Value):
            trust, value = value.trust, value.raw
        if param.py_type is date and isinstance(value, str):
            value = date.fromisoformat(value)
        params[param.name] = Value(value, trust)
    return ToolCall(node.action, params)


def reconcile(
    node: InFlightNode, receipts: ReceiptReader, audit: RecoveryAudit,
) -> ReconcileResult:
    if not audit.verify_chain():
        return ReconcileResult(ReconcileStatus.MANUAL_CONFLICT, reason="audit_chain_invalid")
    try:
        receipt = receipts.by_key(node.business_key)
        events = tuple(event for event in audit.events()
                       if event.kind == KIND_EXECUTION
                       and event.payload.get("idempotency_key") == node.request_key)
    except (ValueError, RuntimeError):
        return ReconcileResult(
            ReconcileStatus.MANUAL_CONFLICT,
            reason="evidence_read_failed",
        )
    if receipt is not None and receipt.action != _PRIMITIVES.get(node.action):
        return ReconcileResult(ReconcileStatus.MANUAL_CONFLICT, receipt,
                               reason="receipt_action_conflict")
    if not events:
        return ReconcileResult(
            ReconcileStatus.NOT_COMMITTED if receipt is None
            else ReconcileStatus.COMMITTED_UNVERIFIED, receipt,
        )
    if receipt is None:
        return ReconcileResult(ReconcileStatus.MANUAL_CONFLICT, reason="audit_without_receipt")
    for event in events:
        payload = event.payload
        if (payload.get("action") != node.action
                or payload.get("frozen_hash") != node.manifest_hash
                or payload.get("record_ids") != list(receipt.record_ids)
                or payload.get("status") != receipt.status):
            return ReconcileResult(ReconcileStatus.MANUAL_CONFLICT, receipt,
                                   reason="audit_receipt_binding_conflict")
    return ReconcileResult(
        ReconcileStatus.VERIFIED,
        receipt,
        tuple(getattr(event, "event_id", "") for event in events),
    )
