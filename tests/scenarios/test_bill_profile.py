"""账单画像 / 趋势：近 6 月支出、分类占比、消费结构 / 订阅负担 / 大额风险都由服务端算好。

口径：数字只来自事实网关（`monthly_bill` / `subscriptions`）与识别报告，前端不除不估。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.scenarios.bill_findings import (
    TREND_MONTHS,
    BillFindingsReport,
    analyze_bill,
    bill_profile,
    category_shares,
    expense_trend,
)

AS_OF = date(2026, 10, 1)
PERIOD = (2026, 9)


def _report(gateway: FactGateway) -> BillFindingsReport:
    return analyze_bill(gateway, DEMO_USER_ID, *PERIOD, as_of=AS_OF)


def test_trend_covers_six_months_and_ends_at_target_period(gateway: FactGateway) -> None:
    report = _report(gateway)
    trend = expense_trend(gateway, DEMO_USER_ID, *PERIOD)

    assert len(trend) == TREND_MONTHS
    assert [point.period for point in trend] == [
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
        "2026-09",
    ]
    expected = sum(-item.amount_cents for item in report.transactions)
    assert trend[-1].expense_cents == expected
    assert all(point.expense_cents >= 0 for point in trend)


def test_category_shares_sum_to_the_period_expense(gateway: FactGateway) -> None:
    shares = category_shares(gateway, DEMO_USER_ID, *PERIOD)

    assert shares, "种子数据本期应有支出分类"
    total = sum(item.expense_cents for item in shares)
    assert total > 0
    share_total = sum((item.share for item in shares), Decimal(0)).quantize(Decimal("0.01"))
    assert share_total == Decimal("1.00")
    assert [item.expense_cents for item in shares] == sorted(
        (item.expense_cents for item in shares), reverse=True
    )


def test_profile_is_derived_from_report_and_subscriptions(gateway: FactGateway) -> None:
    report = _report(gateway)
    profile = bill_profile(gateway, DEMO_USER_ID, report)
    shares = category_shares(gateway, DEMO_USER_ID, *PERIOD)

    assert profile.top_category == shares[0].category
    assert profile.top_category_cents == shares[0].expense_cents
    assert profile.top_category_share == shares[0].share
    assert profile.subscription_monthly_cents > 0

    risky = [
        item
        for item in report.findings
        if item.status in {"confirmed", "suspected"}
    ]
    assert profile.suspicious_count == len(risky)
    assert profile.max_suspicious_cents == max((item.impact_cents for item in risky), default=0)


def test_empty_period_keeps_zero_trend_and_no_profile_top(gateway: FactGateway) -> None:
    report = analyze_bill(gateway, DEMO_USER_ID, 2020, 1, as_of=AS_OF)
    trend = expense_trend(gateway, DEMO_USER_ID, 2020, 1)
    profile = bill_profile(gateway, DEMO_USER_ID, report)

    assert all(point.expense_cents == 0 for point in trend)
    assert category_shares(gateway, DEMO_USER_ID, 2020, 1) == ()
    assert profile.top_category is None
    assert profile.top_category_share is None
    assert profile.suspicious_count == 0


def test_trend_rejects_non_positive_window(gateway: FactGateway) -> None:
    with pytest.raises(ValueError, match="months"):
        expense_trend(gateway, DEMO_USER_ID, 2026, 9, months=0)
