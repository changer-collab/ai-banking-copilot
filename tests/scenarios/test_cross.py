from datetime import date

import pytest

from ai_banking_copilot.contracts.plan import AnchorSource
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import ScenarioRefused, plan_net_effect
from ai_banking_copilot.scenarios.cross import CrossHandler, intents_domains


def _freeze_intent() -> Intent:
    return Intent(
        action="funds.freeze",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "amount": Value(Money(200000), Trust.TRUSTED),
            "grant": "explicit",
            "span": "锁 2000 元给生日",
        },
        confidence=0.9,
        evidence="锁资金",
    )


def _purchase_intent(payee_id: str, cents: int, grant: str = "user_approved") -> Intent:
    return Intent(
        action="transfer.create",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "payee": Value(payee_id, Trust.TRUSTED),
            "amount": Value(Money(cents), Trust.TRUSTED),
            "when": Value(date(2026, 10, 1), Trust.TRUSTED),
            "grant": grant,
            "span": f"给 {payee_id} 转 {cents} 分",
        },
        confidence=0.9,
        evidence="订花 / 订蛋糕",
    )


def _birthday_intents() -> list[Intent]:
    return [
        _freeze_intent(),
        _purchase_intent("p_lina", 30000),
        _purchase_intent("p_zhangqiang", 20000),
    ]


def test_cross_matches_two_domains() -> None:
    handler = CrossHandler()
    assert handler.matches([_freeze_intent(), _purchase_intent("p_lina", 30000)]) is True
    assert handler.matches([_purchase_intent("p_lina", 30000)]) is False
    assert intents_domains([_freeze_intent(), _purchase_intent("p_lina", 30000)]) == {
        "funds",
        "transfer",
    }


def test_cross_requires_interaction_grant(gateway: FactGateway) -> None:
    ungranted = _purchase_intent("p_lina", 30000, grant="none")
    with pytest.raises(ScenarioRefused):
        CrossHandler().build([_freeze_intent(), ungranted], gateway)


def test_cross_builds_multi_step_dag(gateway: FactGateway) -> None:
    plan = CrossHandler().build(_birthday_intents(), gateway)
    assert [node.node_id for node in plan.nodes] == ["n1", "n2", "n3"]
    assert [node.action for node in plan.nodes] == [
        "funds.freeze",
        "transfer.create",
        "transfer.create",
    ]
    assert plan.nodes[1].depends_on == ("n1",)
    assert plan.nodes[2].depends_on == ("n1",)
    assert plan.nodes[0].intent is not None
    assert plan.nodes[0].intent.source is AnchorSource.EXPLICIT
    assert plan.nodes[1].intent is not None
    assert plan.nodes[1].intent.source is AnchorSource.USER_APPROVED


def test_cross_plan_net_effect_is_sum(gateway: FactGateway) -> None:
    plan = CrossHandler().build(_birthday_intents(), gateway)
    net = plan_net_effect(plan, gateway)
    assert net.amount == Money(250000)
    assert net.irreversibles == ()
