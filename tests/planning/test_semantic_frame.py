import pytest

from ai_banking_copilot.planning.semantic_frame import (
    FrameError,
    SemanticFrame,
    SemanticValue,
    action_for_frame,
    frame_options_for_actions,
    normalize_frame,
    validate_frame,
)
from ai_banking_copilot.tools.catalog import build_default_registry


def test_budget_limit_is_not_an_order_amount() -> None:
    utterance = "birthday budget 800 yuan"
    frame = SemanticFrame(
        domain="life_event",
        operation="birthday_plan",
        mode="schedule",
        evidence=utterance,
        confidence=0.95,
        roles={
            "budget": SemanticValue(
                value=80000,
                role="budget_limit",
                evidence="budget 800",
            )
        },
    )

    normalized = validate_frame(frame, utterance)

    assert normalized.roles["budget"].role == "budget_limit"
    assert all(item.role != "order_amount" for item in normalized.roles.values())


def test_frame_rejects_role_evidence_not_present_in_utterance() -> None:
    frame = SemanticFrame(
        domain="transfer",
        operation="create",
        mode="write",
        evidence="pay Li Na 500",
        confidence=0.95,
        roles={
            "amount": SemanticValue(
                value=50000,
                role="transfer_amount",
                evidence="600",
            )
        },
    )

    with pytest.raises(FrameError, match="evidence"):
        validate_frame(frame, "pay Li Na 500")


def test_action_mapping_is_server_owned_and_only_allows_known_frame() -> None:
    frame = SemanticFrame(
        domain="transfer",
        operation="create",
        mode="write",
        evidence="pay Li Na 500",
        confidence=0.95,
    )

    assert action_for_frame(frame) == "transfer.create"


def test_unknown_frame_does_not_map_to_an_action() -> None:
    frame = SemanticFrame(
        domain="transfer",
        operation="shell",
        mode="write",
        evidence="run command",
        confidence=0.95,
    )

    assert action_for_frame(frame) is None


def test_semantic_frame_allowlist_covers_every_registered_action() -> None:
    registry = build_default_registry()

    options = frame_options_for_actions(registry.actions())

    assert {option.action for option in options} == set(registry.actions())
    assert all(option.domain and option.operation and option.mode for option in options)


def test_card_limit_frame_only_accepts_a_limit_role() -> None:
    utterance = "把日限额调到1000元"
    valid = SemanticFrame(
        domain="card",
        operation="set_limit",
        mode="write",
        evidence=utterance,
        confidence=0.95,
        roles={
            "limit": SemanticValue(100000, "daily_limit", "1000元"),
        },
    )
    invalid = SemanticFrame(
        domain="card",
        operation="set_limit",
        mode="write",
        evidence=utterance,
        confidence=0.95,
        roles={
            "amount": SemanticValue(100000, "transfer_amount", "1000元"),
        },
    )

    assert validate_frame(valid, utterance).roles["limit"].role == "daily_limit"
    with pytest.raises(FrameError, match="not allowed"):
        validate_frame(invalid, utterance)


def test_explicit_timed_transfer_normalizes_a_model_book_misclassification() -> None:
    frame = SemanticFrame(
        domain="transfer",
        operation="book",
        mode="schedule",
        evidence="定时给李娜转500元",
        confidence=0.95,
    )

    normalized = normalize_frame(frame)

    assert normalized.operation == "schedule"
    assert action_for_frame(normalized) == "transfer.schedule"
