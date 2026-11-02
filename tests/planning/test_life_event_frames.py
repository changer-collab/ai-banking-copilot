from ai_banking_copilot.planning.life_event_frames import constraints_for_frame
from ai_banking_copilot.planning.semantic_frame import SemanticFrame, SemanticValue


def test_birthday_budget_without_order_amount_stays_unpriced() -> None:
    frame = SemanticFrame(
        domain="life_event",
        operation="birthday_plan",
        mode="schedule",
        evidence="birthday budget 800",
        confidence=0.9,
        roles={"budget": SemanticValue(80000, "budget_limit", "budget 800")},
    )

    constraints = constraints_for_frame(frame)

    assert constraints.budget_cents == 80000
    assert constraints.order_cents is None


def test_birthday_order_amount_is_separate_from_budget() -> None:
    frame = SemanticFrame(
        domain="life_event",
        operation="birthday_plan",
        mode="schedule",
        evidence="birthday budget 800 order 299",
        confidence=0.9,
        roles={
            "budget": SemanticValue(80000, "budget_limit", "budget 800"),
            "order": SemanticValue(29900, "order_amount", "order 299"),
        },
    )

    constraints = constraints_for_frame(frame)

    assert constraints.budget_cents == 80000
    assert constraints.order_cents == 29900
