"""理财域澄清（实测 WEA-Q07 / Q08 / Q20 / Q23 / Q24）。

文案层只负责「说人话 + 给候选」：不带数字、不带产品名——
产品事实一律由 `scenarios.wealth` 从事实网关取，不能在这里编。
"""

from __future__ import annotations

import re

import pytest

from ai_banking_copilot.web.triage import triage

_UTTERANCES = (
    "随便买点理财",
    "最近理财收益太低了，换个赚得多的",
    "我有5万块闲钱，半年内不用，放什么合适",
    "理财和三年定期存款哪个更划算我该买哪个",
    "理财亏了2000，要不要补仓拉低成本",
)

_PRODUCT_NAMES = ("活期理财篮子", "稳利债券", "均衡配置", "成长精选")


@pytest.mark.parametrize("utterance", _UTTERANCES)
def test_wealth_questions_get_a_wealth_answer(utterance: str) -> None:
    verdict = triage(utterance)

    assert verdict is not None
    assert verdict.kind == "ambiguous", "理财咨询不是风控拒绝，也不是闲聊"
    assert "投资建议" in verdict.message
    for word in ("不比较收益", "不预测涨跌"):
        assert word in verdict.message
    assert "生日" not in verdict.message, "不能把理财问题套进生日模板"


@pytest.mark.parametrize("utterance", _UTTERANCES)
def test_wealth_canned_copy_carries_no_facts(utterance: str) -> None:
    verdict = triage(utterance)

    assert verdict is not None
    assert not re.search(r"\d", verdict.message), "文案层不许出现数字（事实只能来自网关）"
    for name in _PRODUCT_NAMES:
        assert name not in verdict.message


def test_wealth_suggestions_are_actionable() -> None:
    verdict = triage("随便买点理财")

    assert verdict is not None
    texts = [item.text for item in verdict.suggestions]
    assert 1 <= len(texts) <= 3
    assert any("测评" in text for text in texts)
    assert any("理财" in text for text in texts)


def test_ambiguous_anaphora_still_defers_to_clarification() -> None:
    """「就买刚才那个，500」在理财承接之前不能被理财文案吞掉。"""
    verdict = triage("就买刚才那个，500", low_confidence=True)
    assert verdict is not None and verdict.kind == "ambiguous"
