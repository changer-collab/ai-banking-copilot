from dataclasses import replace

import pytest

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
)
from ai_banking_copilot.policy.gate import StalePlanError, assert_frozen


def _node(node_id: str, amount_cents: int, deps: tuple[str, ...] = ()) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action="transfer.create",
        params={"amount_cents": amount_cents},
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
        depends_on=deps,
    )


def _plan(*nodes: PlanNode) -> Plan:
    return Plan(plan_id="p1", nodes=nodes)


def test_matching_frozen_hash_passes() -> None:
    plan = _plan(_node("n1", 100), _node("n2", 200, deps=("n1",)))
    assert_frozen(plan, cfhash(plan.nodes))


def test_control_slot_param_change_is_rejected() -> None:
    plan = _plan(_node("n1", 100))
    frozen = cfhash(plan.nodes)
    tampered = _plan(replace(plan.nodes[0], params={"amount_cents": 99999}))
    with pytest.raises(StalePlanError, match="哈希不匹配"):
        assert_frozen(tampered, frozen)


def test_added_node_is_rejected() -> None:
    plan = _plan(_node("n1", 100))
    frozen = cfhash(plan.nodes)
    with pytest.raises(StalePlanError, match="哈希不匹配"):
        assert_frozen(_plan(*plan.nodes, _node("n2", 100)), frozen)


def test_removed_node_is_rejected() -> None:
    plan = _plan(_node("n1", 100), _node("n2", 200))
    frozen = cfhash(plan.nodes)
    with pytest.raises(StalePlanError, match="哈希不匹配"):
        assert_frozen(_plan(plan.nodes[0]), frozen)
