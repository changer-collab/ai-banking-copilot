from dataclasses import replace
from datetime import date

import pytest

from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan, PlanNode, cfhash
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.gate import StalePlanError, assert_frozen


def _node(node_id: str, amount: int, deps: tuple[str, ...] = ()) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action="transfer.create",
        params={
            "account_id": "acc_savings",
            "payee_id": "p_lina",
            "amount_cents": amount,
            "booked_on": "2026-10-01",
        },
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="frozen-transfer"),
        depends_on=deps,
    )


@pytest.mark.parametrize("tamper", ["identity", "dependencies", "execution_order"])
def test_tampered_frozen_dag_is_rejected_before_business_write(
    tamper: str, core_bank: CoreBank, gateway: FactGateway, rw_store: Store
) -> None:
    nodes = (_node("a", 100), _node("b", 200))
    frozen = cfhash(nodes)
    if tamper == "identity":
        changed = (replace(nodes[0], node_id="aa"), nodes[1])
    elif tamper == "dependencies":
        changed = (nodes[0], replace(nodes[1], depends_on=("a",)))
    else:
        changed = tuple(reversed(nodes))
    plan = Plan(plan_id="frozen-plan", nodes=changed)
    balance = gateway.account("acc_savings").payload.balance.cents
    transactions = rw_store.query("SELECT COUNT(*) FROM transactions")[0][0]

    with pytest.raises(StalePlanError, match="哈希不匹配"):
        assert_frozen(plan, frozen)
        core_bank.transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(100),
            idempotency_key="tampered-frozen-plan",
            booked_on=date(2026, 10, 1),
        )

    assert gateway.account("acc_savings").payload.balance.cents == balance
    assert rw_store.query("SELECT COUNT(*) FROM transactions")[0][0] == transactions
