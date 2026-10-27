"""风险测评问卷契约：题库完整性、确定性评分、非法答案拒绝。"""

from __future__ import annotations

import pytest

from ai_banking_copilot.scenarios.wealth_assessment import (
    MAX_SCORE,
    QUESTIONNAIRE,
    AssessmentError,
    evaluate_answers,
    questionnaire_payload,
    score_to_profile,
)

_ALL_LOWEST = {question.id: question.options[0].id for question in QUESTIONNAIRE}
_ALL_HIGHEST = {question.id: question.options[-1].id for question in QUESTIONNAIRE}


def test_questionnaire_has_five_questions_with_four_options() -> None:
    assert len(QUESTIONNAIRE) == 5
    assert len({question.id for question in QUESTIONNAIRE}) == 5
    for question in QUESTIONNAIRE:
        assert len(question.options) == 4
        assert len({option.id for option in question.options}) == 4
    assert MAX_SCORE == 15


def test_payload_does_not_leak_scores() -> None:
    payload = questionnaire_payload()
    assert len(payload) == 5
    for question in payload:
        options = question["options"]
        assert isinstance(options, list)
        for option in options:
            assert set(option) == {"id", "label"}


def test_lowest_answers_map_to_r1_and_highest_to_r5() -> None:
    lowest = evaluate_answers(_ALL_LOWEST)
    highest = evaluate_answers(_ALL_HIGHEST)
    assert lowest.score == 0 and lowest.risk_profile == "R1"
    assert highest.score == MAX_SCORE and highest.risk_profile == "R5"


def test_same_answers_are_deterministic() -> None:
    answers = {**_ALL_HIGHEST, QUESTIONNAIRE[0].id: QUESTIONNAIRE[0].options[1].id}
    first = evaluate_answers(answers)
    second = evaluate_answers(dict(answers))
    assert first == second


def test_answers_are_recorded_in_question_order() -> None:
    result = evaluate_answers(_ALL_HIGHEST)
    assert [question_id for question_id, _ in result.answers] == [
        question.id for question in QUESTIONNAIRE
    ]


def test_missing_answer_is_rejected() -> None:
    incomplete = dict(_ALL_HIGHEST)
    incomplete.pop(QUESTIONNAIRE[2].id)
    with pytest.raises(AssessmentError, match="未作答"):
        evaluate_answers(incomplete)


def test_unknown_question_is_rejected() -> None:
    with pytest.raises(AssessmentError, match="未知题号"):
        evaluate_answers({**_ALL_HIGHEST, "q9_fake": "q9_a"})


def test_unknown_option_is_rejected() -> None:
    answers = {**_ALL_HIGHEST, QUESTIONNAIRE[2].id: "q3_z"}
    with pytest.raises(AssessmentError, match="没有选项"):
        evaluate_answers(answers)


@pytest.mark.parametrize(
    ("score", "expected"),
    [(0, "R1"), (2, "R1"), (3, "R2"), (5, "R2"), (6, "R3"), (8, "R3"),
     (9, "R4"), (11, "R4"), (12, "R5"), (15, "R5")],
)
def test_score_boundaries(score: int, expected: str) -> None:
    assert score_to_profile(score) == expected


def test_score_out_of_range_is_rejected() -> None:
    with pytest.raises(AssessmentError):
        score_to_profile(MAX_SCORE + 1)
