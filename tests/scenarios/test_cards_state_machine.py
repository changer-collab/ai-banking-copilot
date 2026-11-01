"""卡片状态机：临时冻结 / 正式挂失 / 补卡 / 分卡限额的入口校验。"""

from __future__ import annotations

import pytest

from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import ClarificationRequired, ScenarioRefused
from ai_banking_copilot.scenarios.cards import CardsHandler


def _intent(action: str, card: str = "card_debit", **slots: object) -> Intent:
    values: dict[str, object] = {
        "card": Value(card, Trust.TRUSTED),
        "span": action,
    }
    values.update(slots)
    return Intent(action=action, slots=values, confidence=0.9, evidence=action)


def test_temp_lock_defaults_to_seven_days(gateway: FactGateway) -> None:
    plan = CardsHandler().build([_intent("card.lock")], gateway)
    assert plan.nodes[0].action == "card.lock"
    assert plan.nodes[0].params["duration_days"] == 7


def test_temp_lock_rejects_unsupported_duration(gateway: FactGateway) -> None:
    with pytest.raises(ScenarioRefused, match="5 / 7 / 10 / 15"):
        CardsHandler().build(
            [_intent("card.lock", lock_days=Value(9, Trust.TRUSTED))], gateway
        )


def test_report_lost_defaults_to_formal(gateway: FactGateway) -> None:
    plan = CardsHandler().build([_intent("card.report_lost")], gateway)
    assert plan.nodes[0].action == "card.report_lost"
    assert plan.nodes[0].params["loss_type"] == "formal"


def test_report_lost_temporary_builds_a_freeze_node(gateway: FactGateway) -> None:
    plan = CardsHandler().build(
        [_intent("card.report_lost", loss_type=Value("temporary", Trust.TRUSTED))],
        gateway,
    )
    assert plan.nodes[0].action == "card.lock"
    assert plan.nodes[0].params["duration_days"] == 7


def test_unlock_requires_a_locked_card(gateway: FactGateway) -> None:
    with pytest.raises(ScenarioRefused, match="临时冻结"):
        CardsHandler().build([_intent("card.unlock")], gateway)


def test_replace_requires_address_and_mail_method(gateway: FactGateway) -> None:
    with pytest.raises(ClarificationRequired):
        CardsHandler().build([_intent("card.replace")], gateway)


def test_credit_limit_is_credit_only(gateway: FactGateway) -> None:
    with pytest.raises(ScenarioRefused, match="信用卡"):
        CardsHandler().build(
            [
                _intent(
                    "card.adjust_credit_limit",
                    credit_mode=Value("temporary", Trust.TRUSTED),
                    amount=Value(Money(100_000), Trust.TRUSTED),
                )
            ],
            gateway,
        )


def test_debit_channel_limit_is_debit_only(gateway: FactGateway) -> None:
    with pytest.raises(ScenarioRefused, match="信用卡没有日限额"):
        CardsHandler().build(
            [
                _intent(
                    "card.set_limit",
                    card="card_credit",
                    limit_kind=Value("purchase", Trust.TRUSTED),
                    daily_limit=Value(Money(100_000), Trust.TRUSTED),
                )
            ],
            gateway,
        )
