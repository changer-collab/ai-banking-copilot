import pytest

from ai_banking_copilot.planning.llm import (
    ACTION_KINDS,
    CANONICAL_ACTIONS,
    Intent,
    IntentModel,
    IntentProposal,
    TemplateIntentModel,
)
from ai_banking_copilot.tools.catalog import build_default_registry


def test_action_kinds_cover_the_canonical_vocabulary() -> None:
    assert set(ACTION_KINDS) == set(CANONICAL_ACTIONS)
    assert len(CANONICAL_ACTIONS) == 20


def test_action_kinds_match_the_tool_registry_vocabulary() -> None:
    """交叉断言：04 的动作词表必须与 02 目录**逐项一致**（README 规范动作表唯一真源）。"""
    assert set(ACTION_KINDS) == set(build_default_registry().actions())


def test_intent_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        Intent(action="bills.monthly", confidence=1.5)


def test_intent_requires_action() -> None:
    with pytest.raises(ValueError):
        Intent(action="")


def test_template_model_satisfies_the_protocol() -> None:
    assert isinstance(TemplateIntentModel(), IntentModel)


def test_template_model_extracts_multiple_intents_in_clause_order() -> None:
    utterance = "转给李娜 500 元，再查一下上月账单"
    proposal = TemplateIntentModel().propose(utterance, {})
    assert isinstance(proposal, IntentProposal)
    assert [intent.action for intent in proposal.intents] == ["transfer.create", "bills.monthly"]
    assert proposal.raw == utterance


def test_template_model_keeps_clause_span_as_a_hint() -> None:
    proposal = TemplateIntentModel().propose("转给李娜 500 元，再查一下上月账单", {})
    assert proposal.intents[0].slots["span"] == "转给李娜 500 元"


def test_template_model_prefers_scheduled_transfer_over_plain_transfer() -> None:
    proposal = TemplateIntentModel().propose("定时转账给李娜 500 元", {})
    assert [intent.action for intent in proposal.intents] == ["transfer.schedule"]


def test_template_model_returns_nothing_for_unknown_utterance() -> None:
    assert TemplateIntentModel().propose("今天天气不错", {}).intents == ()


def test_template_model_is_deterministic() -> None:
    model = TemplateIntentModel()
    assert model.propose("冻结 5000 元", {}) == model.propose("冻结 5000 元", {})
