"""理财咨询的期限 / 起购筛选（实测 WEA-Q20 / Q23）。

两条纪律：

1. 两个筛选都是**上限**，只会让候选集合变小，不可能放宽风险等级闸门；
2. 解析不到就按「不筛」处理，不猜期限、不猜金额。
"""

from __future__ import annotations

import pytest

from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.planning.slots import (
    OPTIONAL_SLOTS,
    REQUIRED_SLOTS,
    parse_horizon_days,
)
from ai_banking_copilot.scenarios.wealth import _build_wealth_node, filter_candidates
from ai_banking_copilot.web.app import AppDeps


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("半年内不用", 180),
        ("6 个月不用", 180),
        ("三个月内要用", 90),
        ("一年以上都用不到", 365),
        ("90 天后要用", 90),
        ("我想买点理财", None),
    ],
)
def test_parse_horizon_days_reads_only_what_the_user_said(text: str, days: int | None) -> None:
    assert parse_horizon_days(text) == days


def test_wealth_list_optional_slots_do_not_become_questions() -> None:
    """可选槽绝不能进必填表，否则每条理财查询都会多问一句。"""
    assert REQUIRED_SLOTS["wealth.list"] == ()
    assert OPTIONAL_SLOTS["wealth.list"] == ("horizon", "max_amount")


def test_filter_candidates_only_narrows(deps: AppDeps) -> None:
    products = deps.gateway.wealth_products()

    kept = filter_candidates(products, 180, 500_000)

    assert len(kept) <= len(products)
    assert all(item.payload.term_days <= 180 for item in kept)
    assert all(item.payload.min_amount.cents <= 500_000 for item in kept)
    assert filter_candidates(products, None, None) == tuple(products), "不传就不筛"


def test_consultation_plan_carries_upper_bounds(deps: AppDeps) -> None:
    """规划层解析到的期限 / 金额要作为**上限**落进计划参数（口径见 catalog.slot_name）。"""
    intent = Intent(
        action="wealth.list",
        slots={
            "horizon": Value(180, Trust.TRUSTED),
            "max_amount": Value(5_000_000, Trust.TRUSTED),
        },
        confidence=1.0,
        evidence="我手上有五万块闲钱，接下来半年内都不会用到",
    )

    node = _build_wealth_node("n1", intent, deps.gateway, AnchorLedger())

    assert node.action == "wealth.list"
    assert node.params.get("max_term_days") == 180
    assert node.params.get("max_amount_cents") == 5_000_000


def test_untrusted_or_missing_slots_are_not_used_as_filters(deps: AppDeps) -> None:
    """不可信来源的期限不能进查询参数（控制槽零信任）。"""
    intent = Intent(
        action="wealth.list",
        slots={"horizon": Value(180, Trust.UNVERIFIED)},
        confidence=1.0,
        evidence="半年",
    )

    node = _build_wealth_node("n1", intent, deps.gateway, AnchorLedger())

    assert "max_term_days" not in node.params
