import json

import pytest

from ai_banking_copilot.eval.semantic_frame_cases import SEMANTIC_FRAME_CASES
from ai_banking_copilot.planning.llm import LlmIntentModel
from ai_banking_copilot.tools.catalog import build_default_registry


def test_semantic_frame_suite_has_30_cases_and_all_contest_domains() -> None:
    assert len(SEMANTIC_FRAME_CASES) == 30
    assert {case.domain for case in SEMANTIC_FRAME_CASES} == {
        "account", "transfer", "bills", "wealth", "card", "subscription", "life_event",
    }
    registry = build_default_registry()
    assert {case.expected_action for case in SEMANTIC_FRAME_CASES if case.expected_action} == set(
        registry.actions()
    )


@pytest.mark.parametrize(
    "case",
    [case for case in SEMANTIC_FRAME_CASES if case.expected_action],
    ids=lambda case: case.case_id,
)
def test_each_regression_case_maps_only_through_the_server_owned_frame(case) -> None:
    payload = {"frames": [case.frame_payload()]}
    proposal = LlmIntentModel(
        transport=lambda _request: json.dumps(payload, ensure_ascii=False),
    ).propose(case.utterance, {})

    assert proposal.frames[0].domain == case.domain
    assert proposal.frames[0].operation == case.operation
    assert proposal.intents[0].action == case.expected_action
