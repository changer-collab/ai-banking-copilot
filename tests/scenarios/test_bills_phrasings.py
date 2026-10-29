"""账单口述覆盖：「这个月钱花哪了」这类口语问法必须落进 `bills.monthly`。

背景：10-28 的 ⑤ 考题清单里 ① 就是「这个月钱花哪了」；分支合入 10-29 的改动后，
模板规则只剩「花了多少 / 消费明细」，该问法退回「未知意图：无法匹配任何场景」。
这里把三种口语问法钉住，避免再被别人的模板改写顺手弄丢。
"""

from __future__ import annotations

import pytest

from ai_banking_copilot.planning.llm import TemplateIntentModel

PHRASINGS = ("这个月钱花哪了", "钱花哪了", "这个月花了多少")


@pytest.mark.parametrize("utterance", PHRASINGS)
def test_colloquial_spending_questions_map_to_monthly_bills(utterance: str) -> None:
    proposal = TemplateIntentModel().propose(utterance, {})
    assert [intent.action for intent in proposal.intents] == ["bills.monthly"]


def test_yearly_bill_still_wins_over_the_colloquial_rule() -> None:
    """「年度账单」仍优先走 `bills.yearly`：口语规则不能把年账单吃掉。"""
    proposal = TemplateIntentModel().propose("看看今年的年度账单", {})
    assert [intent.action for intent in proposal.intents] == ["bills.yearly"]
