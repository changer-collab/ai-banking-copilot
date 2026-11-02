from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.plan import validate_plan
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.errors import SandboxTimeout, SandboxViolation
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.sandbox import SandboxResourceLimit, SandboxUnavailable
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.analysis_codegen import (
    AnalysisCodeGenerationError,
    AnalysisTask,
)
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pep import NotAuthorizedError, QueryContext, QueryResult
from ai_banking_copilot.scenarios import bills as bills_module
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


@pytest.fixture(autouse=True)
def _disable_real_codegen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIB_DISABLE_LLM", "1")


def _bills_intent() -> Intent:
    return Intent(
        action="bills.monthly",
        slots={"span": "查一下上月账单"},
        confidence=0.9,
        evidence="上月账单",
    )


def test_category_totals_come_from_fact_gateway(gateway: FactGateway) -> None:
    summary = category_totals(gateway, DEMO_USER_ID, 2026, 9)
    assert summary.total_expense.cents == 1612980
    assert summary.total_income.cents == 1850000


def test_monthly_bills_plan_is_a_single_pure_query(gateway: FactGateway) -> None:
    plan = BillsHandler().build([_bills_intent()], gateway)
    assert [node.action for node in plan.nodes] == ["bills.monthly"]
    assert plan.nodes[0].node_id == "n1"
    assert plan.nodes[0].params["month"] == 9
    assert plan.nodes[0].depends_on == ()
    assert plan_is_pure_query(plan) is True


def test_explicit_trend_plan_keeps_monthly_dependency(gateway: FactGateway) -> None:
    plan = BillsHandler().build([Intent(action="bills.category_trend", slots={})], gateway)
    assert [node.action for node in plan.nodes] == ["bills.monthly", "bills.category_trend"]
    assert plan.nodes[0].params["month"] == 9
    assert plan.nodes[1].depends_on == ("n1",)
    assert plan.nodes[1].params["category"] == "购物"
    assert plan.nodes[1].params["base_year"] == 2026
    assert plan.nodes[1].params["base_month"] == 8
    assert plan_is_pure_query(plan) is True


@pytest.mark.parametrize("trend_first", [False, True])
def test_monthly_and_trend_intents_do_not_duplicate_nodes(
    gateway: FactGateway, trend_first: bool
) -> None:
    intents = [_bills_intent(), Intent(action="bills.category_trend", slots={})]
    plan = BillsHandler().build(intents[::-1] if trend_first else intents, gateway)
    assert [node.action for node in plan.nodes] == ["bills.monthly", "bills.category_trend"]
    assert [node.node_id for node in plan.nodes] == ["n1", "n2"]
    assert plan.nodes[1].depends_on == ("n1",)
    validate_plan(plan)


def test_monthly_bill_without_expenses_still_builds(gateway: FactGateway) -> None:
    intent = Intent(
        action="bills.monthly",
        slots={"year": Value(2030, Trust.TRUSTED), "month": Value(1, Trust.TRUSTED)},
    )
    assert category_totals(gateway, DEMO_USER_ID, 2030, 1).total_expense.cents == 0
    plan = BillsHandler().build([intent], gateway)
    assert [node.action for node in plan.nodes] == ["bills.monthly"]
    assert plan.nodes[0].params == {"user_id": DEMO_USER_ID, "year": 2030, "month": 1}


def test_trend_respects_trusted_period_and_category(gateway: FactGateway) -> None:
    intent = Intent(
        action="bills.category_trend",
        slots={
            "year": Value(2026, Trust.TRUSTED),
            "month": Value(1, Trust.TRUSTED),
            "category": Value("餐饮", Trust.TRUSTED),
        },
    )
    plan = BillsHandler().build([intent], gateway)
    assert plan.nodes[0].params == {"user_id": DEMO_USER_ID, "year": 2026, "month": 1}
    assert plan.nodes[1].params == {
        "user_id": DEMO_USER_ID,
        "category": "餐饮",
        "base_year": 2025,
        "base_month": 12,
    }


@pytest.mark.parametrize(
    ("action", "slot_name"),
    [
        ("bills.monthly", "year"),
        ("bills.monthly", "month"),
        ("bills.category_trend", "year"),
        ("bills.category_trend", "month"),
        ("bills.category_trend", "category"),
    ],
)
def test_bills_rejects_untrusted_control_slots(
    gateway: FactGateway, action: str, slot_name: str
) -> None:
    slots = {
        "year": Value(2026, Trust.TRUSTED),
        "month": Value(9, Trust.TRUSTED),
        "category": Value("餐饮", Trust.TRUSTED),
    }
    slots[slot_name] = Value(slots[slot_name].raw, Trust.UNVERIFIED)
    with pytest.raises(ClarificationRequired, match="拒绝不可信来源"):
        BillsHandler().build([Intent(action=action, slots=slots)], gateway)


def test_category_share_uses_whitelisted_operator(gateway: FactGateway) -> None:
    share = category_share(gateway, DEMO_USER_ID, 2026, 9, "购物")
    assert share.operator == "ratio"
    assert share.value == Decimal(826000) / Decimal(1612980)
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
    assert [node.action for node in plan.nodes] == [
        "account.transactions", "account.transactions", "bills.monthly"
    ]
    assert ids == ["txn1", "txn2", "n1"]


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
    # 基准月 = 2026-09（基准时钟 2026-10-31 的前一月）⇒ 本年 1–9 月 + 上年 1–9 月 = 18 条。
    assert len(result.record_ids) == 18
    assert len(result.provenance) == 18
    assert len(set(result.record_ids)) == 18
    assert "同期同比口径" in result.detail
    assert "2026 年 1–9 月" in result.detail
    # 种子库含 2025 年同期数据 ⇒ 产出**真实百分比**：
    # 2026 支出 145366.22 / 2025 同期 123638.22 ⇒ +17.57%。
    assert "同比不可判" not in result.detail
    assert "+17.57%" in result.detail
    assert len(result.sandbox_events) == 4
    assert result.sandbox_events[0]["use"] == "current_year_expense"
    first_result = result.sandbox_events[0]["result"]
    assert isinstance(first_result, dict)
    assert first_result["operator"] == "sum"
    assert len(first_result["sources"]) == 9
    assert result.sandbox_events[-1]["task"] == "yoy"
    assert result.sandbox_events[-1]["use"] == "year_over_year"
    yoy_result = result.sandbox_events[-1]["result"]
    assert isinstance(yoy_result, dict)
    assert yoy_result["operator"] == "yoy"


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


@pytest.mark.parametrize(
    "error_type",
    [SandboxUnavailable, SandboxTimeout, SandboxResourceLimit, SandboxViolation],
)
def test_yearly_bill_sandbox_failure_returns_sources_without_annual_values(
    gateway: FactGateway,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise error_type("test sandbox failure")

    monkeypatch.setattr(bills_module, "run_generated_analysis", fail)
    result = _yearly_result(gateway)

    assert len(result.record_ids) == 18
    assert len(result.provenance) == 18
    assert len(set(result.record_ids)) == 18
    assert result.detail == "年度账单分析沙箱拒绝或无法执行；本次未生成年度数值。"


def test_yearly_bill_yoy_sandbox_failure_hides_computed_annual_values(
    gateway: FactGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_analysis = bills_module.run_generated_analysis

    def fail_yoy(code: str, snapshot: object, **kwargs: object) -> object:
        if code.startswith("yoy("):
            raise SandboxTimeout("test timeout")
        return real_analysis(code, snapshot, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bills_module, "run_generated_analysis", fail_yoy)
    result = _yearly_result(gateway)

    assert len(result.record_ids) == 18
    assert result.detail == "年度账单分析沙箱拒绝或无法执行；本次未生成年度数值。"


def test_yearly_bill_codegen_receives_only_tasks_and_field_names(
    gateway: FactGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[AnalysisTask, tuple[str, ...]]] = []

    def generate(task: AnalysisTask, fields: tuple[str, ...]) -> str:
        calls.append((task, fields))
        if task is AnalysisTask.SUM:
            return f"sum({fields[0]})"
        return f"yoy(sum({fields[0]}), sum({fields[1]}))"

    monkeypatch.setattr(bills_module, "generate_analysis_code", generate)

    result = _yearly_result(gateway)

    assert "+17.57%" in result.detail
    assert calls == [
        (AnalysisTask.SUM, ("facts",)),
        (AnalysisTask.SUM, ("facts",)),
        (AnalysisTask.SUM, ("facts",)),
        (AnalysisTask.YOY, ("current", "prior")),
    ]


def test_yearly_bill_codegen_failure_hides_computed_annual_values(
    gateway: FactGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> str:
        raise AnalysisCodeGenerationError("invalid model output")

    monkeypatch.setattr(bills_module, "generate_analysis_code", fail)

    result = _yearly_result(gateway)

    assert len(result.record_ids) == 18
    assert result.detail == "年度账单分析沙箱拒绝或无法执行；本次未生成年度数值。"


def test_yearly_bill_preserves_failed_sandbox_monitor_event(
    gateway: FactGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = {
        "code_sha256": "a" * 64,
        "limits": {"timeout_s": 2.0},
        "duration_ms": 2_000,
        "exit_code": -1,
        "status": "timeout",
        "isolation": "process",
    }

    def fail(*args: object, **kwargs: object) -> object:
        error = SandboxTimeout("test timeout")
        error.monitor = monitor
        raise error

    monkeypatch.setattr(bills_module, "run_generated_analysis", fail)

    result = _yearly_result(gateway)

    assert len(result.sandbox_events) == 1
    event = result.sandbox_events[0]
    assert event["code_sha256"] == monitor["code_sha256"]
    assert event["task"] == "sum"
    assert event["use"] == "current_year_expense"
    assert event["result"] is None
    assert event["failure"] == "SandboxTimeout"


def test_yearly_bill_rejects_result_when_monitor_audit_fails(
    gateway: FactGateway,
) -> None:
    spec = build_default_registry().get("bills.yearly")

    def reject_monitor(_monitor: object) -> None:
        raise NotAuthorizedError("隔离执行审计不可用，拒绝使用沙箱结果")

    with pytest.raises(NotAuthorizedError, match="隔离执行审计不可用"):
        QUERY_HANDLERS["bills.yearly"](
            QueryContext(
                gateway=gateway,
                params={"user_id": DEMO_USER_ID, "year": 2026},
                spec=spec,
                sandbox_monitor=reject_monitor,
            )
        )
