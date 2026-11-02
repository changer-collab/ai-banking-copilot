"""账单预算进度：只读派生，不写银行事实。

预算本身是提醒偏好（存在洞察元数据里）；本模块只把「预算 + 本期消费」
算成页面可直接渲染的进度、剩余与日均可用。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ai_banking_copilot.scenarios.bill_findings import CategoryShare


def budget_progress(
    *,
    month: str,
    consumption_cents: int,
    shares: Sequence[CategoryShare],
    total_budget_cents: int | None,
    category_budgets: Mapping[str, int],
    days_left: int,
    is_current_month: bool,
) -> dict[str, object]:
    """返回预算进度；未设置总额时 ``needs_setup=True``，不编造 0 进度。"""
    spent_by_l1: dict[str, int] = {}
    for item in shares:
        spent_by_l1[item.category_l1] = (
            spent_by_l1.get(item.category_l1, 0) + item.expense_cents
        )

    categories: list[dict[str, object]] = []
    for category, budget in sorted(category_budgets.items()):
        spent = spent_by_l1.get(category, 0)
        categories.append(
            {
                "category": category,
                "budget_cents": budget,
                "spent_cents": spent,
                "remaining_cents": budget - spent,
                "over": spent > budget,
            }
        )

    if total_budget_cents is None:
        return {
            "month": month,
            "total_cents": None,
            "spent_cents": consumption_cents,
            "remaining_cents": None,
            "daily_available_cents": None,
            "days_left": days_left,
            "over_budget": False,
            "needs_setup": True,
            "categories": categories,
        }

    remaining = total_budget_cents - consumption_cents
    daily_available = (
        max(remaining, 0) // days_left if is_current_month and days_left > 0 else None
    )
    return {
        "month": month,
        "total_cents": total_budget_cents,
        "spent_cents": consumption_cents,
        "remaining_cents": remaining,
        "daily_available_cents": daily_available,
        "days_left": days_left,
        "over_budget": remaining < 0,
        "needs_setup": False,
        "categories": categories,
    }
