from datetime import date

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.contracts.plan import AnchorSource
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import ScenarioStatus, plan_net_effect
from ai_banking_copilot.scenarios.registry import run_scenario, select_handler


def _run(intents: list[Intent], gateway: FactGateway):
    return run_scenario(select_handler(intents), intents, gateway)


def _transfer(payee: str, cents: int) -> Intent:
    return Intent(
        action="transfer.create",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "payee": Value(payee, Trust.TRUSTED),
            "amount": Value(Money(cents), Trust.TRUSTED),
            "when": Value(date(2026, 10, 1), Trust.TRUSTED),
            "span": "转账 500 元",
        },
        confidence=0.9,
        evidence="转账",
    )


def test_six_scenarios_are_selectable_and_plan(gateway: FactGateway) -> None:
    scenarios = {
        "transfer": [_transfer("p_lina", 50000)],
        "bills": [Intent(action="bills.monthly", slots={}, confidence=0.9, evidence="账单")],
        "wealth": [Intent(action="wealth.list", slots={}, confidence=0.9, evidence="理财")],
        "cards": [
            Intent(
                action="card.list",
                slots={},
                confidence=0.9,
                evidence="卡片",
            )
        ],
        "subscriptions": [
            Intent(action="subscription.list", slots={}, confidence=0.9, evidence="订阅")
        ],
        "cross": [
            Intent(
                action="funds.freeze",
                slots={
                    "account": Value("acc_savings", Trust.TRUSTED),
                    "amount": Value(Money(200000), Trust.TRUSTED),
                    "grant": "explicit",
                    "span": "锁 2000 元",
                },
                confidence=0.9,
                evidence="锁资金",
            ),
            Intent(
                action="transfer.create",
                slots={
                    "account": Value("acc_savings", Trust.TRUSTED),
                    "payee": Value("p_lina", Trust.TRUSTED),
                    "amount": Value(Money(30000), Trust.TRUSTED),
                    "when": Value(date(2026, 10, 1), Trust.TRUSTED),
                    "grant": "user_approved",
                    "span": "订花 300 元",
                },
                confidence=0.9,
                evidence="订花",
            ),
        ],
    }
    for name, intents in scenarios.items():
        outcome = _run(intents, gateway)
        assert select_handler(intents).name == name
        assert outcome.status is ScenarioStatus.PLANNED
        assert outcome.plan is not None
        assert outcome.decision is not None


def test_transfer_multi_candidate_never_yields_a_level(gateway: FactGateway) -> None:
    outcome = _run([_transfer("王伟", 50000)], gateway)
    assert outcome.status is ScenarioStatus.NEEDS_CLARIFICATION
    assert outcome.plan is None
    assert outcome.decision is None


def test_bills_scenario_is_green_pure_query(gateway: FactGateway) -> None:
    intent = Intent(action="bills.monthly", slots={}, confidence=0.9, evidence="账单")
    outcome = _run([intent], gateway)
    assert outcome.decision is not None
    assert outcome.decision.level is Level.GREEN


def test_cards_loss_report_is_at_least_red(gateway: FactGateway) -> None:
    intent = Intent(
        action="card.report_lost",
        slots={"card": Value("card_debit", Trust.TRUSTED), "span": "挂失"},
        confidence=0.9,
        evidence="挂失",
    )
    outcome = _run([intent], gateway)
    assert outcome.status is ScenarioStatus.PLANNED
    assert outcome.decision is not None
    assert outcome.decision.level >= Level.RED


def test_subscription_cancel_is_negative_future_outflow(gateway: FactGateway) -> None:
    intent = Intent(
        action="subscription.cancel",
        slots={"subscription": Value("sub_video", Trust.TRUSTED), "span": "取消订阅"},
        confidence=0.9,
        evidence="取消订阅",
    )
    outcome = _run([intent], gateway)
    assert outcome.plan is not None
    assert plan_net_effect(outcome.plan, gateway).amount == Money(-2500)


def test_cross_scenario_dag_and_net_effect(gateway: FactGateway) -> None:
    freeze = Intent(
        action="funds.freeze",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "amount": Value(Money(200000), Trust.TRUSTED),
            "grant": "explicit",
            "span": "锁 2000 元",
        },
        confidence=0.9,
        evidence="锁资金",
    )
    flower = Intent(
        action="transfer.create",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "payee": Value("p_lina", Trust.TRUSTED),
            "amount": Value(Money(30000), Trust.TRUSTED),
            "when": Value(date(2026, 10, 1), Trust.TRUSTED),
            "grant": "user_approved",
            "span": "订花 300 元",
        },
        confidence=0.9,
        evidence="订花",
    )
    outcome = _run([freeze, flower], gateway)
    assert outcome.plan is not None
    assert [node.node_id for node in outcome.plan.nodes] == ["n1", "n2"]
    assert outcome.plan.nodes[1].depends_on == ("n1",)
    assert outcome.plan.nodes[1].intent is not None
    assert outcome.plan.nodes[1].intent.source is AnchorSource.USER_APPROVED
    assert plan_net_effect(outcome.plan, gateway).amount == Money(30000)
