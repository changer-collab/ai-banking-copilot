from datetime import date

import pytest

from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import TemplateIntentModel
from ai_banking_copilot.planning.slots import SlotInputs, fill_control_slots

TODAY = date(2026, 10, 1)


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("查一下余额", "account.balance"),
        ("登记预约转账给李娜 500 元", "transfer.book"),
        ("登记定时转账给李娜 500 元", "transfer.book"),
        ("解冻之前冻结的资金", "funds.release"),
        ("解除冻结", "funds.release"),
    ],
)
def test_business_entrypoints(text: str, action: str) -> None:
    proposal = TemplateIntentModel().propose(text, {})
    assert [item.action for item in proposal.intents] == [action]


@pytest.mark.parametrize("text", ["查一下我朋友王伟的余额", "查询他人的余额", "王伟账户余额"])
def test_balance_template_does_not_replace_another_person_with_self(text: str) -> None:
    assert TemplateIntentModel().propose(text, {}).intents == ()


@pytest.mark.parametrize(
    ("slot", "raw", "expected"),
    [
        ("amount", "25 元", 2500),
        ("daily_limit", "2 万元", 2_000_000),
        ("amount_cents", "2500", 2500),
        ("amount_cents", "25 元", 2500),
    ],
)
def test_selected_money_fields_are_typed(slot: str, raw: str, expected: int) -> None:
    action = {
        "amount": "funds.freeze",
        "daily_limit": "card.set_limit",
        "amount_cents": "subscription.sign",
    }[slot]
    fill = fill_control_slots(
        action, SlotInputs(text="", today=TODAY, selected={slot: raw})
    )
    assert fill.slots[slot] == expected


def test_untrusted_selected_amount_does_not_gain_trust_during_conversion() -> None:
    fill = fill_control_slots(
        "funds.freeze",
        SlotInputs(
            text="冻结资金",
            today=TODAY,
            selected={"account": "acc_savings", "amount": Value("25 元", Trust.UNVERIFIED)},
        ),
    )
    assert "amount" not in fill.slots
    assert [question.slot for question in fill.questions] == ["amount"]


@pytest.mark.parametrize(
    ("action", "slot"), [("funds.freeze", "amount"), ("card.set_limit", "daily_limit")]
)
def test_selected_money_without_a_unit_is_not_guessed(action: str, slot: str) -> None:
    fill = fill_control_slots(
        action, SlotInputs(text="", today=TODAY, selected={slot: "500"})
    )
    assert slot not in fill.slots
    assert any(question.slot == slot for question in fill.questions)


@pytest.mark.parametrize(("text", "period"), [("每月", "monthly"), ("每年", "yearly")])
def test_signing_amount_period_and_date_use_deterministic_parsers(
    text: str, period: str
) -> None:
    fill = fill_control_slots(
        "subscription.sign",
        SlotInputs(
            text=f"签约会员 {text} 25 元，2026-11-01 扣款",
            today=TODAY,
            selected={"user_id": DEMO_USER_ID, "merchant": "视频会员"},
        ),
    )
    assert fill.complete
    assert fill.slots["amount_cents"] == 2500
    assert fill.slots["period"] == period
    assert fill.slots["next_charge_on"] == "2026-11-01"


@pytest.mark.parametrize("raw", ["not-a-date", "2026-02-30"])
def test_invalid_charge_date_remains_a_question(raw: str) -> None:
    fill = fill_control_slots(
        "subscription.sign", SlotInputs(text="签约", today=TODAY, selected={"next_charge_on": raw})
    )
    assert "next_charge_on" not in fill.slots
    assert any(question.slot == "next_charge_on" for question in fill.questions)


def test_selected_period_and_date_are_normalized() -> None:
    fill = fill_control_slots(
        "subscription.sign",
        SlotInputs(
            text="签约",
            today=TODAY,
            selected={"period": "每月", "next_charge_on": date(2026, 11, 1)},
        ),
    )
    assert fill.slots["period"] == "monthly"
    assert fill.slots["next_charge_on"] == "2026-11-01"
