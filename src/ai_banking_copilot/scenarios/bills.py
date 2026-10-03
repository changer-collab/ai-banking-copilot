"""账单分析场景：分类与统计**全走事实网关**；派生数字只走**白名单算子**并在沙箱执行。

口径（PRD §4.1 场景② / §5.1）：
- 数值只来自 `FactGateway.monthly_bill`（H1 事实零生成）；
- 派生数字（占比 / 同比 / 环比）只走 `gateway.operators` 白名单；
- 派生计算一律在 `gateway.sandbox` 的受限子进程中执行（无网络 / 禁库 / 禁写 / 限时）。
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import BillSummary, Money, Transaction
from ai_banking_copilot.gateway.operators import DerivedValue, run_operator
from ai_banking_copilot.gateway.sandbox import (
    SandboxRequest,
    SandboxResult,
    run_in_sandbox,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import (
    ScenarioHandler,
    ScenarioRefused,
    as_int,
    grant_anchor,
    require_trusted,
)

BILLS_ACTIONS = frozenset(
    {"account.balance", "account.transactions", "bills.monthly", "bills.category_trend"}
)
"""账单域动作（README 规范动作表 #1–#4）。"""

ANOMALY_THRESHOLD_CENTS: int = 500_000
"""异常大额阈值（**单位=分**，= 5000 元）。**展示层谓词**，不进算子 / PDP / PEP。"""


def large_transactions(
    gateway: FactGateway, account_id: str, start: date, end: date
) -> tuple[Transaction, ...]:
    """只读：区间内「转出方向且金额 ≤ -阈值」的交易（单位=分、方向写死，禁与元比较）。"""
    return tuple(
        record.payload
        for record in gateway.transactions_between(account_id, start, end)
        if record.payload.direction == "out"
        and record.payload.amount.cents <= -ANOMALY_THRESHOLD_CENTS
    )


def describe_large_transactions(transactions: Sequence[Transaction]) -> str:
    """只拼事实（笔数 / 最大额 / 日期）；**绝不渲染 merchant 自由文本**（H3 展示安全）。"""
    if not transactions:
        return "未发现 ≥5000 元的大额支出"
    largest = max(transactions, key=lambda txn: (-txn.amount.cents, txn.txn_id))
    amount = Money(-largest.amount.cents).yuan
    return (
        f"其中 ≥5000 元支出 {len(transactions)} 笔，"
        f"最大 {amount} 元（{largest.booked_on.isoformat()}）"
    )


def category_totals(gateway: FactGateway, user_id: str, year: int, month: int) -> BillSummary:
    """分类统计**全走事实网关**（H1）。"""
    return gateway.monthly_bill(user_id, year, month).payload


def category_share(
    gateway: FactGateway, user_id: str, year: int, month: int, category: str
) -> DerivedValue:
    """分类占比：派生数字只走**白名单算子**（进程内，供沙箱结果交叉验证）。"""
    part, total, prefix = _share_inputs(gateway, user_id, year, month, category)
    return run_operator(
        "ratio",
        ((f"bills:{prefix}:{category}", Decimal(part)), (f"bills:{prefix}", Decimal(total))),
    )


def category_share_in_sandbox(
    gateway: FactGateway, user_id: str, year: int, month: int, category: str
) -> SandboxResult:
    """同一派生数字在**受限沙箱**中执行（无网络 / 禁库 / 禁写 / 限时）。"""
    part, total, prefix = _share_inputs(gateway, user_id, year, month, category)
    request = SandboxRequest(
        operator="ratio",
        inputs=((f"bills:{prefix}:{category}", Decimal(part)), (f"bills:{prefix}", Decimal(total))),
    )
    return run_in_sandbox(request)


class BillsHandler(ScenarioHandler):
    """账单分析场景处理器。"""

    @property
    def name(self) -> str:
        return "bills"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return any(intent.action in BILLS_ACTIONS for intent in intents)

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        account_txn = [
            intent for intent in intents if intent.action == "account.transactions"
        ]
        bills_specific = [
            intent
            for intent in intents
            if intent.action in {"bills.monthly", "bills.category_trend"}
        ]
        build_bills = bool(bills_specific) or not account_txn
        ledger = AnchorLedger()
        nodes: list[PlanNode] = []
        if account_txn:
            year, month = _period(intents)
            start, end = _month_range(year, month)
            for index, record in enumerate(gateway.accounts_of(DEMO_USER_ID), start=1):
                nodes.append(
                    PlanNode(
                        node_id=f"txn{index}",
                        action="account.transactions",
                        params={
                            "account_id": record.payload.account_id,
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                        },
                        intent=IntentRef(
                            source=AnchorSource.EXPLICIT,
                            anchor_id=grant_anchor(
                                ledger, AnchorSource.EXPLICIT, "异常交易查询"
                            ),
                        ),
                    )
                )
        if build_bills:
            year, month = _period(intents)
            summary = category_totals(gateway, DEMO_USER_ID, year, month)
            category = _chosen_category(intents, summary)
            base_year, base_month = _previous_month(year, month)
            nodes.append(
                PlanNode(
                    node_id="n1",
                    action="bills.monthly",
                    params={"user_id": DEMO_USER_ID, "year": year, "month": month},
                    intent=IntentRef(
                        source=AnchorSource.EXPLICIT,
                        anchor_id=grant_anchor(ledger, AnchorSource.EXPLICIT, "账单月度统计"),
                    ),
                )
            )
            nodes.append(
                PlanNode(
                    node_id="n2",
                    action="bills.category_trend",
                    params={
                        "user_id": DEMO_USER_ID,
                        "category": category,
                        "base_year": base_year,
                        "base_month": base_month,
                    },
                    intent=IntentRef(
                        source=AnchorSource.EXPLICIT,
                        anchor_id=grant_anchor(ledger, AnchorSource.EXPLICIT, "账单分类趋势"),
                    ),
                    depends_on=("n1",),
                )
            )
        plan = Plan(plan_id="bills_monthly", nodes=tuple(nodes))
        validate_plan(plan)
        return plan


def _share_inputs(
    gateway: FactGateway, user_id: str, year: int, month: int, category: str
) -> tuple[int, int, str]:
    summary = category_totals(gateway, user_id, year, month)
    part = _category_expense_cents(summary, category)
    return part, summary.total_expense.cents, f"{user_id}:{year:04d}-{month:02d}"


def _category_expense_cents(summary: BillSummary, category: str) -> int:
    for item in summary.by_category:
        if item.category == category and item.total.cents < 0:
            return -item.total.cents
    raise ScenarioRefused(f"当期账单不存在支出分类：{category}")


def _period(intents: Sequence[Intent]) -> tuple[int, int]:
    for intent in intents:
        if intent.action in BILLS_ACTIONS and "year" in intent.slots and "month" in intent.slots:
            year = as_int(require_trusted(intent.slots["year"], "year"), "year")
            month = as_int(require_trusted(intent.slots["month"], "month"), "month")
            return year, month
    return _previous_month(DEFAULT_BOOKING_DATE.year, DEFAULT_BOOKING_DATE.month)


def _previous_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _month_range(year: int, month: int) -> tuple[date, date]:
    _, last_day = calendar.monthrange(year, month)
    return date(year, month, 1), date(year, month, last_day)


def _chosen_category(intents: Sequence[Intent], summary: BillSummary) -> str:
    for intent in intents:
        if intent.action in BILLS_ACTIONS and "category" in intent.slots:
            raw = require_trusted(intent.slots["category"], "category")
            if isinstance(raw, str) and raw:
                return raw
            raise ScenarioRefused("分类取值不合法")
    expenses = [item for item in summary.by_category if item.total.cents < 0]
    if not expenses:
        raise ScenarioRefused("当期账单无可分析支出，拒绝生成趋势节点")
    top = max(expenses, key=lambda item: (-item.total.cents, item.category))
    return top.category
