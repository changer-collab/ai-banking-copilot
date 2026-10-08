"""账单分析场景：分类与统计**全走事实网关**；派生数字只走**白名单算子**并在沙箱执行。

口径（PRD §4.1 场景② / §5.1）：
- 数值只来自 `FactGateway.monthly_bill`（H1 事实零生成）；
- 派生数字（占比 / 同比 / 环比）只走 `gateway.operators` 白名单；
- 派生计算一律在 `gateway.sandbox` 的受限子进程中执行（无网络 / 禁库 / 禁写 / 限时）。
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Mapping, Sequence
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
from ai_banking_copilot.gateway.facts import FactGateway, Record
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
from ai_banking_copilot.policy.pep import (
    QueryContext,
    QueryHandler,
    QueryResult,
    require_date,
    require_int,
    require_str,
)
from ai_banking_copilot.scenarios.base import (
    ScenarioHandler,
    ScenarioRefused,
    as_int,
    grant_anchor,
    require_trusted,
)

BILLS_ACTIONS = frozenset(
    {
        "account.balance",
        "account.transactions",
        "bills.monthly",
        "bills.category_trend",
        "bills.yearly",
    }
)
"""账单域动作（README 规范动作表 #1–#4 + 步骤 2 冻结的 `bills.yearly`）。"""

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
        yearly = [intent for intent in intents if intent.action == "bills.yearly"]
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
        if yearly:
            nodes.append(
                PlanNode(
                    node_id="y1",
                    action="bills.yearly",
                    params={"user_id": DEMO_USER_ID, "year": _year(intents)},
                    intent=IntentRef(
                        source=AnchorSource.EXPLICIT,
                        anchor_id=grant_anchor(ledger, AnchorSource.EXPLICIT, "年度账单汇总"),
                    ),
                )
            )
        elif build_bills:
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
        plan = Plan(plan_id="bills_yearly" if yearly else "bills_monthly", nodes=tuple(nodes))
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


def _year(intents: Sequence[Intent]) -> int:
    """年度账单的年份：取可信 `year` 槽；缺省用基准记账日的年份（不猜）。"""
    for intent in intents:
        if intent.action == "bills.yearly" and "year" in intent.slots:
            return as_int(require_trusted(intent.slots["year"], "year"), "year")
    return DEFAULT_BOOKING_DATE.year


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


def _account_balance(context: QueryContext) -> QueryResult:
    record = context.gateway.account(require_str(context.params, "account_id"))
    return QueryResult(
        record_ids=(record.record_id,),
        provenance=((record.record_id, record.content_hash),),
        detail=f"账户 {record.payload.account_id} 余额 {record.payload.balance.yuan} 元",
    )


def _account_transactions(context: QueryContext) -> QueryResult:
    account_id = require_str(context.params, "account_id")
    rows = context.gateway.transactions_between(
        account_id,
        require_date(context.params, "start"),
        require_date(context.params, "end"),
    )
    return QueryResult(
        record_ids=tuple(item.record_id for item in rows),
        provenance=tuple((item.record_id, item.content_hash) for item in rows),
        detail=f"账户 {account_id} 区间交易 {len(rows)} 笔",
    )


def _monthly_bill(context: QueryContext) -> QueryResult:
    year = require_int(context.params, "year")
    month = require_int(context.params, "month")
    summary = context.gateway.monthly_bill(
        require_str(context.params, "user_id"), year, month
    )
    return QueryResult(
        record_ids=(summary.record_id,),
        provenance=((summary.record_id, summary.content_hash),),
        detail=(
            f"{year}-{month:02d} 支出 {summary.payload.total_expense.yuan} 元"
            f" / 收入 {summary.payload.total_income.yuan} 元"
        ),
    )


def _category_trend(context: QueryContext) -> QueryResult:
    summary = context.gateway.monthly_bill(
        require_str(context.params, "user_id"),
        require_int(context.params, "base_year"),
        require_int(context.params, "base_month"),
    )
    category = require_str(context.params, "category")
    total = Money(0)
    for item in summary.payload.by_category:
        if item.category == category:
            total = item.total
    return QueryResult(
        record_ids=(summary.record_id,),
        provenance=((summary.record_id, summary.content_hash),),
        detail=f"分类 {category} 基期合计 {total.yuan} 元",
    )


def _yearly_bill(context: QueryContext) -> QueryResult:
    """年度账单（P2-1 加固）：读**本年 + 上年**各 12 条月度事实。

    本年合计、上年合计各自只走 `sum` 算子；**同比**用 `yoy` 算子（H1 事实零生成，
    出处携带两年共 24 个 `record_id`）。上年合计为 0 时 `yoy` 不应计算（白名单算子
    **拒绝不可判**，不得编造），如实声明「同比不可判」。
    """
    user_id = require_str(context.params, "user_id")
    year = require_int(context.params, "year")
    this_year = tuple(
        context.gateway.monthly_bill(user_id, year, month) for month in range(1, 13)
    )
    last_year = tuple(
        context.gateway.monthly_bill(user_id, year - 1, month) for month in range(1, 13)
    )
    expense = _annual_sum(this_year, lambda bill: bill.total_expense.cents)
    income = _annual_sum(this_year, lambda bill: bill.total_income.cents)
    last_expense = _annual_sum(last_year, lambda bill: bill.total_expense.cents)
    detail = (
        f"{year} 年度支出 {expense.yuan} 元 / 收入 {income.yuan} 元"
        f"（{len(this_year)} 个月度账单事实合计）"
    )
    if last_expense.cents != 0:
        yoy = run_operator(
            "yoy",
            (
                (f"bills:{user_id}:{year:04d}", Decimal(expense.cents)),
                (f"bills:{user_id}:{year - 1:04d}", Decimal(last_expense.cents)),
            ),
        )
        detail += f"；支出同比 {_percent(yoy.value)}（上年 {last_expense.yuan} 元）"
    else:
        detail += f"；上年（{year - 1}）无可比账单支出，同比不可判"
    records = (*this_year, *last_year)
    return QueryResult(
        record_ids=tuple(item.record_id for item in records),
        provenance=tuple((item.record_id, item.content_hash) for item in records),
        detail=detail,
    )


def _percent(ratio: Decimal) -> str:
    """把算子产出的比率**格式化**成百分比（只做渲染，不生成事实）。"""
    return f"{(ratio * 100).quantize(Decimal('0.01'))}%"


def _annual_sum(
    records: tuple[Record[BillSummary], ...], pick: Callable[[BillSummary], int]
) -> Money:
    """年度合计：派生数字只走白名单算子，输出携带全部来源 `record_id`。"""
    derived = run_operator(
        "sum",
        tuple((record.record_id, Decimal(pick(record.payload))) for record in records),
    )
    return Money(int(derived.value))


QUERY_HANDLERS: Mapping[str, QueryHandler] = {
    "account.balance": _account_balance,
    "account.transactions": _account_transactions,
    "bills.monthly": _monthly_bill,
    "bills.category_trend": _category_trend,
    "bills.yearly": _yearly_bill,
}
"""账单域**读**处理器（组合根汇入 PEP；`docs/待修复清单.md` 步骤 4「残留」补做）。"""
