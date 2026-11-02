"""Life-event constraints derived from validated semantic frames."""

from __future__ import annotations

from dataclasses import dataclass

from ai_banking_copilot.planning.semantic_frame import SemanticFrame


@dataclass(frozen=True)
class LifeEventConstraints:
    budget_cents: int | None
    order_cents: int | None


def constraints_for_frame(frame: SemanticFrame | None) -> LifeEventConstraints:
    """Extract compatible money roles without cross-role coercion."""
    if frame is None or (frame.domain, frame.operation) != ("life_event", "birthday_plan"):
        return LifeEventConstraints(budget_cents=None, order_cents=None)
    budget_cents: int | None = None
    order_cents: int | None = None
    for value in frame.roles.values():
        if not isinstance(value.value, int) or isinstance(value.value, bool) or value.value <= 0:
            continue
        if value.role == "budget_limit":
            budget_cents = value.value
        elif value.role == "order_amount":
            order_cents = value.value
    return LifeEventConstraints(budget_cents=budget_cents, order_cents=order_cents)
