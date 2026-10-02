from datetime import date

from ai_banking_copilot.contracts.plan import cfhash, validate_plan
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import ScenarioStatus, plan_net_effect
from ai_banking_copilot.scenarios.registry import run_scenario, select_handler


def _transfer(payee: str, cents: int) -> Intent:
    return Intent(
        action="transfer.create",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "payee": Value(payee, Trust.TRUSTED),
            "amount": Value(Money(cents), Trust.TRUSTED),
            "when": Value(date(2026, 10, 1), Trust.TRUSTED),
            "span": "转账",
        },
        confidence=0.9,
        evidence="转账",
    )


def _cancel_subscription() -> Intent:
    return Intent(
        action="subscription.cancel",
        slots={"subscription": Value("sub_video", Trust.TRUSTED), "span": "取消订阅"},
        confidence=0.9,
        evidence="取消订阅",
    )


def _redeem(cents: int) -> Intent:
    return Intent(
        action="wealth.redeem",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "product": Value("wp_bond", Trust.TRUSTED),
            "amount": Value(Money(cents), Trust.TRUSTED),
            "span": "赎回",
        },
        confidence=0.9,
        evidence="赎回",
    )


def _freeze(cents: int, grant: str = "explicit") -> Intent:
    return Intent(
        action="funds.freeze",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "amount": Value(Money(cents), Trust.TRUSTED),
            "grant": grant,
            "span": "冻结",
        },
        confidence=0.9,
        evidence="冻结",
    )


def test_cross_domain_intents_assemble_into_a_single_plan(gateway: FactGateway) -> None:
    intents = [_transfer("p_lina", 50000), _cancel_subscription()]
    assert select_handler(intents).name == "cross"
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.status is ScenarioStatus.PLANNED
    assert outcome.plan is not None
    validate_plan(outcome.plan)
    assert [node.action for node in outcome.plan.nodes] == [
        "transfer.create",
        "subscription.cancel",
    ]
    assert outcome.decision is not None


def test_same_resource_nodes_are_serialised(gateway: FactGateway) -> None:
    intents = [_transfer("p_lina", 50000), _redeem(200000)]
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.plan is not None
    first, second = outcome.plan.nodes
    assert first.action == "transfer.create"
    assert second.action == "wealth.redeem"
    assert second.depends_on == (first.node_id,)


def test_multi_domain_net_effect_is_whole_plan(gateway: FactGateway) -> None:
    intents = [_transfer("p_lina", 50000), _redeem(200000)]
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.plan is not None
    assert plan_net_effect(outcome.plan, gateway).amount == Money(-150000)


def test_any_clarification_blocks_the_whole_plan(gateway: FactGateway) -> None:
    intents = [_transfer("王伟", 50000), _cancel_subscription()]
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.status is ScenarioStatus.NEEDS_CLARIFICATION
    assert outcome.plan is None
    assert outcome.decision is None


def test_multi_domain_plan_hash_is_deterministic(gateway: FactGateway) -> None:
    intents = [_transfer("p_lina", 50000), _cancel_subscription()]
    first = run_scenario(select_handler(intents), intents, gateway)
    second = run_scenario(select_handler(intents), intents, gateway)
    assert first.plan is not None
    assert second.plan is not None
    assert cfhash(first.plan.nodes) == cfhash(second.plan.nodes)
    assert len(cfhash(first.plan.nodes)) == 64


def test_single_domain_funds_is_now_supported(gateway: FactGateway) -> None:
    intents = [_freeze(100000)]
    assert select_handler(intents).name == "cross"
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.status is ScenarioStatus.PLANNED
    assert outcome.plan is not None
    assert [node.action for node in outcome.plan.nodes] == ["funds.freeze"]


def test_frozen_pattern_semantics_are_unchanged(gateway: FactGateway) -> None:
    freeze = _freeze(200000)
    flower = _transfer("p_lina", 30000)
    flower = Intent(
        action=flower.action,
        slots={**flower.slots, "grant": "user_approved"},
        confidence=flower.confidence,
        evidence="订花",
    )
    outcome = run_scenario(select_handler([freeze, flower]), [freeze, flower], gateway)
    assert outcome.plan is not None
    assert [node.node_id for node in outcome.plan.nodes] == ["n1", "n2"]
    assert outcome.plan.nodes[1].depends_on == ("n1",)
