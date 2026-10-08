from collections.abc import Callable
from datetime import date, datetime

import pytest

from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.saga import ReversalState, ReversalWindow, Saga
from ai_banking_copilot.scenarios.base import ScenarioRefused, plan_is_pure_query, plan_net_effect
from ai_banking_copilot.scenarios.subscriptions import (
    SubscriptionsHandler,
    describe_detected_charges,
    describe_upcoming_charges,
    detected_charges,
    future_outflow_effect,
    resolve_cancel_race,
    upcoming_charges,
)

NOW = datetime(2026, 10, 1, 9, 0, 0)


def _intent(action: str, **overrides: object) -> Intent:
    slots: dict[str, object] = {"span": action}
    slots.update(overrides)
    return Intent(action=action, slots=slots, confidence=0.9, evidence=action)


def _recorder(calls: list[str], tag: str) -> Callable[[], str]:
    def record() -> str:
        calls.append(tag)
        return tag

    return record


def test_cancel_plan_has_negative_future_outflow(gateway: FactGateway) -> None:
    intent = _intent("subscription.cancel", subscription=Value("sub_video", Trust.TRUSTED))
    plan = SubscriptionsHandler().build([intent], gateway)
    assert plan.nodes[0].action == "subscription.cancel"
    assert plan.nodes[0].params["subscription_id"] == "sub_video"
    assert plan_net_effect(plan, gateway).amount == Money(-2500)


def test_future_outflow_effect_is_negative(gateway: FactGateway) -> None:
    subscription = gateway.subscription("sub_video").payload
    assert future_outflow_effect(subscription) == Money(-2500)


def test_list_is_pure_query(gateway: FactGateway) -> None:
    plan = SubscriptionsHandler().build([_intent("subscription.list")], gateway)
    assert plan_is_pure_query(plan) is True


def test_sign_action_uses_merchant_slots_not_subscription_id(gateway: FactGateway) -> None:
    """README 裁决 #17：代扣签约按 商户/金额/周期/下次扣款日 + user_id 装配（不涉及订阅 id）。"""
    intent = _intent(
        "subscription.sign",
        merchant=Value("merchant_video", Trust.TRUSTED),
        amount=Value(Money(2500), Trust.TRUSTED),
        period=Value("monthly", Trust.TRUSTED),
        next_charge_on=Value("2026-11-01", Trust.TRUSTED),
    )
    node = SubscriptionsHandler().build([intent], gateway).nodes[0]
    assert node.action == "subscription.sign"
    assert node.params == {
        "user_id": DEMO_USER_ID,
        "merchant": "merchant_video",
        "amount_cents": 2500,
        "period": "monthly",
        "next_charge_on": "2026-11-01",
    }
    assert "subscription_id" not in node.params


def test_sign_action_rejects_an_unknown_period(gateway: FactGateway) -> None:
    """代扣周期只支持 monthly / yearly，其余一律硬拒绝（不产出计划）。"""
    intent = _intent(
        "subscription.sign",
        merchant=Value("merchant_video", Trust.TRUSTED),
        amount=Value(Money(2500), Trust.TRUSTED),
        period=Value("weekly", Trust.TRUSTED),
        next_charge_on=Value("2026-11-01", Trust.TRUSTED),
    )
    with pytest.raises(ScenarioRefused):
        SubscriptionsHandler().build([intent], gateway)


def test_cancel_race_compensates_without_claiming_cancelled() -> None:
    calls: list[str] = []
    saga = Saga()
    saga.add("charge", _recorder(calls, "charge"), _recorder(calls, "refund"))
    saga.run()
    window = ReversalWindow.open("sub_video", now=NOW)
    outcome = resolve_cancel_race(window, saga, now=NOW, settled=True)
    assert outcome.state is ReversalState.COMPENSATING
    assert "已扣" in outcome.message
    assert "退回" in outcome.message
    assert "已取消" not in outcome.message
    assert outcome.compensated_steps == ("charge",)


def test_upcoming_charges_sorted_by_next_charge_on(gateway: FactGateway) -> None:
    charges = upcoming_charges(gateway, DEMO_USER_ID)
    assert len(charges) == 3
    assert [item.next_charge_on for item in charges] == sorted(
        item.next_charge_on for item in charges
    )
    assert charges[0].next_charge_on == date(2026, 10, 8)
    text = describe_upcoming_charges(charges)
    assert "共 3 项" in text
    assert "2026-10-08" in text


def test_detected_charges_are_merged_from_transactions(gateway: FactGateway) -> None:
    """自动识别订阅扣费：仅按**商户名精确匹配**归并流出流水，识别不到的不编造。"""
    charges = detected_charges(gateway, DEMO_USER_ID)
    assert [charge.merchant for charge in charges] == ["视频会员"]
    assert charges[0].subscription_id == "sub_video"
    assert charges[0].txn_count == 3
    assert charges[0].total.cents == 7500
    assert charges[0].last_booked_on == date(2026, 9, 18)
    assert "识别到 1 项订阅扣费" in describe_detected_charges(charges)


def test_detected_charges_are_empty_for_an_unknown_user(gateway: FactGateway) -> None:
    assert detected_charges(gateway, "user_nobody") == ()
