from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan, PlanNode, cfhash
from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.policy.breaker import BreakerSnapshot, BreakerState
from ai_banking_copilot.policy.pep import ExecResult, manifest_hash
from ai_banking_copilot.policy.session_recovery import InFlightNode, business_key_for, call_for_node
from ai_banking_copilot.policy.verification import (
    ChallengeRecord,
    ChallengeStatus,
    VerificationBinding,
)
from ai_banking_copilot.tools.registry import ToolCall
from ai_banking_copilot.web.plan_card import PlanCard
from ai_banking_copilot.web.session import ConfirmationRecord, DailyQuota, _Pending
from ai_banking_copilot.web.session_codec import (
    decode_snapshot,
    decode_value,
    encode_snapshot,
    encode_value,
)
from ai_banking_copilot.web.session_snapshot import (
    SESSION_SNAPSHOT_VERSION,
    SessionSnapshot,
    SessionSnapshotError,
)


def _snapshot() -> SessionSnapshot:
    node = PlanNode(
        node_id="n1",
        action="transfer.create",
        params={"amount_cents": 123, "account_id": "acc_savings"},
        intent=IntentRef(AnchorSource.EXPLICIT, "a1"),
    )
    plan = Plan("p1", (node,))
    return SessionSnapshot(
        version=SESSION_SNAPSHOT_VERSION, user_id="u1", session_id="s1", revision=0,
        execution_id="e1", state="awaiting_confirm",
        payload={
            "plan": plan, "cfhash": cfhash(plan.nodes),
            "decision": Decision(Level.YELLOW, (ReasonCode.STATE_CHANGE_SMALL,)),
            "money": Money(123),
            "slot": Slot(SlotKind.CONTROL, Value("acc_savings", Trust.TRUSTED)),
            "items": ("x", 2),
        },
    )


def _pending(snapshot):
    plan, decision = snapshot.payload["plan"], snapshot.payload["decision"]
    card = PlanCard("c1", snapshot.payload["cfhash"], Level.YELLOW, ("line",), ("user_confirm",))
    return _Pending(card, plan, decision, (decision,), ("n1",), "intent", ())


@pytest.mark.parametrize("field,broken", [
    ("decision", "invalid"), ("plan", "invalid"), ("card", "invalid"),
    ("node_ids", ()), ("node_ids", ("other",)), ("node_decisions", ("invalid",)),
    ("node_decisions", ()), ("flags", ()), ("intent_text", 42),
])
def test_codec_rejects_malformed_pending(field, broken) -> None:
    snapshot = _snapshot()
    pending = _pending(snapshot)
    snapshot.payload["pending"] = pending
    assert decode_snapshot(encode_snapshot(snapshot), user_id="u1", session_id="s1") == snapshot
    snapshot.payload["pending"] = replace(pending, **{field: broken})
    with pytest.raises(SessionSnapshotError):
        encode_snapshot(snapshot)


def test_codec_rejects_confirmation_bound_to_another_pending_plan() -> None:
    snapshot = _snapshot()
    snapshot.payload["pending"] = _pending(snapshot)
    other = Plan("other", (PlanNode(
        "other", "account.balance", {}, IntentRef(AnchorSource.EXPLICIT, "other"),
    ),))
    snapshot.payload["confirmation"] = ConfirmationRecord(
        other, cfhash(other.nodes), "e1", Level.YELLOW, False,
    )
    with pytest.raises(SessionSnapshotError):
        encode_snapshot(snapshot)


def test_pending_node_ids_preserve_frozen_plan_order() -> None:
    snapshot = _snapshot()
    first = PlanNode("first", "account.balance", {}, IntentRef(AnchorSource.EXPLICIT, "a"))
    second = PlanNode(
        "second", "card.list", {}, IntentRef(AnchorSource.EXPLICIT, "b"), ("first",),
    )
    plan = Plan("out-of-order", (second, first))
    snapshot.payload.update(plan=plan, cfhash=cfhash(plan.nodes))
    pending = _pending(snapshot)
    pending = replace(pending, node_ids=("second", "first"),
                      node_decisions=(pending.decision, pending.decision))
    snapshot.payload["pending"] = pending
    assert decode_snapshot(encode_snapshot(snapshot), user_id="u1", session_id="s1") == snapshot
    snapshot.payload["pending"] = replace(pending, node_ids=("first", "second"))
    with pytest.raises(SessionSnapshotError):
        encode_snapshot(snapshot)


def test_round_trip_explicit_types_and_hash() -> None:
    restored = decode_snapshot(encode_snapshot(_snapshot()), user_id="u1", session_id="s1")
    assert restored == _snapshot()
    assert isinstance(restored.payload["money"], Money)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(version=99),
        lambda data: data.update(user_id="other"),
        lambda data: data["payload"].update(cfhash="0" * 64),
        lambda data: data["payload"]["slot"]["value"].update(trust="unverified"),
    ],
)
def test_codec_rejects_bad_snapshot(mutate) -> None:
    data = json.loads(encode_snapshot(_snapshot()))
    mutate(data)
    with pytest.raises(ValueError):
        decode_snapshot(json.dumps(data), user_id="u1", session_id="s1")


def test_codec_rejects_unknown_payload_object() -> None:
    snapshot = _snapshot()
    snapshot.payload["bad"] = object()
    with pytest.raises(SessionSnapshotError):
        encode_snapshot(snapshot)


def test_codec_round_trips_digest_challenge() -> None:
    snapshot = _snapshot()
    snapshot.payload["challenge"] = ChallengeRecord(
        "challenge-1", VerificationBinding("u1", "s1", "e1", "c1", "digest"),
        "code-digest", datetime(2026, 10, 20, tzinfo=UTC), ChallengeStatus.ACTIVE,
    )
    encoded = encode_snapshot(snapshot)
    assert "code-digest" in encoded
    assert '"code":' not in encoded
    assert decode_snapshot(encoded, user_id="u1", session_id="s1") == snapshot


def test_codec_rejects_challenge_identity_mismatch() -> None:
    snapshot = _snapshot()
    snapshot.payload["challenge"] = ChallengeRecord(
        "challenge-1", VerificationBinding("other", "s1", "e1", "c1", "digest"),
        "code-digest", datetime(2026, 10, 20, tzinfo=UTC), ChallengeStatus.ACTIVE,
    )
    with pytest.raises(ValueError, match="snapshot_challenge_identity_mismatch"):
        encode_snapshot(snapshot)


@pytest.mark.parametrize("field", ["decision", "slot"])
def test_codec_rejects_broken_contract_fields(field) -> None:
    data = json.loads(encode_snapshot(_snapshot()))
    if field == "decision":
        data["payload"][field]["reasons"] = {"$type": "tuple", "items": ["fake"]}
    else:
        data["payload"][field]["kind"] = {"$type": "SlotKind", "value": "data"}
        data["payload"][field]["value"] = "trusted-looking-text"
    with pytest.raises(ValueError):
        decode_snapshot(json.dumps(data), user_id="u1", session_id="s1")


def test_snapshot_rejects_non_string_execution_identity() -> None:
    with pytest.raises(SessionSnapshotError):
        replace(_snapshot(), execution_id=42)


def test_snapshot_rejects_malformed_quota_and_risk_counters() -> None:
    snapshot = _snapshot()
    snapshot.payload["quota"] = DailyQuota("not-int", True)
    with pytest.raises(SessionSnapshotError):
        encode_snapshot(snapshot)
    snapshot.payload.pop("quota")
    snapshot.payload["policy_rejections"] = True
    with pytest.raises(SessionSnapshotError):
        encode_snapshot(snapshot)


@pytest.mark.parametrize(
    "kind", ["confirmation", "challenge", "binding", "result", "flight", "breaker"],
)
def test_codec_rejects_important_record_field_types(kind) -> None:
    snapshot = _snapshot()
    binding = VerificationBinding("u1", "s1", "e1", "c1", "digest")
    cases = {
        "confirmation": (ConfirmationRecord(
            snapshot.payload["plan"], snapshot.payload["cfhash"], "e1", Level.YELLOW, False,
        ), "strong_verified", "true"),
        "challenge": (ChallengeRecord(
            "c1", binding, "digest", datetime(2026, 10, 20, tzinfo=UTC), ChallengeStatus.ACTIVE,
        ), "expires_at", "not-a-time"),
        "binding": (binding, "user_id", 42),
        "result": (ExecResult("settled", ("txn:1",), "ok"), "record_ids", "txn:1"),
        "flight": (InFlightNode("n1", "r", "b", "transfer.create", "m"), "node_id", 42),
        "breaker": (BreakerSnapshot(BreakerState.CLOSED), "triggers", {
            "$type": "tuple", "items": ["bad"],
        }),
    }
    record, field, broken = cases[kind]
    data = encode_value(record)
    data[field] = broken
    with pytest.raises(SessionSnapshotError):
        decode_value(data)


@pytest.mark.parametrize("action,reference", [
    ("transfer.create", "schedule_id"), ("merchant.order", "occasion_id"),
])
def test_codec_round_trips_in_flight_due_binding(action, reference) -> None:
    snapshot = _snapshot()
    node = PlanNode("n1", action, {reference: "ref1"}, IntentRef(AnchorSource.EXPLICIT, "a1"))
    plan = Plan("due", (node,))
    digest = cfhash(plan.nodes)
    call = call_for_node(node, "u1")
    request = f"e1:{digest}:n1"
    snapshot.payload.update(plan=plan, cfhash=digest, in_flight=InFlightNode(
        "n1", request, business_key_for(call, request, "u1"), action, manifest_hash(call),
    ))
    assert decode_snapshot(encode_snapshot(snapshot), user_id="u1", session_id="s1") == snapshot


@pytest.mark.parametrize(
    "field", ["node_id", "action", "request_key", "manifest_hash", "business_key"],
)
def test_snapshot_rejects_in_flight_not_bound_to_plan(field) -> None:
    snapshot = _snapshot()
    plan = snapshot.payload["plan"]
    digest = snapshot.payload["cfhash"]
    assert isinstance(plan, Plan)
    call = ToolCall("transfer.create", {
        "user_id": Value("u1", Trust.TRUSTED),
        "amount_cents": Value(123, Trust.TRUSTED),
        "account_id": Value("acc_savings", Trust.TRUSTED),
    })
    request = f"e1:{digest}:n1"
    flight = InFlightNode("n1", request, request, "transfer.create", manifest_hash(call))
    snapshot.payload["in_flight"] = flight
    assert decode_snapshot(encode_snapshot(snapshot), user_id="u1", session_id="s1") == snapshot
    snapshot.payload["in_flight"] = replace(flight, **{field: "corrupted"})
    with pytest.raises(SessionSnapshotError):
        encode_snapshot(snapshot)


def test_codec_rejects_unknown_action_and_dependency_gap() -> None:
    snapshot = _snapshot()
    node = PlanNode(
        "n1", "unknown.write", {}, IntentRef(AnchorSource.EXPLICIT, "a1")
    )
    snapshot.payload["plan"] = Plan("bad", (node,))
    snapshot.payload["cfhash"] = cfhash((node,))
    with pytest.raises(ValueError, match="unknown_snapshot_action"):
        encode_snapshot(snapshot)

    first = PlanNode(
        "n1", "account.balance", {"account_id": "a"},
        IntentRef(AnchorSource.EXPLICIT, "a1"),
    )
    second = PlanNode(
        "n2", "card.list", {"user_id": "u1"},
        IntentRef(AnchorSource.EXPLICIT, "a2"), ("n1",),
    )
    plan = Plan("dependent", (first, second))
    snapshot.payload.update(
        plan=plan,
        cfhash=cfhash(plan.nodes),
        completed_node_ids=("n2",),
    )
    with pytest.raises(ValueError, match="snapshot_completed_dependency_gap"):
        encode_snapshot(snapshot)

    snapshot.payload["completed_node_ids"] = ("n1",)
    snapshot.payload["pending_node_ids"] = ("n1", "n2")
    with pytest.raises(ValueError, match="snapshot_node_sets_overlap"):
        encode_snapshot(snapshot)
