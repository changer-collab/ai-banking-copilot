from dataclasses import dataclass

import pytest

from ai_banking_copilot.contracts.authorization import Level, ReasonCode
from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    PlanValidationError,
)
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.policy.pdp import (
    DAILY_LIMIT_YUAN,
    FLAG_DEVICE_CHANGE,
    FLAG_FIRST_LARGE_TO_OBJECT,
    FLAG_INJECTION_DETECTED,
    NET_EFFECT_THRESHOLD_YUAN,
    AmbiguousObjectError,
    ObjectState,
    PolicyRequest,
    evaluate,
    evaluate_plan,
    reject_ambiguous,
)


def _request(
    *,
    amount: int = 0,
    daily: int = 0,
    state: ObjectState = ObjectState.SELF,
    pure: bool = False,
    irreversible: bool = False,
    flags: frozenset[str] = frozenset(),
) -> PolicyRequest:
    return PolicyRequest(
        user_id=DEMO_USER_ID,
        action="transfer.create",
        amount=Money(amount),
        daily_accumulated=Money(daily),
        object_state=state,
        is_pure_query=pure,
        irreversible=irreversible,
        context_flags=flags,
    )


def test_official_thresholds_are_anchored() -> None:
    assert DAILY_LIMIT_YUAN == 1000
    assert NET_EFFECT_THRESHOLD_YUAN == 1000


def test_pure_query_is_green() -> None:
    decision = evaluate(_request(pure=True))
    assert decision.level is Level.GREEN
    assert decision.reasons == (ReasonCode.QUERY_ONLY,)


def test_daily_at_limit_is_yellow() -> None:
    decision = evaluate(_request(amount=100000, daily=100000))
    assert decision.level is Level.YELLOW
    assert ReasonCode.STATE_CHANGE_SMALL in decision.reasons


def test_daily_over_limit_is_red() -> None:
    decision = evaluate(_request(amount=100001, daily=100001))
    assert decision.level is Level.RED
    assert ReasonCode.AMOUNT_OVER_DAILY_LIMIT in decision.reasons


def test_non_monetary_request_is_not_banded_by_daily_accumulation() -> None:
    request = PolicyRequest(
        user_id=DEMO_USER_ID,
        action="card.lock",
        amount=Money(0),
        daily_accumulated=Money(200000),  # 当日已流出 ¥2000
        object_state=ObjectState.SELF,
        is_pure_query=False,
        irreversible=False,
        amount_applies=False,
    )
    decision = evaluate(request)
    assert decision.level is Level.YELLOW
    assert decision.reasons == (ReasonCode.STATE_CHANGE_SMALL,)


def test_amount_applies_true_still_bands_even_with_zero_node_amount() -> None:
    request = PolicyRequest(
        user_id=DEMO_USER_ID,
        action="transfer.create",
        amount=Money(0),
        daily_accumulated=Money(200000),
        object_state=ObjectState.SELF,
        is_pure_query=False,
        irreversible=False,
        amount_applies=True,
    )
    assert evaluate(request).level is Level.RED


def test_blocked_is_deny_regardless_of_amount_and_purity() -> None:
    for request in (
        _request(state=ObjectState.BLOCKED, pure=True),
        _request(state=ObjectState.BLOCKED, amount=100, daily=100),
        _request(state=ObjectState.BLOCKED, amount=99999999, daily=99999999),
    ):
        decision = evaluate(request)
        assert decision.level is Level.DENY
        assert decision.reasons == (ReasonCode.OBJECT_BLOCKED,)


def test_unknown_object_requires_identity_confirmation() -> None:
    decision = evaluate(_request(state=ObjectState.UNKNOWN))
    assert decision.level is Level.YELLOW
    assert ReasonCode.MANUAL_REVIEW in decision.reasons


def test_known_unverified_object_requires_identity_confirmation() -> None:
    assert evaluate(_request(state=ObjectState.KNOWN_UNVERIFIED)).level is Level.YELLOW


def test_verified_object_does_not_lower_amount_band() -> None:
    decision = evaluate(
        _request(amount=150000, daily=150000, state=ObjectState.VERIFIED_SAVED)
    )
    assert decision.level is Level.RED


def test_irreversible_action_is_at_least_red() -> None:
    decision = evaluate(_request(irreversible=True))
    assert decision.level is Level.RED
    assert ReasonCode.IRREVERSIBLE in decision.reasons


def test_fail_safe_conflict_takes_strictest_level() -> None:
    decision = evaluate(_request(pure=True, irreversible=True))
    assert decision.level is Level.RED
    assert ReasonCode.IRREVERSIBLE in decision.reasons


def test_first_large_transfer_to_object_forces_red() -> None:
    flags = frozenset({FLAG_FIRST_LARGE_TO_OBJECT})
    assert evaluate(_request(amount=500, daily=500, flags=flags)).level is Level.RED


def test_injection_flag_escalates_to_manual() -> None:
    flags = frozenset({FLAG_INJECTION_DETECTED})
    assert evaluate(_request(amount=100, daily=100, flags=flags)).level is Level.MANUAL


def test_context_anomaly_raises_green_query_to_yellow() -> None:
    """上下文异常是**只升档**的 modifier：连纯查询也被抬到 ≥ 黄（fail-safe）。"""
    flags = frozenset({FLAG_DEVICE_CHANGE})
    assert evaluate(_request(pure=True, flags=flags)).level is Level.YELLOW


def test_multi_candidate_refuses_to_assign_level() -> None:
    reject_ambiguous(1)
    with pytest.raises(AmbiguousObjectError, match="澄清"):
        reject_ambiguous(2)


def test_request_rejects_amount_above_daily() -> None:
    with pytest.raises(ValueError, match="daily_accumulated"):
        _request(amount=200, daily=100)


def test_request_rejects_unknown_context_flag() -> None:
    with pytest.raises(ValueError, match="未知上下文标记"):
        _request(flags=frozenset({"made_up_flag"}))


def test_request_rejects_bad_object_state_type() -> None:
    with pytest.raises(TypeError, match="object_state"):
        PolicyRequest(
            user_id=DEMO_USER_ID,
            action="transfer.create",
            amount=Money(0),
            daily_accumulated=Money(0),
            object_state="self",  # type: ignore[arg-type]
            is_pure_query=False,
            irreversible=False,
        )


@dataclass(frozen=True)
class _Net:
    """`planning.NetEffect` 的最小结构替身（证明 03 不依赖 04）。"""

    amount: Money
    irreversibles: tuple[str, ...] = ()


def _node(node_id: str, amount_cents: int) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action="transfer.create",
        params={"amount_cents": amount_cents},
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
    )


def _plan(*nodes: PlanNode) -> Plan:
    return Plan(plan_id="p1", nodes=nodes)


def test_plan_net_effect_below_threshold_is_yellow() -> None:
    decision = evaluate_plan(_plan(_node("n1", 99999)), _Net(amount=Money(99999)))
    assert decision.level is Level.YELLOW


def test_plan_net_effect_at_threshold_is_red() -> None:
    decision = evaluate_plan(_plan(_node("n1", 100000)), _Net(amount=Money(100000)))
    assert decision.level is Level.RED
    assert ReasonCode.AMOUNT_OVER_DAILY_LIMIT in decision.reasons


def test_plan_zero_net_effect_is_at_most_yellow() -> None:
    decision = evaluate_plan(_plan(_node("n1", 0)), _Net(amount=Money(0)))
    assert decision.level is Level.YELLOW


def test_plan_with_irreversible_node_is_red_and_unions_reasons() -> None:
    decision = evaluate_plan(
        _plan(_node("n1", 100000)), _Net(amount=Money(100000), irreversibles=("n1",))
    )
    assert decision.level is Level.RED
    assert set(decision.reasons) == {
        ReasonCode.AMOUNT_OVER_DAILY_LIMIT,
        ReasonCode.IRREVERSIBLE,
    }


def test_plan_without_anchor_is_rejected() -> None:
    orphan = PlanNode(node_id="n1", action="transfer.create", params={"amount_cents": 1})
    with pytest.raises(PlanValidationError):
        evaluate_plan(_plan(orphan), _Net(amount=Money(0)))
