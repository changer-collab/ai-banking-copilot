"""账期解析：原话里写明的账期必须落到计划参数上，没写才按「上一个月」。

背景（手动实测 BIL-07 / 10-29 自测-07）：「查 2019 年 1 月账单」原先和「查账单」一样，
一律按基准记账日的上一个月出计划——问的是 2019 年，算的是 2026 年，数字对不上用户原话。

口径：
- 只认**写明**的账期：「2019 年 1 月」「2026-09」「去年 12 月」；不猜「上上个月」；
- 越界（2000–2100 年、1–12 月）直接拒绝，不静默换成本月；
- 未写账期时行为与之前一致（基准记账日的上一个月），不改变既有用例语义。
"""

from __future__ import annotations

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import ScenarioRefused
from ai_banking_copilot.scenarios.bills import BillsHandler


def _plan_for(utterance: str, gateway: FactGateway, action: str = "bills.monthly"):  # type: ignore[no-untyped-def]
    intent = Intent(action=action, slots={"span": utterance}, confidence=0.9, evidence=utterance)
    return BillsHandler().build([intent], gateway)


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("查 2019 年 1 月账单", ("2019", "1")),
        ("看看 2019 年 1 月的账单", ("2019", "1")),
        ("查 2026-09 账单", ("2026", "9")),
        ("查一下去年 12 月账单", ("2025", "12")),
        ("查今年 3 月的账单", ("2026", "3")),
    ],
)
def test_stated_period_reaches_the_plan(
    utterance: str, expected: tuple[str, str], gateway: FactGateway
) -> None:
    plan = _plan_for(utterance, gateway)
    node = plan.nodes[0]
    assert node.action == "bills.monthly"
    assert (str(node.params["year"]), str(node.params["month"])) == expected


def test_missing_period_keeps_the_previous_month_default(gateway: FactGateway) -> None:
    assert _plan_for("这个月账单按分类给我看看", gateway).nodes[0].params["year"] == 2026
    assert _plan_for("这个月账单按分类给我看看", gateway).nodes[0].params["month"] == 9


@pytest.mark.parametrize("utterance", ["查 2019 年 13 月账单", "查 1999 年 1 月账单"])
def test_out_of_range_period_is_refused(utterance: str, gateway: FactGateway) -> None:
    with pytest.raises(ScenarioRefused):
        _plan_for(utterance, gateway)


def test_yearly_bill_honours_the_stated_year(gateway: FactGateway) -> None:
    plan = _plan_for("查 2019 年的年度账单", gateway, action="bills.yearly")
    assert plan.nodes[0].action == "bills.yearly"
    assert plan.nodes[0].params["year"] == 2019
