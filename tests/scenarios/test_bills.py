from datetime import date
from decimal import Decimal

from ai_banking_copilot.contracts.plan import validate_plan
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import plan_is_pure_query
from ai_banking_copilot.scenarios.bills import (
    ANOMALY_THRESHOLD_CENTS,
    BillsHandler,
    category_share,
    category_share_in_sandbox,
    category_totals,
    large_transactions,
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


def _account_transactions_intent() -> Intent:
    return Intent(
        action="account.transactions",
        slots={"span": "有没有异常交易"},
        confidence=0.9,
        evidence="有没有异常交易",
    )


def test_account_transactions_only_plan_uses_txn_namespace(gateway: FactGateway) -> None:
    plan = BillsHandler().build([_account_transactions_intent()], gateway)
    assert [node.node_id for node in plan.nodes] == ["txn1", "txn2"]
    assert all(node.action == "account.transactions" for node in plan.nodes)
    assert not any(node.action.startswith("bills.") for node in plan.nodes)
    assert plan_is_pure_query(plan) is True
    for node in plan.nodes:
        assert node.params["start"] == "2026-09-01"
        assert node.params["end"] == "2026-09-30"
        assert isinstance(node.params["account_id"], str)
        assert node.params["account_id"]


def test_account_and_bills_mixed_plan_has_unique_ids(gateway: FactGateway) -> None:
    plan = BillsHandler().build([_account_transactions_intent(), _bills_intent()], gateway)
    ids = [node.node_id for node in plan.nodes]
    assert len(set(ids)) == len(ids)
    validate_plan(plan)
    assert any(node_id.startswith("txn") for node_id in ids)
    assert {"n1", "n2"}.issubset(set(ids))


def test_large_transactions_is_out_only_and_in_cents(gateway: FactGateway) -> None:
    hits = large_transactions(gateway, "acc_current", date(2026, 9, 1), date(2026, 9, 30))
    assert [txn.amount.cents for txn in hits] == [-688000]
    assert large_transactions(gateway, "acc_savings", date(2026, 9, 1), date(2026, 9, 30)) == ()


def test_threshold_unit_is_cents() -> None:
    assert ANOMALY_THRESHOLD_CENTS == 500_000
