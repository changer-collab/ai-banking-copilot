from datetime import date

import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import ScenarioHandler, ScenarioStatus
from ai_banking_copilot.scenarios.registry import (
    QUERY_HANDLERS,
    SCENARIO_HANDLERS,
    WRITE_HANDLERS,
    UnknownScenarioError,
    run_scenario,
    select_handler,
)
from ai_banking_copilot.tools.catalog import build_default_registry


def _transfer_intent(payee: str = "p_lina") -> Intent:
    return Intent(
        action="transfer.create",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "payee": Value(payee, Trust.TRUSTED),
            "amount": Value(Money(50000), Trust.TRUSTED),
            "when": Value(date(2026, 10, 1), Trust.TRUSTED),
            "span": "转给李娜 500 元",
        },
        confidence=0.9,
        evidence="转账",
    )


def test_all_handlers_satisfy_the_protocol() -> None:
    assert len(SCENARIO_HANDLERS) == 7
    assert all(isinstance(handler, ScenarioHandler) for handler in SCENARIO_HANDLERS)
    assert [handler.name for handler in SCENARIO_HANDLERS] == [
        "cross",
        "life_events",
        "transfer",
        "bills",
        "wealth",
        "cards",
        "subscriptions",
    ]


def test_select_handler_picks_transfer_for_single_domain() -> None:
    assert select_handler([_transfer_intent()]).name == "transfer"


def test_select_handler_picks_cross_for_multi_domain() -> None:
    intents = [
        Intent(action="funds.freeze", slots={}, confidence=0.9, evidence="锁资金"),
        _transfer_intent(),
    ]
    assert select_handler(intents).name == "cross"


def test_select_handler_rejects_unknown_intents() -> None:
    unknown = Intent(action="exec_shell", slots={}, confidence=0.9, evidence="越权")
    with pytest.raises(UnknownScenarioError):
        select_handler([unknown])


def test_run_scenario_reports_clarification_without_a_level(gateway: FactGateway) -> None:
    intent = _transfer_intent(payee="王伟")
    outcome = run_scenario(select_handler([intent]), [intent], gateway)
    assert outcome.status is ScenarioStatus.NEEDS_CLARIFICATION
    assert outcome.plan is None
    assert outcome.decision is None


def test_run_scenario_plans_transfer_at_least_yellow(gateway: FactGateway) -> None:
    intent = _transfer_intent()
    outcome = run_scenario(select_handler([intent]), [intent], gateway)
    assert outcome.status is ScenarioStatus.PLANNED
    assert outcome.plan is not None
    assert outcome.decision is not None
    assert outcome.decision.level >= Level.YELLOW


def test_run_scenario_pure_query_is_green(gateway: FactGateway) -> None:
    intent = Intent(action="bills.monthly", slots={}, confidence=0.9, evidence="上月账单")
    outcome = run_scenario(select_handler([intent]), [intent], gateway)
    assert outcome.status is ScenarioStatus.PLANNED
    assert outcome.decision is not None
    assert outcome.decision.level is Level.GREEN


def test_run_scenario_treats_refusal_as_blocked(gateway: FactGateway) -> None:
    intent = Intent(
        action="wealth.subscribe",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "product": Value("wp_bond", Trust.TRUSTED),
            "amount": Value(Money(100000), Trust.TRUSTED),
        },
        confidence=0.9,
        evidence="申购理财",
    )
    outcome = run_scenario(select_handler([intent]), [intent], gateway)
    assert outcome.status is ScenarioStatus.BLOCKED
    assert outcome.plan is None


def test_every_canonical_action_has_exactly_one_handler() -> None:
    """生产侧**严格双向**断言：读处理器 ∪ 写处理器 == 注册表动作集，且两侧不相交。

    这是「新增动作不必改 `policy/pep.py`」这条不变式的守卫：漏登记任一处理器、
    或把同动作登记进两侧，此处立即报红（对称断言见 `tests/policy/test_pep.py`）。
    """
    registry_actions = set(build_default_registry().actions())
    queries = set(QUERY_HANDLERS)
    writes = set(WRITE_HANDLERS)
    assert queries.isdisjoint(writes)
    assert queries | writes == registry_actions
