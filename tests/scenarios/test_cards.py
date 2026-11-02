import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pdp import evaluate
from ai_banking_copilot.scenarios.base import ScenarioRefused
from ai_banking_copilot.scenarios.cards import CardsHandler, loss_report_request


def _card_intent(action: str, **overrides: object) -> Intent:
    slots: dict[str, object] = {"card": Value("card_debit", Trust.TRUSTED), "span": action}
    slots.update(overrides)
    return Intent(action=action, slots=slots, confidence=0.9, evidence=action)


def test_report_lost_is_irreversible_and_at_least_red(gateway: FactGateway) -> None:
    plan = CardsHandler().build([_card_intent("card.report_lost")], gateway)
    assert plan.nodes[0].action == "card.report_lost"
    assert plan.nodes[0].params["card_id"] == "card_debit"
    assert evaluate(loss_report_request(DEMO_USER_ID)).level >= Level.RED


def test_lock_builds_node_and_unlock_of_a_healthy_card_builds(
    gateway: FactGateway,
) -> None:
    """口径 2026-11-02 变更：只有已挂失（lost）的卡不可解锁，健康卡可正常出计划。"""
    plan = CardsHandler().build([_card_intent("card.lock")], gateway)
    assert [node.action for node in plan.nodes] == ["card.lock"]
    unlock = CardsHandler().build([_card_intent("card.unlock")], gateway)
    assert [node.action for node in unlock.nodes] == ["card.unlock"]


def test_list_plan_is_pure_query(gateway: FactGateway) -> None:
    intent = Intent(action="card.list", slots={}, confidence=0.9, evidence="我的卡")
    plan = CardsHandler().build([intent], gateway)
    assert plan.nodes[0].action == "card.list"


def test_set_limit_requires_positive_limit(gateway: FactGateway) -> None:
    intent = _card_intent(
        "card.set_limit",
        daily_limit=Value(Money(0), Trust.TRUSTED),
        limit_kind=Value("purchase", Trust.TRUSTED),
    )
    with pytest.raises(ScenarioRefused):
        CardsHandler().build([intent], gateway)


def test_set_limit_builds_node_and_rejects_unknown_card(gateway: FactGateway) -> None:
    plan = CardsHandler().build(
        [
            _card_intent(
                "card.set_limit",
                daily_limit=Value(Money(500000), Trust.TRUSTED),
                limit_kind=Value("purchase", Trust.TRUSTED),
            )
        ],
        gateway,
    )
    assert plan.nodes[0].params["daily_limit_cents"] == 500000
    unknown = _card_intent("card.lock", card=Value("card_nope", Trust.TRUSTED))
    with pytest.raises(ScenarioRefused):
        CardsHandler().build([unknown], gateway)
