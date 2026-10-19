from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.plan import validate_plan
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pep import QueryContext, QueryResult
from ai_banking_copilot.scenarios.base import ClarificationRequired, plan_is_pure_query
from ai_banking_copilot.scenarios.bills import (
    ANOMALY_THRESHOLD_CENTS,
    QUERY_HANDLERS,
    BillsHandler,
    category_share,
    category_share_in_sandbox,
    category_totals,
    large_transactions,
)
from ai_banking_copilot.tools.catalog import build_default_registry


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


@pytest.mark.parametrize("slot_name", ["account", "account_id"])
def test_account_transactions_respects_trusted_account(
    gateway: FactGateway, slot_name: str
) -> None:
    intent = Intent(
        action="account.transactions",
        slots={slot_name: Value("acc_savings", Trust.TRUSTED)},
    )
    plan = BillsHandler().build([intent], gateway)
    assert [node.params["account_id"] for node in plan.nodes] == ["acc_savings"]
    assert [node.node_id for node in plan.nodes] == ["txn1"]


def test_account_transactions_keeps_each_intents_account(gateway: FactGateway) -> None:
    intents = [
        Intent(
            action="account.transactions",
            slots={"account": Value(account_id, Trust.TRUSTED)},
        )
        for account_id in ("acc_savings", "acc_current")
    ]
    plan = BillsHandler().build(intents, gateway)
    assert [node.params["account_id"] for node in plan.nodes] == [
        "acc_savings", "acc_current"
    ]
    assert [node.node_id for node in plan.nodes] == ["txn1", "txn2"]
    validate_plan(plan)


@pytest.mark.parametrize("slot_name", ["account", "account_id"])
def test_account_transactions_rejects_untrusted_account(
    gateway: FactGateway, slot_name: str
) -> None:
    intent = Intent(
        action="account.transactions",
        slots={slot_name: Value("acc_savings", Trust.UNVERIFIED)},
    )
    with pytest.raises(ClarificationRequired, match="拒绝不可信来源"):
        BillsHandler().build([intent], gateway)


def test_large_transactions_is_out_only_and_in_cents(gateway: FactGateway) -> None:
    hits = large_transactions(gateway, "acc_current", date(2026, 9, 1), date(2026, 9, 30))
    assert [txn.amount.cents for txn in hits] == [-688000]
    assert large_transactions(gateway, "acc_savings", date(2026, 9, 1), date(2026, 9, 30)) == ()


def test_threshold_unit_is_cents() -> None:
    assert ANOMALY_THRESHOLD_CENTS == 500_000


def _yearly_result(gateway: FactGateway) -> QueryResult:
    spec = build_default_registry().get("bills.yearly")
    return QUERY_HANDLERS["bills.yearly"](
        QueryContext(
            gateway=gateway,
            params={"user_id": DEMO_USER_ID, "year": 2026},
            spec=spec,
        )
    )


def test_yearly_bill_reads_same_period_of_two_years(gateway: FactGateway) -> None:
    """年度**同期同比**的出处 = 本年与上年**同期区间**各自的月度账单事实（H1）。"""
    result = _yearly_result(gateway)
    # 基准月 = 2026-09（基准时钟 2026-10-01 的前一月）⇒ 本年 1–9 月 + 上年 1–9 月 = 18 条。
    assert len(result.record_ids) == 18
    assert len(result.provenance) == 18
    assert len(set(result.record_ids)) == 18
    assert "同期同比口径" in result.detail
    assert "2026 年 1–9 月" in result.detail
    # 种子库含 2025 年同期数据 ⇒ 产出**真实百分比**（2026 支出为 2025 的 100/85 ⇒ +17.65%）。
    assert "同比不可判" not in result.detail
    assert "+17.65%" in result.detail


def test_yearly_bill_reports_no_prior_period_without_calling_operator(tmp_path: Path) -> None:
    """上年同期**完全无流水事实** ⇒ 不调用算子，如实声明「无可比数据」。"""
    db_path = tmp_path / "no_prior.db"
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    with writer.transaction() as conn:
        conn.execute("DELETE FROM transactions WHERE booked_on < '2026-01-01'")
    writer.close()
    reader = Store(db_path, StoreMode.READ_ONLY)
    try:
        detail = _yearly_result(FactGateway(reader)).detail
    finally:
        reader.close()
    assert "上年同期" in detail
    assert "无可比数据" in detail


def test_yearly_bill_refuses_yoy_when_prior_period_sums_to_zero(tmp_path: Path) -> None:
    """上年同期**有事实但支出合计为 0** ⇒ 照走算子、由算子拒绝除零，如实输出「不可判」。"""
    db_path = tmp_path / "zero_prior.db"
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    with writer.transaction() as conn:
        conn.execute("DELETE FROM transactions WHERE booked_on < '2026-01-01'")
        conn.execute(
            "INSERT INTO transactions(txn_id, user_id, account_id, booked_on, amount_cents,"
            " merchant, category, channel, direction) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "txn_prior_income",
                DEMO_USER_ID,
                "acc_savings",
                "2025-03-05",
                100000,
                "测试商户",
                "工资",
                "bank",
                "in",
            ),
        )
    writer.close()
    reader = Store(db_path, StoreMode.READ_ONLY)
    try:
        detail = _yearly_result(FactGateway(reader)).detail
    finally:
        reader.close()
    assert "支出合计为 0" in detail
    assert "同比不可判" in detail
