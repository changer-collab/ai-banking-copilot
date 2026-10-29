"""账单能力边界：不支持的问法要明确说做不到，不能拿大额扫描结果冒充答案。"""

from __future__ import annotations

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import ScenarioRefused
from ai_banking_copilot.scenarios.bills import BillsHandler


def _intent(action: str, span: str) -> Intent:
    return Intent(action=action, slots={"span": span}, confidence=0.9, evidence=action)


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        ("把去年每个月的账单导成 Excel 发我邮箱", "导出 Excel"),
        ("深夜那几笔帮我看看", "时间点"),
        ("我在上海刷过吗", "城市"),
        ("退款到账了吗", "退款状态"),
    ],
)
def test_unsupported_questions_are_refused_with_a_human_message(
    span: str, expected: str, gateway: FactGateway
) -> None:
    with pytest.raises(ScenarioRefused, match=expected):
        BillsHandler().build([_intent("account.transactions", span)], gateway)


def test_supported_bill_question_still_builds_a_plan(gateway: FactGateway) -> None:
    plan = BillsHandler().build([_intent("bills.monthly", "这个月账单按分类看看")], gateway)
    assert plan.nodes[0].action == "bills.monthly"
