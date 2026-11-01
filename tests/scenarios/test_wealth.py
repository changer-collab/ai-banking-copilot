from datetime import date

import pytest

from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.wealth import (
    FORBIDDEN_HINTS,
    AssessmentState,
    RiskAssessment,
    SuitabilityError,
    WealthHandler,
    describe_product,
    product_comparison,
)


def _subscribe_intent(**overrides: object) -> Intent:
    slots: dict[str, object] = {
        "account": Value("acc_savings", Trust.TRUSTED),
        "product": Value("wp_bond", Trust.TRUSTED),
        "amount": Value(Money(100000), Trust.TRUSTED),
        "span": "买 1000 元理财",
    }
    slots.update(overrides)
    return Intent(action="wealth.subscribe", slots=slots, confidence=0.9, evidence="申购理财")


def test_list_plan_is_pure_query(gateway: FactGateway) -> None:
    intent = Intent(action="wealth.list", slots={}, confidence=0.9, evidence="看看理财")
    plan = WealthHandler().build([intent], gateway)
    assert [node.action for node in plan.nodes] == ["wealth.list"]


def test_subscribe_without_assessment_is_refused(gateway: FactGateway) -> None:
    with pytest.raises(SuitabilityError):
        WealthHandler().build([_subscribe_intent()], gateway)


def test_subscribe_rejects_unsuitable_risk(gateway: FactGateway) -> None:
    assessment = RiskAssessment(
        user_id=DEMO_USER_ID, risk_profile="R1", assessed_on=date(2026, 9, 1)
    )
    intent = _subscribe_intent(
        product=Value("wp_equity", Trust.TRUSTED),
        amount=Value(Money(1000000), Trust.TRUSTED),
        assessment=assessment,
    )
    with pytest.raises(SuitabilityError):
        WealthHandler().build([intent], gateway)


def test_subscribe_after_assessment_builds_plan(gateway: FactGateway) -> None:
    assessment = RiskAssessment(
        user_id=DEMO_USER_ID, risk_profile="R3", assessed_on=date(2026, 9, 1)
    )
    plan = WealthHandler().build([_subscribe_intent(assessment=assessment)], gateway)
    node = plan.nodes[0]
    assert node.action == "wealth.subscribe"
    assert node.params["product_id"] == "wp_bond"
    assert node.params["amount_cents"] == 100000


def test_assessment_state_machine_expires() -> None:
    expired = RiskAssessment(
        user_id=DEMO_USER_ID, risk_profile="R3", assessed_on=date(2026, 1, 1), valid_days=30
    )
    assert expired.state_on(date(2026, 10, 1)) is AssessmentState.EXPIRED
    fresh = RiskAssessment(
        user_id=DEMO_USER_ID, risk_profile="R3", assessed_on=date(2026, 9, 30), valid_days=30
    )
    assert fresh.state_on(date(2026, 10, 1)) is AssessmentState.VALID


def test_describe_product_has_no_yield_hint(gateway: FactGateway) -> None:
    product = gateway.wealth_product("wp_bond").payload
    text = describe_product(product)
    assert "R2" in text
    assert all(word not in text for word in FORBIDDEN_HINTS)
    assert "年化" not in text
    assert "收益" not in text


def test_product_comparison_states_facts_only(gateway: FactGateway) -> None:
    items = product_comparison(gateway)
    assert len(items) == 5
    products = tuple(record.payload for record in gateway.wealth_products())
    assert {product.risk_level for product in products} == {"R1", "R2", "R3", "R4"}
    for product in products:
        assert any(product.name in item and product.risk_level in item for item in items)
    joined = "\n".join(items)
    assert all(word not in joined for word in FORBIDDEN_HINTS)
    assert "推荐" not in joined
    assert "排序" not in joined
