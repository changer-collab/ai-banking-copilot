"""账单后的只读消费建议：事实来自网关，派生数值来自白名单算子。"""

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ai_banking_copilot.gateway.facts import FactGateway, Record
from ai_banking_copilot.gateway.models import BillSummary, Money
from ai_banking_copilot.gateway.operators import run_operator
from ai_banking_copilot.scenarios.bill_categories import is_consumption_category
from ai_banking_copilot.scenarios.bill_findings import consumption_totals

# 仅作为产品提醒规则，不据此判断消费是否必要。
_ADJUSTABLE_CATEGORIES = frozenset({"购物", "餐饮", "娱乐"})
_SHARE_THRESHOLD = Decimal("0.50")
_GROWTH_THRESHOLD = Decimal("0.20")
_INCREASE_THRESHOLD_CENTS = 10_000


@dataclass(frozen=True)
class BillAdvice:
    kind: str
    period: str
    title: str
    evidence: str
    suggestion: str
    source_refs: tuple[tuple[str, str], ...]


def build_bill_advice(
    gateway: FactGateway, user_id: str, year: int, month: int, *, as_of: date
) -> tuple[BillAdvice, ...]:
    """返回至多三条可回读的提醒；不写预算、订阅或银行业务事实。"""
    current = gateway.monthly_bill(user_id, year, month)
    consumption, _income, _transfer, _transfer_count = consumption_totals(
        gateway, user_id, year, month
    )
    if consumption <= 0:
        return ()

    period = f"{year:04d}-{month:02d}"
    current_sources = _bill_sources(gateway, current)
    expenses = _net_expenses(current.payload)
    results: list[BillAdvice] = []
    category, cents = (
        max(expenses.items(), key=lambda item: (item[1], item[0]))
        if expenses
        else ("", 0)
    )
    if category in _ADJUSTABLE_CATEGORIES:
        share = run_operator(
            "ratio",
            (
                (current.record_id, Decimal(cents)),
                (current.record_id, Decimal(consumption)),
            ),
        )
        if share.value >= _SHARE_THRESHOLD:
            results.append(
                BillAdvice(
                    kind="category-share",
                    period=period,
                    title=f"复核{category}支出安排",
                    evidence=(
                        f"{period} {category}净支出 {Money(cents).yuan} 元，"
                        f"占当期消费 {Money(consumption).yuan} 元的 "
                        f"{share.value:.2%}（转账/还款等不计入消费）。"
                    ),
                    suggestion=(
                        f"先核对{category}中的大额或一次性购买，再考虑设置{category}额度，"
                        "仅减少可调整部分；占比高不代表不必要。50% 仅为产品提醒规则。"
                    ),
                    source_refs=current_sources,
                )
            )

    base_year, base_month = (year - 1, 12) if month == 1 else (year, month - 1)
    previous = gateway.monthly_bill(user_id, base_year, base_month)
    growth = _growth_advice(current, previous, current_sources, gateway)
    if growth is not None:
        results.append(growth)

    subscriptions = tuple(
        record
        for record in gateway.subscriptions_of(user_id)
        if record.payload.user_id == user_id
        and record.payload.status == "active"
        and record.payload.next_charge_on >= as_of
    )
    if subscriptions:
        groups: list[str] = []
        for cycle, label in (("monthly", "月度"), ("yearly", "年度")):
            entries = [
                f"订阅 {record.payload.subscription_id}，每期 {record.payload.amount.yuan} 元，"
                f"下次扣费 {record.payload.next_charge_on.isoformat()}"
                for record in subscriptions
                if record.payload.period == cycle
            ]
            if entries:
                groups.append(f"{label}：" + "；".join(entries))
        results.append(
            BillAdvice(
                kind="subscription-review",
                period=period,
                title="核对当前订阅续费",
                evidence=f"截至 {as_of.isoformat()} 的当前续费核对。" + "。".join(groups) + "。",
                suggestion=(
                    "逐项核对续费是否仍需要，以及实际使用情况，再决定是否保留；"
                    "月度与年度费用分别核对，不直接相加。"
                ),
                source_refs=tuple((item.record_id, item.content_hash) for item in subscriptions),
            )
        )
    return tuple(results)


def _net_expenses(summary: BillSummary) -> dict[str, int]:
    """分类净额：退款先在同分类抵扣，仅取负净额；转账/还款等不计入消费。"""
    return {
        item.category: -item.total.cents
        for item in summary.by_category
        if item.total.cents < 0 and is_consumption_category(item.category)
    }


def _growth_advice(
    current: Record[BillSummary],
    previous: Record[BillSummary],
    current_sources: tuple[tuple[str, str], ...],
    gateway: FactGateway,
) -> BillAdvice | None:
    current_expenses = _net_expenses(current.payload)
    previous_expenses = _net_expenses(previous.payload)
    candidates: list[tuple[int, str, Decimal]] = []
    for category in sorted(_ADJUSTABLE_CATEGORIES):
        cents = current_expenses.get(category, 0)
        base_cents = previous_expenses.get(category, 0)
        if cents <= base_cents or base_cents <= 0:
            continue
        increase = run_operator(
            "sum",
            ((current.record_id, Decimal(cents)), (previous.record_id, Decimal(-base_cents))),
        )
        if increase.value < _INCREASE_THRESHOLD_CENTS:
            continue
        rate = run_operator(
            "mom",
            ((current.record_id, Decimal(cents)), (previous.record_id, Decimal(base_cents))),
        )
        if rate.value >= _GROWTH_THRESHOLD:
            candidates.append((int(increase.value), category, rate.value))
    if not candidates:
        return None

    increase_cents, category, rate_value = max(candidates)
    period = f"{current.payload.year:04d}-{current.payload.month:02d}"
    base_period = f"{previous.payload.year:04d}-{previous.payload.month:02d}"
    return BillAdvice(
        kind="category-growth",
        period=period,
        title=f"关注{category}净支出变化",
        evidence=(
            f"{base_period} {category}净支出 {Money(previous_expenses[category]).yuan} 元，"
            f"{period} {category}净支出 {Money(current_expenses[category]).yuan} 元，"
            f"增加 {Money(increase_cents).yuan} 元，环比 +{rate_value:.2%}。"
        ),
        suggestion=(
            f"对照两期{category}明细，确认是否有一次性购买，再考虑设置{category}额度，"
            "仅调整可减少的部分；增长不等于浪费。20% 且增加至少 100 元仅为产品提醒规则。"
        ),
        source_refs=(*current_sources, *_bill_sources(gateway, previous)),
    )


def _bill_sources(gateway: FactGateway, record: Record[BillSummary]) -> tuple[tuple[str, str], ...]:
    """保留汇总账单及其区间流水来源，不展示 merchant 自由文本。"""
    summary = record.payload
    start = date(summary.year, summary.month, 1)
    end = date(summary.year, summary.month, calendar.monthrange(summary.year, summary.month)[1])
    sources = [(record.record_id, record.content_hash)]
    for account in gateway.accounts_of(summary.user_id):
        sources.extend(
            (transaction.record_id, transaction.content_hash)
            for transaction in gateway.transactions_between(account.payload.account_id, start, end)
            if transaction.payload.user_id == summary.user_id
        )
    return tuple(sources)
