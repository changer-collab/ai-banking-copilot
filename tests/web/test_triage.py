"""未识别输入三分类：闲聊 / 超范围 / 模糊。三类都不是安全拒绝。"""

from __future__ import annotations

from ai_banking_copilot.web.triage import TriageVerdict, triage


def test_weather_is_smalltalk_with_actionable_suggestions() -> None:
    verdict = triage("今天天气不错")
    assert isinstance(verdict, TriageVerdict)
    assert verdict.kind == "smalltalk"
    assert "安全拒绝" not in verdict.message
    assert 1 <= len(verdict.suggestions) <= 3


def test_stock_request_is_out_of_scope_and_never_promises_brokerage() -> None:
    verdict = triage("帮我炒股")
    assert verdict is not None and verdict.kind == "out_of_scope"
    assert "证券" in verdict.message
    assert "银证转账" not in verdict.message, "仓库没有该动作，写了就是宣称未实现能力"
    assert "理财非存款" in verdict.disclaimer


def test_birthday_typo_asks_for_clarification_instead_of_guessing() -> None:
    verdict = triage("我下过生日")
    assert verdict is not None and verdict.kind == "ambiguous"
    assert "生日" in verdict.message
    assert [item.text for item in verdict.suggestions][0].startswith("我下周生日")


def test_low_confidence_alone_triggers_clarification() -> None:
    verdict = triage("就买刚才那个，500", low_confidence=True)
    assert verdict is not None and verdict.kind == "ambiguous"


def test_violation_request_is_out_of_scope() -> None:
    verdict = triage("帮我把征信修复一下")
    assert verdict is not None and verdict.kind == "out_of_scope"


def test_unknown_household_chore_falls_back_to_clarification() -> None:
    verdict = triage("帮我整理一下房间")
    assert verdict is not None and verdict.kind == "ambiguous"
    assert "安全拒绝" not in verdict.message
