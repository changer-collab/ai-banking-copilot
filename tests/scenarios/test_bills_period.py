"""账单场景账期解析：本月 / 上月 / 显式月份。

基准日为 `2026-10-31`，故「本月」= 2026-10、「上月」= 2026-09；两句不得返回同一月。
"""

from __future__ import annotations

import pytest

from ai_banking_copilot.planning.llm import TemplateIntentModel
from ai_banking_copilot.scenarios.bills import BillsHandler


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("查一下本月账单", (2026, 10)),
        ("查一下这个月账单", (2026, 10)),
        ("查一下上月账单", (2026, 9)),
        ("查一下上个月账单", (2026, 9)),
        ("查一下 2026-09 账单", (2026, 9)),
    ],
)
def test_relative_bill_month(gateway, utterance, expected) -> None:
    intents = TemplateIntentModel().propose(utterance, {}).intents
    plan = BillsHandler().build(intents, gateway)
    node = next(n for n in plan.nodes if n.action == "bills.monthly")
    assert (node.params["year"], node.params["month"]) == expected
