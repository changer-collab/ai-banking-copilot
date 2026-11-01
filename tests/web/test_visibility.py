"""Q18：私密执行不进共享推送；LLM 推荐只能来自固定清单。"""

from __future__ import annotations

from ai_banking_copilot.web.session import wants_private_visibility
from ai_banking_copilot.web.suggest import CAPABILITIES, select_suggestions


def test_privacy_phrase_is_detected() -> None:
    assert wants_private_visibility("别在她常看的账单推送里暴露这笔支出") is True
    assert wants_private_visibility("帮我订一束花") is False


def test_llm_outside_capabilities_falls_back() -> None:
    result = select_suggestions(
        "随便说点什么", llm=lambda _text, _caps: ["联系客服"] * 3,
    )
    assert all(item["text"] in CAPABILITIES for item in result)
    assert len(result) == 3
