"""「这笔 6800 是不是我刷的」这类指代问法：不判未知意图、也不拿大额扫描顶替，转**追问**。

背景（10-28 验收记录 §5 T4 / 10-29 自测-05）：这句话原先走「未知意图：无法匹配任何场景」，
评委看到的是系统听不懂人话。今天改成两步：

1. 意图层：归到账单域（`bills.monthly`）——**不是** `account.transactions`，否则会先被
   「账户对象未匹配」的追问拦下，问的是「请明确改查本人账户」，答不上「哪一笔」；
2. 场景层：账单场景识别指代片段后抛 `ClarificationRequired`，追问「请补充交易日期与商户，
   或到可疑清单逐笔核对」。

同时钉住**不抢别人的话**：「这笔转账」「取消这笔订阅」仍走转账 / 订阅。
"""

from __future__ import annotations

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.planning.llm import Intent, TemplateIntentModel
from ai_banking_copilot.scenarios.base import ClarificationRequired
from ai_banking_copilot.scenarios.bills import BillsHandler

VAGUE = "这笔 6800 是不是我刷的"


def _actions(utterance: str) -> list[str]:
    return [intent.action for intent in TemplateIntentModel().propose(utterance, {}).intents]


def test_vague_transaction_reference_goes_to_the_bills_scenario() -> None:
    assert _actions(VAGUE) == ["bills.monthly"]


def test_vague_transaction_reference_does_not_steal_other_domains() -> None:
    assert _actions("这笔转账失败了怎么办") == ["transfer.create"]
    assert _actions("取消这笔订阅") == ["subscription.cancel"]


def test_bills_scenario_asks_which_transaction_instead_of_guessing(
    gateway: FactGateway,
) -> None:
    intent = Intent(action="bills.monthly", slots={"span": VAGUE}, confidence=0.9, evidence=VAGUE)
    with pytest.raises(ClarificationRequired) as error:
        BillsHandler().build([intent], gateway)
    assert "无法定位你说的那一笔" in error.value.prompt
    assert "可疑清单" in error.value.prompt


def test_normal_bill_request_is_not_affected(gateway: FactGateway) -> None:
    intent = Intent(
        action="bills.monthly",
        slots={"span": "这个月账单按分类给我看看"},
        confidence=0.9,
        evidence="账单",
    )
    plan = BillsHandler().build([intent], gateway)
    assert plan.nodes[0].action == "bills.monthly"
