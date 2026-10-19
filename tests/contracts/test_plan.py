from dataclasses import replace

import pytest

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    PlanValidationError,
    cfhash,
    topological_order,
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


def test_topological_order_follows_dependencies_not_listing_order() -> None:
    """节点列表顺序**不是**依赖序（乱序合法 DAG）时，执行序仍由 `depends_on` 决定。"""
    plan = Plan(plan_id="p", nodes=(_node("c", deps=("b",)), _node("a"), _node("b", deps=("a",))))
    validate_plan(plan)
    assert topological_order(plan) == ("a", "b", "c")


def test_topological_order_is_deterministic_for_independent_nodes() -> None:
    """无依赖的并列节点按计划中靠前者优先，保证执行序可复现、可审计。"""
    plan = Plan(plan_id="p", nodes=(_node("zeta"), _node("alpha")))
    assert topological_order(plan) == ("zeta", "alpha")


def test_topological_order_rejects_a_cyclic_plan() -> None:
    plan = Plan(plan_id="p", nodes=(_node("a", deps=("b",)), _node("b", deps=("a",))))
    with pytest.raises(PlanValidationError, match="cycle"):
        topological_order(plan)


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


def test_duplicate_dependency_is_rejected() -> None:
    plan = Plan(plan_id="p1", nodes=(_node("a"), _node("b", deps=("a", "a"))))
    with pytest.raises(PlanValidationError, match="duplicate dependency"):
        validate_plan(plan)


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


def test_cfhash_changes_when_node_identity_changes() -> None:
    assert cfhash((_node("a"),)) != cfhash((_node("renamed"),))


def test_cfhash_changes_when_dependencies_reverse() -> None:
    forward = (_node("a"), _node("b", deps=("a",)))
    reverse = (_node("a", deps=("b",)), _node("b"))
    assert cfhash(forward) != cfhash(reverse)


def test_cfhash_changes_when_dependency_changes_without_reordering_execution() -> None:
    dependent = (_node("a"), _node("b", deps=("a",)))
    independent = (_node("a"), _node("b"))
    assert topological_order(Plan(plan_id="p", nodes=dependent)) == ("a", "b")
    assert topological_order(Plan(plan_id="p", nodes=independent)) == ("a", "b")
    assert cfhash(dependent) != cfhash(independent)


def test_cfhash_changes_when_independent_execution_order_changes() -> None:
    nodes = (_node("a"), _node("b"))
    assert cfhash(nodes) != cfhash(tuple(reversed(nodes)))


def test_cfhash_is_stable_when_listing_order_does_not_change_execution_order() -> None:
    nodes = (_node("a"), _node("b", deps=("a",)), _node("c", deps=("b",)))
    assert cfhash(nodes) == cfhash(tuple(reversed(nodes)))


def test_cfhash_is_stable_under_dependency_declaration_order() -> None:
    nodes = (_node("a"), _node("b"), _node("c", deps=("a", "b")))
    reordered = nodes[:2] + (replace(nodes[2], depends_on=("b", "a")),)
    assert cfhash(nodes) == cfhash(reordered)


def test_cfhash_changes_when_action_or_anchor_changes() -> None:
    node = _node("a")
    assert cfhash((node,)) != cfhash((replace(node, action="transfer.schedule"),))
    intent = IntentRef(source=AnchorSource.USER_APPROVED, anchor_id="another-anchor")
    assert cfhash((node,)) != cfhash((replace(node, intent=intent),))


@pytest.mark.parametrize(
    ("nodes", "reason"),
    [
        ((_node("a"), _node("a")), "duplicate node_id"),
        ((_node("a"), _node("b", deps=("a", "a"))), "duplicate dependency"),
        ((replace(_node("a"), intent=None),), "intent anchor"),
        ((_node("a", deps=("missing",)),), "unknown"),
        ((_node("a", deps=("b",)), _node("b", deps=("a",))), "cycle"),
        (
            (
                replace(
                    _node("a"),
                    intent=IntentRef(source=AnchorSource.DERIVED, anchor_id="derived"),
                ),
            ),
            "derived",
        ),
    ],
)
def test_cfhash_rejects_invalid_plan(nodes: tuple[PlanNode, ...], reason: str) -> None:
    with pytest.raises(PlanValidationError, match=reason):
        cfhash(nodes)


def test_cfhash_accepts_empty_nodes() -> None:
    empty_hash = cfhash(())
    assert len(empty_hash) == 64
    assert int(empty_hash, 16) >= 0
    assert cfhash(()) == empty_hash
