from datetime import date

from ai_banking_copilot.contracts.taint import SlotKind, Trust, Value
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import ACTION_KINDS
from ai_banking_copilot.planning.slots import (
    _PROMPTS,
    REQUIRED_SLOTS,
    SLOT_PARAMS,
    SlotInputs,
    fill_control_slots,
)
from ai_banking_copilot.tools.catalog import build_default_registry

TODAY = date(2026, 10, 1)


def _transfer_inputs(
    text: str, payees: tuple[Payee, ...], account: object = "acc_savings"
) -> SlotInputs:
    return SlotInputs(
        text=text,
        today=TODAY,
        payees=payees,
        selected={"user_id": DEMO_USER_ID, "account": account},
    )


def test_transfer_slots_are_filled_from_deterministic_resolvers(payees: tuple[Payee, ...]) -> None:
    fill = fill_control_slots("transfer.create", _transfer_inputs("转给李娜 500 元", payees))
    assert fill.complete is True
    assert fill.negated is False
    assert fill.slots == {
        "user_id": DEMO_USER_ID,
        "account": "acc_savings",
        "payee": "p_lina",
        "amount": 50000,
        "when": "2026-10-01",
    }


def test_amount_without_unit_is_not_guessed(payees: tuple[Payee, ...]) -> None:
    fill = fill_control_slots("transfer.create", _transfer_inputs("转给李娜 500", payees))
    assert fill.complete is False
    assert [question.slot for question in fill.questions] == ["amount"]
    assert "payee" in fill.slots


def test_daily_limit_is_parsed_from_text_with_a_unit() -> None:
    """`daily_limit` 有可控 NL（P2-4 残留）：口述额度（**必须带单位**）会被采纳为整数分。"""
    fill = fill_control_slots(
        "card.set_limit",
        SlotInputs(
            text="把这张卡的额度调到 2 万元",
            today=TODAY,
            payees=(),
            selected={"card": "card_debit"},
        ),
    )
    assert fill.slots["daily_limit"] == 2_000_000
    assert fill.complete is True


def test_daily_limit_without_a_unit_is_not_guessed() -> None:
    fill = fill_control_slots(
        "card.set_limit",
        SlotInputs(
            text="把这张卡的额度调到 2000", today=TODAY, payees=(), selected={"card": "card_debit"}
        ),
    )
    assert "daily_limit" not in fill.slots
    assert [question.slot for question in fill.questions] == ["daily_limit"]


def test_selected_daily_limit_still_wins_over_the_text() -> None:
    """受信任调用方显式选定**优先**于文本解析（`_resolve_slot` 首段），演示链行为不变。"""
    fill = fill_control_slots(
        "card.apply_virtual",
        SlotInputs(
            text="申请一张虚拟卡，额度 5 万元",
            today=TODAY,
            payees=(),
            selected={"user_id": DEMO_USER_ID, "daily_limit": 100_000},
        ),
    )
    assert fill.slots["daily_limit"] == 100_000


def test_multiple_candidates_force_clarification_without_a_level(
    payees: tuple[Payee, ...],
) -> None:
    fill = fill_control_slots("transfer.create", _transfer_inputs("转给王伟 500 元", payees))
    assert fill.complete is False
    payee_question = next(item for item in fill.questions if item.slot == "payee")
    assert len(payee_question.candidates) == 2
    assert all("王伟" in label for label in payee_question.candidates)
    assert "payee" not in fill.slots


def test_unverified_value_never_enters_a_control_slot(payees: tuple[Payee, ...]) -> None:
    unverified = Value("acc_savings", Trust.UNVERIFIED)
    fill = fill_control_slots(
        "transfer.create", _transfer_inputs("转给李娜 500 元", payees, unverified)
    )
    assert "account" not in fill.slots
    assert any("rejected unverified:account" in note for note in fill.trace)
    assert any(question.slot == "account" for question in fill.questions)


def test_trusted_wrapped_value_is_accepted(payees: tuple[Payee, ...]) -> None:
    trusted = Value("acc_savings", Trust.TRUSTED)
    fill = fill_control_slots(
        "transfer.create", _transfer_inputs("转给李娜 500 元", payees, trusted)
    )
    assert fill.slots["account"] == "acc_savings"


def test_negation_blocks_slot_filling(payees: tuple[Payee, ...]) -> None:
    fill = fill_control_slots("transfer.create", _transfer_inputs("不要转给李娜 500 元", payees))
    assert fill.negated is True
    assert fill.complete is False
    assert fill.slots == {}


def test_bills_monthly_requires_a_trusted_user_id() -> None:
    """纯查询动作同样需要 `user_id`（02 目录声明；由受信任调用方经 `selected` 填入）。"""
    without_user = fill_control_slots(
        "bills.monthly", SlotInputs(text="查一下上月账单", today=TODAY)
    )
    assert without_user.complete is False
    assert [question.slot for question in without_user.questions] == ["user_id"]
    with_user = fill_control_slots(
        "bills.monthly",
        SlotInputs(text="查一下上月账单", today=TODAY, selected={"user_id": DEMO_USER_ID}),
    )
    assert with_user.complete is True
    assert with_user.slots == {"user_id": DEMO_USER_ID}


def test_required_slots_cover_every_canonical_action() -> None:
    assert set(REQUIRED_SLOTS) == set(ACTION_KINDS)


def test_slot_params_cover_every_referenced_slot() -> None:
    referenced = {slot for slots in REQUIRED_SLOTS.values() for slot in slots}
    assert referenced <= set(SLOT_PARAMS)


def test_slot_params_are_derived_from_the_catalog() -> None:
    """`SLOT_PARAMS` 是 02 目录的**投影**（不再是手写平行表）：槽位名必须映射到自己的参数名。"""
    registry = build_default_registry()
    for action in registry.actions():
        for param in registry.get(action).params:
            if param.slot_name is None:
                continue
            assert SLOT_PARAMS[param.slot_name] == param.name


def test_slot_params_keep_the_known_wire_vocabulary() -> None:
    """**下限式**锁：两套词汇的既有对应不得被悄悄改名（新增槽位不受影响，不算冲突点）。"""
    assert {
        "account": "account_id",
        "payee": "payee_id",
        "amount": "amount_cents",
        "when": "booked_on",
        "product": "product_id",
        "subscription": "subscription_id",
        "card": "card_id",
        "hold": "hold_id",
        "daily_limit": "daily_limit_cents",
        "user_id": "user_id",
        "merchant": "merchant",
        "amount_cents": "amount_cents",
        "period": "period",
        "next_charge_on": "next_charge_on",
    }.items() <= SLOT_PARAMS.items()


def test_every_slot_param_has_a_clarification_prompt() -> None:
    """每个可补全槽位都必须有澄清话术，否则追问路径会 KeyError。"""
    assert set(SLOT_PARAMS) <= set(_PROMPTS)


def test_required_slots_follow_the_catalog_required_control_slots() -> None:
    """02 声明必填 user_id 的动作，REQUIRED_SLOTS 必须一致含 user_id（README 裁决 #16）。"""
    registry = build_default_registry()
    requires_user_id = {
        action
        for action in registry.actions()
        if any(
            param.name == "user_id" and param.slot is SlotKind.CONTROL and param.required
            for param in registry.get(action).params
        )
    }
    assert requires_user_id == {
        "bills.monthly",
        "bills.category_trend",
        "bills.yearly",
        "card.list",
        "subscription.list",
        "subscription.cancel",
        "card.apply_virtual",
        "transfer.book",
        "transfer.create",
        "transfer.request_collection",
        "transfer.schedule",
        "wealth.subscribe",
        "wealth.redeem",
        "subscription.sign",
        "merchant.order",
        "password.change",
    }
    for action in requires_user_id:
        assert "user_id" in REQUIRED_SLOTS[action]
    assert REQUIRED_SLOTS["subscription.sign"] == (
        "user_id",
        "merchant",
        "amount_cents",
        "period",
        "next_charge_on",
    )
    assert "subscription_id" not in REQUIRED_SLOTS["subscription.sign"]


def test_signing_slots_are_filled_from_trusted_selection() -> None:
    """代扣签约的控制槽由受信任调用方经 `selected` 填入（对象是商户，不是订阅 id）。"""
    fill = fill_control_slots(
        "subscription.sign",
        SlotInputs(
            text="签约代扣",
            today=TODAY,
            selected={
                "user_id": DEMO_USER_ID,
                "merchant": "merchant_video",
                "amount_cents": 2500,
                "period": "monthly",
                "next_charge_on": "2026-11-01",
            },
        ),
    )
    assert fill.complete is True
    assert fill.slots == {
        "user_id": DEMO_USER_ID,
        "merchant": "merchant_video",
        "amount_cents": 2500,
        "period": "monthly",
        "next_charge_on": "2026-11-01",
    }


def test_cancel_subscription_is_not_blocked_by_its_own_trigger_word() -> None:
    fill = fill_control_slots(
        "subscription.cancel",
        SlotInputs(text="取消订阅", today=TODAY, selected={"subscription": "sub_video"}),
    )
    assert fill.negated is False
    assert fill.slots["subscription"] == "sub_video"


def test_refusing_to_cancel_is_still_blocked() -> None:
    fill = fill_control_slots(
        "subscription.cancel",
        SlotInputs(text="不要取消订阅", today=TODAY, selected={"subscription": "sub_video"}),
    )
    assert fill.negated is True
    assert fill.slots == {}
