import pytest

from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan, PlanNode
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.net_effect import NetEffectError, net_effect


def _node(node_id: str, action: str, params: dict[str, object] | None = None) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action=action,
        params={} if params is None else params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=f"a_{node_id}"),
    )


def _plan(*nodes: PlanNode) -> Plan:
    return Plan(plan_id="p1", nodes=nodes)


def test_transfer_and_freeze_are_positive_outflows(gateway: FactGateway) -> None:
    plan = _plan(
        _node("n1", "transfer.create", {"amount_cents": 50000}),
        _node("n2", "funds.freeze", {"amount_cents": 100000}),
    )
    result = net_effect(plan, gateway)
    assert result.amount == Money(150000)
    assert result.irreversibles == ()
    assert result.node_ids == ("n1", "n2")


def test_cancel_subscription_subtracts_the_future_outflow(gateway: FactGateway) -> None:
    plan = _plan(_node("n1", "subscription.cancel", {"subscription_id": "sub_video"}))
    assert net_effect(plan, gateway).amount == Money(-2500)


def test_redeem_is_a_negative_outflow(gateway: FactGateway) -> None:
    plan = _plan(_node("n1", "wealth.redeem", {"amount_cents": 200000}))
    assert net_effect(plan, gateway).amount == Money(-200000)


def test_query_and_state_change_contribute_zero(gateway: FactGateway) -> None:
    plan = _plan(_node("n1", "bills.monthly"), _node("n2", "card.lock"))
    result = net_effect(plan, gateway)
    assert result.amount == Money(0)
    assert result.node_ids == ("n1", "n2")


def test_report_lost_is_listed_as_irreversible_and_excluded_from_cf(gateway: FactGateway) -> None:
    plan = _plan(
        _node("n1", "transfer.create", {"amount_cents": 50000}),
        _node("n2", "card.report_lost"),
    )
    result = net_effect(plan, gateway)
    assert result.amount == Money(50000)
    assert result.irreversibles == ("n2",)


def test_unknown_action_is_rejected(gateway: FactGateway) -> None:
    with pytest.raises(NetEffectError, match="未知动作"):
        net_effect(_plan(_node("n1", "exec_shell")), gateway)


def test_missing_amount_is_rejected(gateway: FactGateway) -> None:
    with pytest.raises(NetEffectError, match="amount_cents"):
        net_effect(_plan(_node("n1", "transfer.create")), gateway)


def test_missing_subscription_id_is_rejected(gateway: FactGateway) -> None:
    with pytest.raises(NetEffectError, match="subscription_id"):
        net_effect(_plan(_node("n1", "subscription.cancel")), gateway)
