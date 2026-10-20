"""显式白名单 JSON codec。任何未知类型、动作或破损绑定均拒绝恢复。"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime
from enum import Enum

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
    validate_plan,
)
from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.hashing import canonical_json
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.policy.breaker import BreakerSnapshot, BreakerState, BreakerTrigger
from ai_banking_copilot.policy.pep import manifest_hash
from ai_banking_copilot.policy.session_recovery import InFlightNode, business_key_for, call_for_node
from ai_banking_copilot.policy.verification import (
    ChallengeRecord,
    ChallengeStatus,
    VerificationBinding,
)
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.web.session_snapshot import SessionSnapshot, SessionSnapshotError


def _constructors() -> dict[str, Callable[..., object]]:
    # 静态白名单延迟导入，避免 Session 在导入 codec 时形成循环。
    from ai_banking_copilot.planning.reviewer import ReviewReport
    from ai_banking_copilot.policy.pep import ExecResult
    from ai_banking_copilot.policy.session_recovery import InFlightNode
    from ai_banking_copilot.web.plan_card import PlanCard
    from ai_banking_copilot.web.session import (
        ConfirmationRecord,
        DailyQuota,
        TakeoverPackage,
        _Pending,
    )

    return {
        "Plan": Plan, "PlanNode": PlanNode, "IntentRef": IntentRef,
        "Decision": Decision, "Value": Value, "Slot": Slot, "Money": Money,
        "PlanCard": PlanCard, "ConfirmationRecord": ConfirmationRecord,
        "DailyQuota": DailyQuota, "TakeoverPackage": TakeoverPackage,
        "ChallengeRecord": ChallengeRecord, "VerificationBinding": VerificationBinding,
        "_Pending": _Pending, "ExecResult": ExecResult,
        "ReviewReport": ReviewReport, "BreakerSnapshot": BreakerSnapshot,
        "InFlightNode": InFlightNode,
    }


def _enums() -> dict[str, type[Enum]]:
    from ai_banking_copilot.planning.reviewer import ReviewChannel, ReviewerFailure, ReviewStatus
    from ai_banking_copilot.web.session import SessionState

    return {kind.__name__: kind for kind in (
        AnchorSource, Level, ReasonCode, Trust, SlotKind, BreakerState, BreakerTrigger,
        ReviewStatus, ReviewChannel, ReviewerFailure, ChallengeStatus, SessionState,
    )}


def _encode(value: object) -> object:
    if isinstance(value, Enum):
        if type(value).__name__ not in _enums():
            raise TypeError("unsupported_snapshot_enum")
        return {"$type": type(value).__name__, "value": value.value}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non_finite_snapshot_number")
        return value
    if isinstance(value, datetime | date):
        return {"$type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, tuple | list | frozenset):
        kind = type(value).__name__
        items = sorted(value) if isinstance(value, frozenset) else value
        return {"$type": kind, "items": [_encode(item) for item in items]}
    if isinstance(value, Mapping):
        if "$type" in value or any(not isinstance(key, str) for key in value):
            raise ValueError("invalid_snapshot_mapping_key")
        return {key: _encode(item) for key, item in value.items()}
    kind = type(value).__name__
    if kind in _constructors() and is_dataclass(value) and not isinstance(value, type):
        _validate_record(kind, {item.name: getattr(value, item.name) for item in fields(value)})
        return {"$type": kind, **{
            item.name: _encode(getattr(value, item.name)) for item in fields(value)
        }}
    raise TypeError(f"unsupported_snapshot_type:{kind}")


def _decode(value: object) -> object:
    if isinstance(value, list):
        raise SessionSnapshotError("untagged_snapshot_sequence")
    if not isinstance(value, dict):
        if value is None or isinstance(value, str | bool | int | float):
            if isinstance(value, float) and not math.isfinite(value):
                raise SessionSnapshotError("non_finite_snapshot_number")
            return value
        raise SessionSnapshotError("invalid_snapshot_value")
    kind = value.get("$type")
    if kind is None:
        return {str(key): _decode(item) for key, item in value.items()}
    if not isinstance(kind, str):
        raise SessionSnapshotError("invalid_snapshot_type_tag")
    if kind in _enums():
        if set(value) != {"$type", "value"}:
            raise SessionSnapshotError("invalid_snapshot_enum")
        return _enums()[kind](value["value"])
    if kind in {"date", "datetime"}:
        raw = value.get("value")
        if not isinstance(raw, str) or set(value) != {"$type", "value"}:
            raise SessionSnapshotError("invalid_snapshot_time")
        return datetime.fromisoformat(raw) if kind == "datetime" else date.fromisoformat(raw)
    if kind in {"tuple", "list", "frozenset"}:
        items = value.get("items")
        if not isinstance(items, list) or set(value) != {"$type", "items"}:
            raise SessionSnapshotError("invalid_snapshot_sequence")
        decoded = [_decode(item) for item in items]
        return tuple(decoded) if kind == "tuple" else (
            frozenset(decoded) if kind == "frozenset" else decoded
        )
    constructor = _constructors().get(kind)
    if constructor is None:
        raise SessionSnapshotError(f"unknown_snapshot_type:{kind}")
    arguments = {str(key): _decode(item) for key, item in value.items() if key != "$type"}
    if isinstance(constructor, type) and is_dataclass(constructor):
        if set(arguments) != {item.name for item in fields(constructor)}:
            raise SessionSnapshotError("invalid_snapshot_fields")
    _validate_record(kind, arguments)
    if kind == "Plan":
        return Plan(plan_id=_string(arguments["plan_id"]), nodes=_nodes(arguments["nodes"]))
    if kind == "PlanNode":
        params = arguments["params"]
        intent = arguments["intent"]
        deps = arguments["depends_on"]
        if not isinstance(params, dict) or not isinstance(intent, IntentRef):
            raise SessionSnapshotError("invalid_snapshot_node")
        if not isinstance(deps, tuple) or any(not isinstance(item, str) for item in deps):
            raise SessionSnapshotError("invalid_snapshot_dependencies")
        return PlanNode(_string(arguments["node_id"]), _string(arguments["action"]),
                        params, intent, deps)
    if kind == "IntentRef":
        source = arguments["source"]
        if not isinstance(source, AnchorSource):
            raise SessionSnapshotError("invalid_snapshot_anchor")
        return IntentRef(source, _string(arguments["anchor_id"]))
    if kind == "Decision":
        level, reasons = arguments["level"], arguments["reasons"]
        if (not isinstance(level, Level) or not isinstance(reasons, tuple)
                or any(not isinstance(reason, ReasonCode) for reason in reasons)):
            raise SessionSnapshotError("invalid_snapshot_decision")
        return Decision(level, reasons)
    if kind == "Value":
        trust = arguments["trust"]
        if not isinstance(trust, Trust):
            raise SessionSnapshotError("invalid_snapshot_trust")
        return Value(arguments["raw"], trust)
    if kind == "Slot":
        slot_kind, slot_value = arguments["kind"], arguments["value"]
        if not isinstance(slot_kind, SlotKind) or not isinstance(slot_value, Value):
            raise SessionSnapshotError("invalid_snapshot_slot")
        return Slot(slot_kind, slot_value)
    return constructor(**arguments)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _text_tuple(value: object) -> bool:
    return isinstance(value, tuple) and all(_text(item) for item in value)


def _validate_record(kind: str, data: dict[str, object]) -> None:
    from ai_banking_copilot.web.plan_card import PlanCard

    valid = True
    if kind == "_Pending":
        plan, card, decision = data["plan"], data["card"], data["decision"]
        decisions, node_ids = data["node_decisions"], data["node_ids"]
        flags = data["flags"]
        valid = (isinstance(plan, Plan) and isinstance(card, PlanCard)
                 and isinstance(decision, Decision) and isinstance(node_ids, tuple)
                 and _text_tuple(node_ids)
                 and isinstance(decisions, tuple)
                 and all(isinstance(item, Decision) for item in decisions)
                 and len(decisions) == len(node_ids)
                 and isinstance(data["intent_text"], str) and _text_tuple(data["hit_rules"])
                 and isinstance(flags, frozenset) and all(_text(item) for item in flags))
        if valid:
            assert isinstance(plan, Plan) and isinstance(card, PlanCard)
            assert isinstance(decision, Decision)
            valid = (node_ids == tuple(node.node_id for node in plan.nodes)
                     and card.frozen_hash == _plan_hash(plan) and card.level == decision.level)
    elif kind == "ConfirmationRecord":
        valid = (isinstance(data["plan"], Plan) and _text(data["frozen_hash"])
                 and _text(data["execution_id"]) and isinstance(data["approved_level"], Level)
                 and type(data["strong_verified"]) is bool)
        if valid:
            plan = data["plan"]
            assert isinstance(plan, Plan)
            valid = data["frozen_hash"] == _plan_hash(plan)
    elif kind == "VerificationBinding":
        valid = all(_text(data[key]) for key in (
            "user_id", "session_id", "execution_id", "card_id", "frozen_hash",
        ))
    elif kind == "ChallengeRecord":
        valid = (_text(data["challenge_id"]) and _text(data["code_digest"])
                 and isinstance(data["binding"], VerificationBinding)
                 and isinstance(data["expires_at"], datetime)
                 and isinstance(data["status"], ChallengeStatus)
                 and (data["provider_reference"] is None or _text(data["provider_reference"])))
    elif kind == "ExecResult":
        valid = (_text(data["status"]) and _text_tuple(data["record_ids"])
                 and isinstance(data["message"], str))
    elif kind == "InFlightNode":
        valid = all(_text(data[key]) for key in (
            "node_id", "request_key", "business_key", "action", "manifest_hash",
        ))
    elif kind == "BreakerSnapshot":
        triggers = data["triggers"]
        valid = (isinstance(data["state"], BreakerState) and _count(data["failures"])
                 and isinstance(triggers, tuple)
                 and all(isinstance(item, BreakerTrigger) for item in triggers))
    elif kind == "DailyQuota":
        valid = _count(data["limit_cents"]) and _count(data["accumulated_cents"])
    if not valid:
        raise SessionSnapshotError(f"invalid_snapshot_record:{kind}")


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SessionSnapshotError("invalid_snapshot_string")
    return value


def _nodes(value: object) -> tuple[PlanNode, ...]:
    if not isinstance(value, tuple) or any(not isinstance(item, PlanNode) for item in value):
        raise SessionSnapshotError("invalid_snapshot_nodes")
    return value


def _plan_hash(plan: Plan) -> str:
    # 与执行门一致：日期/金额等参数先规范为 JSON 基础值，再调用原 cfhash。
    nodes = tuple(
        replace(node, params=json.loads(canonical_json(node.params)))
        for node in plan.nodes
    )
    return cfhash(nodes)


def _validate_payload(snapshot: SessionSnapshot) -> None:
    known = set(build_default_registry().actions())
    plans: list[Plan] = []

    def inspect(value: object) -> None:
        if is_dataclass(value) and not isinstance(value, type):
            _validate_record(type(value).__name__, {
                item.name: getattr(value, item.name) for item in fields(value)
            })
        if isinstance(value, ChallengeRecord):
            binding = value.binding
            if (binding.user_id != snapshot.user_id
                    or binding.session_id != snapshot.session_id
                    or binding.execution_id != snapshot.execution_id):
                raise SessionSnapshotError("snapshot_challenge_identity_mismatch")
        if isinstance(value, VerificationBinding) and (
            value.user_id != snapshot.user_id or value.session_id != snapshot.session_id
            or value.execution_id != snapshot.execution_id
        ):
            raise SessionSnapshotError("snapshot_verification_identity_mismatch")
        if isinstance(value, Plan):
            validate_plan(value)
            plans.append(value)
            for node in value.nodes:
                if node.action not in known:
                    raise SessionSnapshotError("unknown_snapshot_action")
                raw_user = node.params.get("user_id")
                if isinstance(raw_user, Value):
                    raw_user = raw_user.raw
                if raw_user is not None and raw_user != snapshot.user_id:
                    raise SessionSnapshotError("snapshot_plan_identity_mismatch")
            _plan_hash(value)
        if isinstance(value, Mapping):
            plan = value.get("plan")
            if isinstance(plan, Plan):
                for key in ("cfhash", "frozen_hash"):
                    if key in value and value[key] != _plan_hash(plan):
                        raise SessionSnapshotError("snapshot_hash_mismatch")
            for item in value.values():
                inspect(item)
        elif isinstance(value, list | tuple | frozenset):
            for item in value:
                inspect(item)
        elif is_dataclass(value) and not isinstance(value, type):
            plan = getattr(value, "plan", None)
            digest = getattr(value, "frozen_hash", None)
            card = getattr(value, "card", None)
            if isinstance(plan, Plan):
                if digest is not None and digest != _plan_hash(plan):
                    raise SessionSnapshotError("snapshot_hash_mismatch")
                if card is not None and getattr(card, "frozen_hash", None) != _plan_hash(plan):
                    raise SessionSnapshotError("snapshot_card_hash_mismatch")
            for item in fields(value):
                inspect(getattr(value, item.name))

    inspect(snapshot.payload)
    confirmation = snapshot.payload.get("confirmation", snapshot.payload.get("_confirmation"))
    if (confirmation is not None
            and getattr(confirmation, "execution_id", None) != snapshot.execution_id):
        raise SessionSnapshotError("snapshot_confirmation_identity_mismatch")
    pending = snapshot.payload.get("pending", snapshot.payload.get("_pending"))
    plan = snapshot.payload.get("plan", getattr(pending, "plan", None))
    if (confirmation is not None and isinstance(plan, Plan)
            and getattr(confirmation, "plan", None) != plan):
        raise SessionSnapshotError("snapshot_confirmation_plan_mismatch")
    for key in ("policy_rejections", "_policy_rejections"):
        if key in snapshot.payload and not _count(snapshot.payload[key]):
            raise SessionSnapshotError("invalid_snapshot_policy_rejections")
    for key in ("risky_at", "_risky_at"):
        if key in snapshot.payload:
            stamps = snapshot.payload[key]
            if not isinstance(stamps, list | tuple) or any(
                not isinstance(stamp, datetime) for stamp in stamps
            ):
                raise SessionSnapshotError("invalid_snapshot_risk_timestamps")
    flight = snapshot.payload.get("in_flight", snapshot.payload.get("_in_flight"))
    if flight is not None:
        if not isinstance(flight, InFlightNode) or not isinstance(plan, Plan):
            raise SessionSnapshotError("invalid_snapshot_in_flight")
        node = plan.node_map().get(flight.node_id)
        if node is None or node.action != flight.action:
            raise SessionSnapshotError("snapshot_in_flight_node_mismatch")
        frozen_hash = _plan_hash(plan)
        request_key = f"{snapshot.execution_id}:{frozen_hash}:{flight.node_id}"
        call = call_for_node(node, snapshot.user_id)
        if (flight.request_key != request_key or flight.manifest_hash != manifest_hash(call)
                or flight.business_key != business_key_for(call, request_key, snapshot.user_id)):
            raise SessionSnapshotError("snapshot_in_flight_binding_mismatch")
    if isinstance(plan, Plan):
        node_map = plan.node_map()
        for key in (
            "completed_node_ids", "_completed_node_ids",
            "pending_node_ids", "_pending_node_ids",
        ):
            items = snapshot.payload.get(key, ())
            if not isinstance(items, tuple | list) or any(
                not isinstance(item, str) for item in items
            ):
                raise SessionSnapshotError("invalid_snapshot_node_set")
            if len(set(items)) != len(items) or not set(items) <= node_map.keys():
                raise SessionSnapshotError("invalid_snapshot_node_set")
        completed = snapshot.payload.get(
            "completed_node_ids", snapshot.payload.get("_completed_node_ids", ())
        )
        pending_ids = snapshot.payload.get(
            "pending_node_ids", snapshot.payload.get("_pending_node_ids", ())
        )
        assert isinstance(completed, tuple | list)
        assert isinstance(pending_ids, tuple | list)
        if set(completed) & set(pending_ids):
            raise SessionSnapshotError("snapshot_node_sets_overlap")
        if any(not set(node_map[item].depends_on) <= set(completed) for item in completed):
            raise SessionSnapshotError("snapshot_completed_dependency_gap")


def encode_snapshot(snapshot: SessionSnapshot) -> str:
    try:
        _validate_payload(snapshot)
        return json.dumps({
            "version": snapshot.version, "user_id": snapshot.user_id,
            "session_id": snapshot.session_id, "revision": snapshot.revision,
            "execution_id": snapshot.execution_id, "state": snapshot.state,
            "payload": _encode(snapshot.payload),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except SessionSnapshotError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise SessionSnapshotError(f"snapshot_encode_failed:{error}") from error


def decode_snapshot(text: str, *, user_id: str, session_id: str) -> SessionSnapshot:
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {
            "version", "user_id", "session_id", "revision", "execution_id", "state", "payload",
        }:
            raise SessionSnapshotError("invalid_snapshot_envelope")
        if data["user_id"] != user_id or data["session_id"] != session_id:
            raise SessionSnapshotError("snapshot_identity_mismatch")
        payload = _decode(data["payload"])
        if not isinstance(payload, dict):
            raise SessionSnapshotError("invalid_snapshot_payload")
        snapshot = SessionSnapshot(data["version"], data["user_id"], data["session_id"],
                                   data["revision"], data["execution_id"], data["state"], payload)
        _validate_payload(snapshot)
        return snapshot
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise SessionSnapshotError(f"snapshot_decode_failed:{error}") from error


dumps = encode_snapshot
loads = decode_snapshot


def encode_value(value: object) -> object:
    """公开单值 codec，供测试和存储适配器复用。"""
    try:
        return _encode(value)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise SessionSnapshotError(f"snapshot_value_encode_failed:{error}") from error


def decode_value(value: object) -> object:
    """公开单值 decoder；未知标签和非法结构均 fail-closed。"""
    try:
        return _decode(value)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise SessionSnapshotError(f"snapshot_value_decode_failed:{error}") from error
