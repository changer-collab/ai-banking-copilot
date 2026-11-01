"""未识别意图的功能推荐：没有模型也能给 3 条，且只来自固定能力清单。"""

from __future__ import annotations

from ai_banking_copilot.web.suggest import CAPABILITIES, select_suggestions


def test_without_llm_returns_three_capabilities() -> None:
    result = select_suggestions("随便说点什么")

    assert len(result) == 3
    assert all(item["text"] in CAPABILITIES for item in result)


def test_partial_llm_result_is_padded_without_duplicates() -> None:
    only = next(iter(CAPABILITIES))
    result = select_suggestions("随便说点什么", llm=lambda _text, _caps: [only])

    assert len(result) == 3
    texts = [item["text"] for item in result]
    assert texts[0] == only
    assert len(set(texts)) == 3
    assert all(text in CAPABILITIES for text in texts)
