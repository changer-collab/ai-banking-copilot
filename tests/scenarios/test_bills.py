from decimal import Decimal

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import plan_is_pure_query
from ai_banking_copilot.scenarios.bills import (
    BillsHandler,
    category_share,
    category_share_in_sandbox,
    category_totals,
)


def _bills_intent() -> Intent:
    return Intent(
        action="bills.monthly",
        slots={"span": "查一下上月账单"},
        confidence=0.9,
        evidence="上月账单",
    )


def test_category_totals_come_from_fact_gateway(gateway: FactGateway) -> None:
    summary = category_totals(gateway, DEMO_USER_ID, 2026, 9)
    assert summary.total_expense.cents == 763980
    assert summary.total_income.cents == 1850000


def test_bills_plan_is_a_two_step_pure_query_dag(gateway: FactGateway) -> None:
    plan = BillsHandler().build([_bills_intent()], gateway)
    assert [node.action for node in plan.nodes] == ["bills.monthly", "bills.category_trend"]
    assert plan.nodes[0].params["month"] == 9
    assert plan.nodes[1].depends_on == ("n1",)
    assert plan.nodes[1].params["category"] == "购物"
    assert plan_is_pure_query(plan) is True


def test_category_share_uses_whitelisted_operator(gateway: FactGateway) -> None:
    share = category_share(gateway, DEMO_USER_ID, 2026, 9, "购物")
    assert share.operator == "ratio"
    assert share.value == Decimal(730000) / Decimal(763980)
    assert share.sources


def test_category_share_in_sandbox_matches_in_process(gateway: FactGateway) -> None:
    in_process = category_share(gateway, DEMO_USER_ID, 2026, 9, "购物")
    sandboxed = category_share_in_sandbox(gateway, DEMO_USER_ID, 2026, 9, "购物")
    assert sandboxed.operator == "ratio"
    assert sandboxed.value == in_process.value
