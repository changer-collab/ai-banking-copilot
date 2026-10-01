import pytest

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    PlanValidationError,
    cfhash,
    validate_plan,
)


def _node(node_id: str, amount: int = 100, deps: tuple[str, ...] = ()) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action="transfer.create",
        params={"amount": amount, "to": "saved:001"},
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
        depends_on=deps,
    )


def test_valid_plan_passes() -> None:
    plan = Plan(plan_id="p1", nodes=(_node("n1"), _node("n2", deps=("n1",))))
    validate_plan(plan)


def test_node_without_intent_anchor_is_rejected() -> None:
    bad = PlanNode(node_id="n1", action="transfer.create", params={"amount": 100})
    with pytest.raises(PlanValidationError, match="intent anchor"):
        validate_plan(Plan(plan_id="p1", nodes=(bad,)))


def test_derived_anchor_is_rejected_at_node_level() -> None:
    node = PlanNode(
        node_id="n1",
        action="transfer.create",
        params={"amount": 100},
        intent=IntentRef(source=AnchorSource.DERIVED, anchor_id="d1"),
    )
    with pytest.raises(PlanValidationError, match="derived"):
        validate_plan(Plan(plan_id="p1", nodes=(node,)))


def test_unknown_dependency_is_rejected() -> None:
    with pytest.raises(PlanValidationError, match="unknown"):
        validate_plan(Plan(plan_id="p1", nodes=(_node("n1", deps=("n9",)),)))


def test_cycle_is_rejected() -> None:
    a = _node("a", deps=("b",))
    b = _node("b", deps=("a",))
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan(Plan(plan_id="p1", nodes=(a, b)))


def test_duplicate_node_id_is_rejected() -> None:
    with pytest.raises(PlanValidationError, match="duplicate"):
        validate_plan(Plan(plan_id="p1", nodes=(_node("n1"), _node("n1"))))


def test_cfhash_is_stable_under_param_key_order() -> None:
    n1 = PlanNode(
        node_id="n1",
        action="transfer.create",
        params={"to": "saved:001", "amount": 100},
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
    )
    n2 = PlanNode(
        node_id="n1",
        action="transfer.create",
        params={"amount": 100, "to": "saved:001"},
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
    )
    assert cfhash((n1,)) == cfhash((n2,))


def test_cfhash_changes_when_control_slot_param_changes() -> None:
    assert cfhash((_node("n1", amount=100),)) != cfhash((_node("n1", amount=101),))
