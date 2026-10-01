"""账单分析场景：分类与统计**全走事实网关**；派生数字只走**白名单算子**并在沙箱执行。

口径（PRD §4.1 场景② / §5.1）：
- 数值只来自 `FactGateway.monthly_bill`（H1 事实零生成）；
- 派生数字（占比 / 同比 / 环比）只走 `gateway.operators` 白名单；
- 派生计算一律在 `gateway.sandbox` 的受限子进程中执行（无网络 / 禁库 / 禁写 / 限时）。
"""

from __future__ import annotations

from collections.abc import Sequence
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
from ai_banking_copilot.gateway.models import BillSummary
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
        year, month = _period(intents)
        summary = category_totals(gateway, DEMO_USER_ID, year, month)
        category = _chosen_category(intents, summary)
        base_year, base_month = _previous_month(year, month)
        ledger = AnchorLedger()
        nodes = (
            PlanNode(
                node_id="n1",
                action="bills.monthly",
                params={"user_id": DEMO_USER_ID, "year": year, "month": month},
                intent=IntentRef(
                    source=AnchorSource.EXPLICIT,
                    anchor_id=grant_anchor(ledger, AnchorSource.EXPLICIT, "账单月度统计"),
                ),
            ),
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
            ),
        )
        plan = Plan(plan_id="bills_monthly", nodes=nodes)
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
